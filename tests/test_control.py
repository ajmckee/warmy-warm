"""Deterministic Phase 2 gate against real HA services and the simulated climate."""

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry
from homeassistant.util import dt as dt_util

from custom_components.thermal_planner.const import DOMAIN
from custom_components.thermal_planner.control import MAX_RETRIES, LabController
from custom_components.thermal_planner.integration import ReplayManager

CLIMATE = "climate.fake_house_heat_pump"


@pytest.fixture
async def lab(simulator_factory, simulated_cooldown):
    hass = await simulator_factory()
    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": CLIMATE, "temperature": 24}, blocking=True
    )
    entry = SimpleNamespace(
        entry_id="lab-entry", data=deepcopy(simulated_cooldown["config"]), options={}
    )
    entry.data["heat_source"] = {
        "name": "Test pump",
        "climate": CLIMATE,
        "pause_limit_minutes": 60,
        "dwell_minutes": 1,
    }
    manager = ReplayManager(hass, entry)
    hass.data[DOMAIN] = {entry.entry_id: manager}
    clock = [dt_util.utcnow()]
    controller = LabController(hass, now=lambda: clock[0])
    await controller.load()
    yield hass, manager, controller, clock
    controller.close()


async def test_persist_before_off_stop_readback_and_idempotence(lab):
    hass, manager, controller, _ = lab
    original = hass.services.async_call

    async def checked(domain, service, data, **kwargs):
        if data.get("hvac_mode") == "off":
            saved = (await controller.store.async_load())["journal"]
            assert saved["phase"] == "prepared"
            assert saved["previous_mode"] == "heat"
            assert saved["previous_target"] == 24
            assert saved["deadline"] and saved["run_id"]
        return await original(domain, service, data, **kwargs)

    with patch.object(type(hass.services), "async_call", side_effect=checked):
        await controller.start(manager, 5)
    assert hass.states.get(CLIMATE).state == "off"
    assert controller.journal["phase"] == "paused"
    with pytest.raises(ServiceValidationError, match="already"):
        await controller.start(manager, 5)
    await controller.stop(manager.entry.entry_id)
    assert hass.states.get(CLIMATE).state == "heat"
    assert hass.states.get(CLIMATE).attributes["temperature"] == 24
    assert controller.journal is None
    assert (await controller.store.async_load())["journal"] is None
    assert controller.last_run["outcome"] == "restored"
    with patch.object(type(hass.services), "async_call") as calls:
        await controller.stop(manager.entry.entry_id)
        calls.assert_not_called()


async def test_deadline_uses_absolute_clock(lab):
    hass, manager, controller, clock = lab
    await controller.start(manager, 1)
    clock[0] += timedelta(seconds=61)
    await controller.monitor()
    assert hass.states.get(CLIMATE).state == "heat"
    assert controller.last_run["reason"] == "Pause deadline reached."


async def test_guard_abort_uses_persisted_room_snapshot(lab):
    hass, manager, controller, _ = lab
    await controller.start(manager, 60)
    manager.entry.data["rooms"][2]["comfort_min"] = 5
    await hass.services.async_call("fake_house", "step", {"minutes": 120}, blocking=True)
    await controller.monitor()
    assert hass.states.get(CLIMATE).state == "heat"
    assert "bathroom" in controller.last_run["reason"]
    assert controller.last_run["guards"]["rooms"][2]["comfort_min"] == 19


@pytest.mark.parametrize("failure", ["unavailable", "unknown", "nan", "stale"])
async def test_invalid_guard_aborts_and_restores(lab, failure):
    hass, manager, controller, clock = lab
    await controller.start(manager, 60)
    if failure == "stale":
        clock[0] += timedelta(minutes=16)
    else:
        hass.states.async_set("sensor.fake_house_bathroom", failure, {"unit_of_measurement": "°C"})
    await controller.monitor()
    assert hass.states.get(CLIMATE).state == "heat"
    assert "Missing or stale" in controller.last_run["reason"]


async def test_actual_ha_restart_restores_simulated_climate(lab, simulator_factory, caplog):
    hass, manager, controller, _ = lab
    await controller.start(manager, 60)
    await hass.async_stop(force=True)
    restarted = await simulator_factory()
    assert restarted.states.get(CLIMATE).state == "off"
    restored = LabController(restarted)
    await restored.async_setup()
    restarted.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await restarted.async_block_till_done()
    assert restarted.states.get(CLIMATE).state == "heat"
    assert restarted.states.get(CLIMATE).attributes["temperature"] == 24
    assert restored.journal is None
    assert restored.last_run["outcome"] == "restored"
    assert "restart" in restored.last_run["reason"]
    restored.close()
    assert "Unable to remove unknown job listener" not in caplog.text


@pytest.mark.parametrize("after_off", [False, True])
async def test_crash_between_prepared_command_and_ack(lab, simulator_factory, after_off):
    hass, manager, controller, _ = lab
    original = hass.services.async_call

    class SimulatedCrash(BaseException):
        pass

    async def crash(domain, service, data, **kwargs):
        if data.get("hvac_mode") == "off":
            if after_off:
                await original(domain, service, data, **kwargs)
            raise SimulatedCrash
        return await original(domain, service, data, **kwargs)

    with (
        patch.object(type(hass.services), "async_call", side_effect=crash),
        pytest.raises(SimulatedCrash),
    ):
        await controller.start(manager, 60)
    assert (await controller.store.async_load())["journal"]["phase"] == "prepared"
    await hass.async_stop(force=True)
    restarted = await simulator_factory()
    recovered = LabController(restarted)
    await recovered.load()
    await recovered.monitor(restart=True)
    assert restarted.states.get(CLIMATE).state == "heat"
    assert restarted.states.get(CLIMATE).attributes["temperature"] == 24
    assert recovered.journal is None


async def test_restore_failures_bounded_and_journal_retained(lab):
    hass, manager, controller, _ = lab
    await controller.start(manager, 60)
    original = hass.services.async_call
    attempts = []

    async def fail(domain, service, data, **kwargs):
        if data.get("hvac_mode") == "heat":
            attempts.append(data)
            raise HomeAssistantError("Injected restore failure")
        return await original(domain, service, data, **kwargs)

    with patch.object(type(hass.services), "async_call", side_effect=fail):
        await controller.stop(manager.entry.entry_id)
        for _ in range(MAX_RETRIES + 2):
            await controller.monitor()
    assert len(attempts) == MAX_RETRIES
    assert controller.journal["phase"] == "restore_failed"
    assert (await controller.store.async_load())["journal"]["attempts"] == MAX_RETRIES
    assert hass.states.get(CLIMATE).state == "off"
    await controller.stop(manager.entry.entry_id)
    assert hass.states.get(CLIMATE).state == "heat"
    assert controller.journal is None


async def test_unavailable_climate_defers_restore(lab):
    hass, manager, controller, _ = lab
    await controller.start(manager, 60)
    attrs = dict(hass.states.get(CLIMATE).attributes)
    hass.states.async_set(CLIMATE, "unavailable", attrs)
    await controller.stop(manager.entry.entry_id)
    assert controller.journal["phase"] == "waiting_for_entity"
    assert (await controller.store.async_load())["journal"] is not None
    await hass.services.async_call("climate", "turn_off", {"entity_id": CLIMATE}, blocking=True)
    await controller.monitor()
    assert hass.states.get(CLIMATE).state == "heat"
    assert controller.journal is None


@pytest.mark.parametrize("override", ["target", "mode"])
async def test_manual_takeover_is_preserved(lab, override):
    hass, manager, controller, _ = lab
    await controller.start(manager, 60)
    if override == "target":
        await hass.services.async_call(
            "climate", "set_temperature", {"entity_id": CLIMATE, "temperature": 25}, blocking=True
        )
    else:
        await hass.services.async_call("climate", "turn_on", {"entity_id": CLIMATE}, blocking=True)
    with patch.object(type(hass.services), "async_call") as calls:
        await controller.monitor()
        calls.assert_not_called()
    assert controller.last_run["outcome"] == "external_override"
    assert hass.states.get(CLIMATE).attributes["temperature"] == (
        25 if override == "target" else 24
    )


async def test_entity_id_without_fake_registry_identity_is_rejected(lab):
    hass, manager, controller, _ = lab
    attrs = dict(hass.states.get(CLIMATE).attributes)
    entity_registry.async_get(hass).async_remove(CLIMATE)
    hass.states.async_set(CLIMATE, "heat", attrs)
    # An entity with the expected name is insufficient to authorize control.
    with pytest.raises(ServiceValidationError, match="development"):
        await controller.start(manager, 5)
    assert hass.data["fake_house"].house.mode == "heat"
    assert controller.journal is None


async def test_real_guard_sensor_is_rejected(lab):
    hass, manager, controller, _ = lab
    entity_registry.async_get(hass).async_get_or_create("sensor", "real_device", "live-temperature")
    manager.entry.data["rooms"][0]["sensors"] = ["sensor.live_temperature"]
    with pytest.raises(ServiceValidationError, match="fake-house"):
        await controller.start(manager, 5)
    assert hass.states.get(CLIMATE).state == "heat"


async def test_guard_events_restore_without_manual_monitor(lab):
    hass, manager, controller, _ = lab
    await controller.async_setup()
    await controller.start(manager, 60)
    hass.states.async_set("sensor.fake_house_bathroom", "unavailable")
    await hass.async_block_till_done()
    assert hass.states.get(CLIMATE).state == "heat"
    assert controller.last_run["outcome"] == "restored"


async def test_repeated_manual_off_releases_control(lab):
    hass, manager, controller, _ = lab
    await controller.async_setup()
    await controller.start(manager, 60)
    await hass.services.async_call("climate", "turn_off", {"entity_id": CLIMATE}, blocking=True)
    await hass.async_block_till_done()
    assert controller.journal is None
    assert controller.last_run["outcome"] == "external_override"
    assert hass.states.get(CLIMATE).state == "off"


async def test_simulator_rename_restores_by_registry_identity(lab):
    hass, manager, controller, _ = lab
    await controller.start(manager, 60)
    renamed = "climate.arbitrary_lab_name"
    entity_registry.async_get(hass).async_update_entity(CLIMATE, new_entity_id=renamed)
    await hass.async_block_till_done()
    await controller.stop(manager.entry.entry_id)
    assert hass.states.get(renamed).state == "heat"
    assert controller.journal is None


async def test_missing_target_readback_retains_journal(lab):
    hass, manager, controller, _ = lab
    await controller.start(manager, 60)
    attrs = dict(hass.states.get(CLIMATE).attributes)
    attrs.pop("temperature")
    hass.states.async_set(CLIMATE, "off", attrs)
    await controller.stop(manager.entry.entry_id)
    assert controller.journal["phase"] == "waiting_for_entity"
    assert (await controller.store.async_load())["journal"] is not None
    await hass.services.async_call("climate", "turn_off", {"entity_id": CLIMATE}, blocking=True)
    await controller.monitor()
    assert hass.states.get(CLIMATE).state == "heat"
    assert controller.journal is None


async def test_restore_requires_actual_readback(lab):
    hass, manager, controller, _ = lab
    await controller.start(manager, 60)
    # A successful service response alone does not prove the device changed.
    with patch.object(type(hass.services), "async_call"):
        await controller.stop(manager.entry.entry_id)
    assert controller.journal["phase"] == "restore_failed"
    assert hass.states.get(CLIMATE).state == "off"
    assert (await controller.store.async_load())["journal"] is not None


async def test_crash_after_restore_command_before_clear(lab, simulator_factory):
    hass, manager, controller, _ = lab
    await controller.start(manager, 60)
    original = hass.services.async_call

    class SimulatedCrash(BaseException):
        pass

    async def crash(domain, service, data, **kwargs):
        result = await original(domain, service, data, **kwargs)
        if data.get("hvac_mode") == "heat":
            raise SimulatedCrash
        return result

    with (
        patch.object(type(hass.services), "async_call", side_effect=crash),
        pytest.raises(SimulatedCrash),
    ):
        await controller.stop(manager.entry.entry_id)
    assert (await controller.store.async_load())["journal"]["phase"] == "restoring"
    await hass.async_stop(force=True)
    restarted = await simulator_factory()
    recovered = LabController(restarted)
    await recovered.load()
    with patch.object(type(restarted.services), "async_call") as calls:
        await recovered.monitor(restart=True)
        calls.assert_not_called()
    assert recovered.journal is None
    assert restarted.states.get(CLIMATE).state == "heat"
    assert restarted.states.get(CLIMATE).attributes["temperature"] == 24


async def test_shutdown_cleans_listeners_on_event_loop(lab, caplog):
    hass, _, controller, _ = lab
    await controller.async_setup()
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    await hass.async_stop(force=True)
    assert controller.cancel == []
    assert "Unable to remove unknown job listener" not in caplog.text
