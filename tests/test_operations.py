"""Configured-source campaigns, observations, commissioning and direct flow safety."""

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.exceptions import ServiceValidationError, Unauthorized
from homeassistant.util import dt as dt_util

from custom_components.thermal_planner.analysis.demo import demo_episodes
from custom_components.thermal_planner.analysis.planning import local_instant
from custom_components.thermal_planner.config_flow import ThermalPlannerOptionsFlow
from custom_components.thermal_planner.const import DOMAIN
from custom_components.thermal_planner.control import CONTROL_KEY, SourceController
from custom_components.thermal_planner.integration import ReplayManager
from custom_components.thermal_planner.operations import OperationsEngine
from custom_components.thermal_planner.panel import save_measurement_setup, ws_operations
from custom_components.thermal_planner.readiness import measurement_readiness

CLIMATE = "climate.fake_house_heat_pump"
FLOW = "number.fake_house_flow_target"


@pytest.fixture
async def operations(simulator_factory, simulated_cooldown):
    hass = await simulator_factory()
    config = deepcopy(simulated_cooldown["config"])
    config["max_age_minutes"] = 1440
    config["weather"] = "weather.fake_house_weather"
    config["heat_source"] = {
        "name": "One shared pump",
        "climate": CLIMATE,
        "zones": [{"id": "one", "name": "Shared zone"}],
        "pause_limit_minutes": 60,
        "dwell_minutes": 1,
        "recovery_minutes": 120,
        "space_heating": "binary_sensor.fake_house_space_heating",
        "operating_mode": "sensor.fake_house_operating_mode",
        "dhw_states": ["dhw"],
        "defrost_states": ["defrost"],
        "flow_readback": FLOW,
        "flow_setter": FLOW,
        "flow_min": 30,
        "flow_max": 50,
        "flow_max_step": 2,
        "direct_mode_confirmed": True,
    }
    config["energy"] = {
        "electrical": {
            "entity": "sensor.fake_house_electrical_power",
            "kind": "power",
            "scope": "space_heating",
        },
        "thermal": {
            "entity": "sensor.fake_house_thermal_power",
            "kind": "power",
            "scope": "space_heating",
        },
    }
    for room in config["rooms"]:
        room["zone"] = "one"
    entry = SimpleNamespace(
        entry_id="operations-entry", title="Thermal Planner", data=config, options={}
    )
    manager = ReplayManager(hass, entry)
    hass.data[DOMAIN] = {entry.entry_id: manager}
    clock = [dt_util.utcnow()]
    controller = SourceController(hass, now=lambda: clock[0])
    hass.data[CONTROL_KEY] = controller
    await controller.load()
    engine = manager.engine = OperationsEngine(hass, manager, now=lambda: clock[0])
    await engine.load()
    yield hass, manager, controller, engine, clock
    engine.close()
    controller.close()


async def test_native_source_and_zone_configuration_without_commands(operations):
    hass, manager, _, _, _ = operations
    flow = ThermalPlannerOptionsFlow(manager.entry)
    flow.hass = hass
    flow.flow_id = "options"
    form = await flow.async_step_heat_source()
    source = manager.config["heat_source"]
    fields = {
        key: value for key, value in source.items() if key not in ("name", "climate", "zones")
    }
    fields["dhw_states"], fields["defrost_states"] = "dhw", "defrost"
    values = form["data_schema"](
        {"name": "One pump, mixed rooms", "climate": CLIMATE, "advanced": fields}
    )
    with patch.object(type(hass.services), "async_call") as calls:
        assert (await flow.async_step_heat_source(values))["step_id"] == "rooms"
        calls.assert_not_called()
    await flow.async_step_zone({"name": "Second distribution zone"})
    zones = flow.settings["heat_source"]["zones"]
    assert len(zones) == 2
    form = await flow.async_step_room()
    assert "zone" in {str(key) for key in form["data_schema"].schema}
    assert (await flow.async_step_remove_zone({"zone": "one"}))["errors"]["base"] == "zone_in_use"
    await flow.async_step_remove_zone({"zone": zones[1]["id"]})
    assert len(flow.settings["heat_source"]["zones"]) == 1


async def test_one_zone_still_has_radiator_and_ufh(operations):
    _, manager, _, _, _ = operations
    assert {room["zone"] for room in manager.config["rooms"]} == {"one"}
    assert {room["emitter"] for room in manager.config["rooms"]} == {"radiator", "underfloor"}


async def test_campaign_claim_is_persisted_before_start_and_not_repeated(operations):
    hass, manager, _, engine, clock = operations
    due = clock[0] + timedelta(minutes=1)
    local = due.astimezone(ZoneInfo(hass.config.time_zone))
    manager.entry.data["campaign"] = {
        "start_time": local.strftime("%H:%M:%S"),
        "end_date": (local.date() + timedelta(days=3)).isoformat(),
        "nights": 2,
        "pause_minutes": 10,
    }
    await engine.start_campaign()
    clock[0] = due + timedelta(seconds=1)

    async def start(*args, **kwargs):
        saved = await engine.store.async_load()
        assert saved["campaign"]["claimed_date"] == local.date().isoformat()
        assert saved["campaign"]["next_date"] > local.date().isoformat()

    with patch.object(engine, "run_once", AsyncMock(side_effect=start)) as run:
        await engine.campaign_tick()
        await engine.campaign_tick()
        assert run.await_count == 1
    reloaded = OperationsEngine(hass, manager, now=lambda: clock[0])
    await reloaded.load()
    with patch.object(reloaded, "run_once", AsyncMock()) as run:
        await reloaded.campaign_tick()
        run.assert_not_awaited()


@pytest.mark.parametrize("date", ["2026-03-29", "2026-10-25"])
async def test_campaign_dst_skips_with_reason(operations, date):
    _, manager, _, engine, clock = operations
    clock[0] = local_instant(date + "T00:30:00", "Europe/Warsaw")
    manager.entry.data["campaign"] = {
        "start_time": "02:30:00",
        "end_date": date,
        "nights": 1,
        "pause_minutes": 5,
    }
    await engine.start_campaign()
    with patch.object(engine, "run_once", AsyncMock()) as run:
        await engine.campaign_tick()
        run.assert_not_awaited()
    assert "DST" in engine.campaign["skips"][0]["reason"]


async def test_campaign_missed_busy_and_guard_failures_record_reasons(operations):
    hass, manager, _, engine, clock = operations
    due = clock[0] + timedelta(minutes=1)
    local = due.astimezone(ZoneInfo(hass.config.time_zone))
    manager.entry.data["campaign"] = {
        "start_time": local.strftime("%H:%M:%S"),
        "end_date": (local.date() + timedelta(days=3)).isoformat(),
        "nights": 2,
        "pause_minutes": 10,
    }
    await engine.start_campaign()
    clock[0] = due + timedelta(minutes=10)
    with patch.object(engine, "run_once", AsyncMock()) as run:
        await engine.campaign_tick()
        run.assert_not_awaited()
    assert "missed" in engine.campaign["skips"][-1]["reason"]
    engine.campaign["next_date"] = local.date().isoformat()
    clock[0] = due
    with patch.object(
        engine, "run_once", AsyncMock(side_effect=ServiceValidationError("Guard unavailable"))
    ):
        await engine.campaign_tick()
    assert "Guard" in engine.campaign["skips"][-1]["reason"]


async def test_record_recovery_and_storage_reload(operations):
    hass, manager, controller, engine, clock = operations
    await engine.run_once(5)
    clock[0] += timedelta(minutes=2)
    await hass.services.async_call("fake_house", "step", {"minutes": 30}, blocking=True)
    await controller.monitor()
    await controller.stop(manager.entry.entry_id)
    await engine.ingest_completed_control()
    assert engine.observation and not controller.journal
    with pytest.raises(ServiceValidationError, match="Recovery"):
        await engine.run_once(5)
    clock[0] += timedelta(minutes=5)
    await hass.services.async_call("fake_house", "step", {"minutes": 60}, blocking=True)
    await engine.observe_tick()
    assert engine.observation is None
    assert engine.runs[-1]["recovery"]["complete"]
    assert engine.runs[-1]["energy"]["channels"]["electrical"]["kwh"] is not None
    reloaded = OperationsEngine(hass, manager, now=lambda: clock[0])
    await reloaded.load()
    assert reloaded.runs[-1]["id"] == engine.runs[-1]["id"]
    assert "points" in reloaded.runs[-1]


async def test_direct_flow_trial_restores_original_flow_and_mode(operations):
    hass, manager, controller, _, clock = operations
    await controller.start(manager, 5, trial_flow=38)
    assert hass.states.get(FLOW).state == "38.0"
    assert hass.states.get(CLIMATE).state == "off"
    clock[0] += timedelta(minutes=6)
    await controller.monitor()
    assert controller.journal["phase"] == "recovering"
    assert hass.states.get(CLIMATE).state == "heat"
    await controller.stop(manager.entry.entry_id)
    assert hass.states.get(FLOW).state == "40.0"
    assert hass.states.get(CLIMATE).state == "heat"
    assert controller.journal is None


async def test_flow_only_apply_never_issues_off(operations):
    hass, manager, controller, _, clock = operations
    original = hass.services.async_call
    calls = []

    async def record(domain, service, data, **kwargs):
        calls.append((domain, service, data))
        return await original(domain, service, data, **kwargs)

    with patch.object(type(hass.services), "async_call", side_effect=record):
        await controller.start(manager, 5, trial_flow=38, flow_only=True)
    assert not any(data.get("hvac_mode") == "off" for _, _, data in calls)
    clock[0] += timedelta(minutes=6)
    await controller.monitor()
    assert hass.states.get(FLOW).state == "40.0"
    assert controller.journal is None


async def test_flow_limit_and_manual_override(operations):
    hass, manager, controller, _, _ = operations
    with pytest.raises(ServiceValidationError, match="bounds"):
        await controller.start(manager, 5, trial_flow=35)
    assert hass.states.get(FLOW).state == "40.0"
    await controller.async_setup()
    await controller.start(manager, 5, trial_flow=38)
    await hass.services.async_call(
        "number", "set_value", {"entity_id": FLOW, "value": 39}, blocking=True
    )
    await hass.async_block_till_done()
    assert controller.journal is None
    assert controller.last_run["outcome"] == "external_override"
    assert hass.states.get(FLOW).state == "39.0"


async def test_flow_readback_loss_resumes_heat_but_retains_flow_journal(operations):
    hass, manager, controller, _, _ = operations
    await controller.start(manager, 5, trial_flow=38)
    attrs = dict(hass.states.get(FLOW).attributes)
    hass.states.async_set(FLOW, "unavailable", attrs)
    await controller.monitor()
    assert hass.states.get(CLIMATE).state == "heat"
    assert controller.journal and controller.journal["phase"] == "waiting_for_entity"
    await hass.services.async_call(
        "number", "set_value", {"entity_id": FLOW, "value": 38}, blocking=True
    )
    await controller.monitor()
    assert hass.states.get(FLOW).state == "40.0"
    assert controller.journal is None


async def test_flow_restart_restores_original_before_clearing(operations, simulator_factory):
    hass, manager, controller, _, _ = operations
    await controller.start(manager, 5, trial_flow=38)
    await hass.async_stop(force=True)
    restarted = await simulator_factory()
    recovered = SourceController(restarted)
    await recovered.load()
    await recovered.monitor(restart=True)
    assert restarted.states.get(CLIMATE).state == "heat"
    assert restarted.states.get(FLOW).state == "40.0"
    assert recovered.journal is None


async def test_uncommissioned_real_policy_needs_explicit_success(operations):
    hass, manager, controller, engine, _ = operations
    with patch.object(controller, "simulated", return_value=False):
        assert not controller.allowed(manager.config)
        with pytest.raises(ServiceValidationError, match="commission"):
            await controller.start(manager, 1)
        with pytest.raises(ServiceValidationError):
            await engine.commission(False)
        manager.entry.data["heat_source"]["commissioning_allowed"] = True
        await engine.commission(True)
        await controller.stop(manager.entry.entry_id)
        assert controller.allowed(manager.config)
        assert hass.states.get(CLIMATE).state == "heat"


async def test_native_hourly_weather_fit_and_apply_once(operations):
    hass, manager, controller, engine, _ = operations
    engine.runs = demo_episodes(manager.config, 40)
    await engine.fit()
    plan = await engine.replan()
    assert engine.forecast["available"]
    assert plan["status"] == "ready" and plan["automatic_eligible"], (
        plan["reasons"],
        engine.models.get("reasons"),
        engine.models.get("flows"),
    )
    await engine.apply(plan["id"])
    await engine.planner_tick()
    assert controller.journal and controller.journal["purpose"] == "planner"
    assert hass.states.get(CLIMATE).state == "off"
    await engine.stop()
    assert hass.states.get(CLIMATE).state == "heat"


async def test_queued_apply_rechecks_fresh_temperature_and_external_commands(operations):
    hass, manager, controller, engine, _ = operations
    engine.runs = demo_episodes(manager.config, 40)
    await engine.fit()
    plan = await engine.replan()
    await engine.apply(plan["id"])
    hass.states.async_set("sensor.fake_house_radiator", 18.1, {"unit_of_measurement": "°C"})
    await engine.planner_tick()
    assert controller.journal is None
    assert engine.mode == "advisory" and "comfort" in engine.events[-1]["message"]
    assert hass.states.get(CLIMATE).state == "heat"
    await controller.async_setup()
    engine.mode = "auto"
    engine.pending = {"plan_id": "anything"}
    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": CLIMATE, "temperature": 24}, blocking=True
    )
    await hass.async_block_till_done()
    assert engine.mode == "advisory" and engine.pending is None


async def test_passive_observation_survives_reload_without_commands(operations):
    hass, manager, controller, engine, clock = operations
    with patch.object(type(hass.services), "async_call") as calls:
        await engine.observe_normal(30)
        resumed = OperationsEngine(hass, manager, now=lambda: clock[0])
        await resumed.load()
        assert resumed.baseline["id"] == engine.baseline["id"]
        clock[0] += timedelta(minutes=31)
        await resumed.baseline_tick()
        assert resumed.baseline is None
        assert resumed.runs[-1]["outcome"] == "observed"
        assert not resumed.runs[-1]["learning_eligible"]  # Offline gap is not fresh data.
        assert controller.journal is None
        calls.assert_not_called()


async def test_offline_manual_change_cancels_automatic_plan(operations):
    hass, _, _, engine, _ = operations
    engine.mode = "auto"
    engine.baseline_settings = engine.source_settings()
    await engine.save()
    state = hass.states.get(CLIMATE)
    hass.states.async_set(CLIMATE, "off", dict(state.attributes))
    await engine.load()
    await engine.planner_tick()
    assert engine.mode == "advisory"
    assert engine.pending is None


async def test_lost_forecast_restores_active_planner_pause(operations):
    hass, manager, controller, engine, _ = operations
    engine.runs = demo_episodes(manager.config, 40)
    await engine.fit()
    plan = await engine.replan()
    await engine.apply(plan["id"])
    await engine.planner_tick()
    assert controller.journal and hass.states.get(CLIMATE).state == "off"
    engine.forecast = {"available": False}
    hass.states.async_set(manager.config["weather"], "unavailable")
    await engine.planner_tick()
    assert hass.states.get(CLIMATE).state == "heat"
    assert controller.journal is None
    assert engine.mode == "advisory"
    assert "Forecast lost" in engine.events[-1]["message"]


async def test_setup_saves_only_configuration_without_heat_commands(operations):
    hass, manager, controller, engine, clock = operations
    values = {
        "climate": "climate.fake_house_heat_pump",
        "outdoor": "sensor.fake_house_outdoor",
        "max_age_minutes": 60,
        "commissioning_allowed": False,
    }
    with (
        patch.object(hass.config_entries, "async_update_entry") as update,
        patch.object(type(hass.services), "async_call") as commands,
    ):
        await save_measurement_setup(hass, manager, values)
        saved = update.call_args.kwargs["options"]
        assert saved["heat_source"]["climate"] == values["climate"]
        assert saved["max_age_minutes"] == 60
        commands.assert_not_called()
    engine.mode = "auto"
    with pytest.raises(ServiceValidationError, match="Stop"):
        await save_measurement_setup(hass, manager, values)


async def test_readiness_explains_missing_control_and_dhw(operations):
    hass, manager, controller, engine, clock = operations
    manager.entry.data["heat_source"].pop("climate")
    checks = measurement_readiness(hass, manager.config, controller, engine.now)
    assert not checks["can_pause"] and checks["can_observe"]
    assert any(c["action"] == "setup" for c in checks["checks"])
    manager.entry.data["heat_source"]["operating_mode"] = "sensor.custom_mode"
    hass.states.async_set("sensor.custom_mode", "DHW")
    checks = measurement_readiness(hass, manager.config, controller, engine.now)
    assert any("DHW" in c["message"] and not c["ready"] for c in checks["checks"])


async def test_setup_requires_admin(operations):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    hass, manager, _, _, _ = operations
    connection = SimpleNamespace(
        user=SimpleNamespace(
            is_admin=False, permissions=SimpleNamespace(check_entity=lambda *args: True)
        ),
        send_result=MagicMock(),
    )
    with pytest.raises(Unauthorized):
        await ws_operations.__wrapped__(
            hass,
            connection,
            {"id": 1, "entry_id": manager.entry.entry_id, "action": "setup", "setup": {}},
        )


@pytest.mark.parametrize("scheduled", ["auto", "campaign"])
async def test_manual_cooldown_cannot_compete_with_schedule(operations, scheduled):
    hass, _, controller, engine, _ = operations
    if scheduled == "auto":
        engine.mode = "auto"
    else:
        engine.campaign = {"active": True}
    with patch.object(type(hass.services), "async_call") as commands:
        with pytest.raises(ServiceValidationError, match="Stop scheduled heating"):
            await engine.run_once(5)
        commands.assert_not_called()
    assert controller.journal is None
