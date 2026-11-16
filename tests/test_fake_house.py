"""Verify the simulator, not the as-yet unimplemented Phase 2 controller."""

from dataclasses import asdict

import pytest
from homeassistant import loader
from homeassistant.helpers import service

from custom_components.thermal_planner.analysis import replay
from dev.custom_components.fake_house import Simulator


def test_simulated_cooldown_replay(simulated_cooldown):
    f = simulated_cooldown
    result = replay(f["config"], f["history"], f["start"], f["end"])
    window = result["windows"][0]
    assert window["rooms"]["radiator"]["slope_c_per_h"] == pytest.approx(-1)
    assert window["rooms"]["ufh"]["slope_c_per_h"] == pytest.approx(-0.25)
    assert window["floors"]["Ground"]["slope_c_per_h"] == pytest.approx(-0.75)
    assert window["rooms"]["bathroom"]["coverage"] == 1
    assert result["limiting_room"] == "bathroom"
    assert result["rooms"]["bathroom"]["comfort_breached"]


def test_cooldown_and_recovery(fake_house):
    fake_house.mode = "off"
    fake_house.step(60)
    assert (fake_house.radiator, fake_house.ufh, fake_house.bathroom) == (21, 21.75, 19.5)
    fake_house.mode = "heat"
    fake_house.step(60)
    assert (fake_house.radiator, fake_house.ufh, fake_house.bathroom) == (23, 22.25, 21.5)
    assert fake_house.elapsed_minutes == 120


@pytest.mark.parametrize("minutes", [0, -1, float("nan"), float("inf"), 1441])
def test_invalid_step_is_atomic(fake_house, minutes):
    before = asdict(fake_house)
    with pytest.raises(ValueError):
        fake_house.step(minutes)
    assert asdict(fake_house) == before


async def test_native_climate_services_and_persisted_off(simulator_factory):
    hass = await simulator_factory()
    climate = "climate.fake_house_heat_pump"
    assert hass.states.get(climate).state == "heat"
    integration = await loader.async_get_integration(hass, "fake_house")
    descriptions = await hass.async_add_executor_job(service._load_services_file, integration)
    assert descriptions["step"]["fields"]["minutes"]["required"]
    assert "reset" in descriptions
    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": climate, "temperature": 24}, blocking=True
    )
    await hass.services.async_call("climate", "turn_off", {"entity_id": climate}, blocking=True)
    await hass.services.async_call("fake_house", "step", {"minutes": 60}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(climate).state == "off"
    assert hass.states.get("sensor.fake_house_radiator").state == "21.0"
    assert hass.states.get("sensor.fake_house_ufh").state == "21.75"
    # A fresh Store reader gets OFF, not a default-to-HEAT that could mask a
    # planner restart bug. This does not claim controller restart restoration.
    saved = await Simulator(hass).store.async_load()
    assert saved["mode"] == "off"
    assert saved["target"] == 24
    await hass.services.async_call("climate", "turn_on", {"entity_id": climate}, blocking=True)
    await hass.services.async_call("fake_house", "step", {"minutes": 60}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(climate).state == "heat"
    assert hass.states.get(climate).attributes["temperature"] == 24
    assert hass.states.get("sensor.fake_house_radiator").state == "23.0"
    await hass.services.async_call("fake_house", "reset", {}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(climate).attributes["temperature"] == 23


async def test_simulator_restart_preserves_device_state(simulator_factory):
    hass = await simulator_factory()
    climate = "climate.fake_house_heat_pump"
    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": climate, "temperature": 24}, blocking=True
    )
    await hass.services.async_call("climate", "turn_off", {"entity_id": climate}, blocking=True)
    await hass.services.async_call("fake_house", "step", {"minutes": 60}, blocking=True)
    await hass.async_stop(force=True)
    restarted = await simulator_factory()
    assert restarted.states.get(climate).state == "off"
    assert restarted.states.get(climate).attributes["temperature"] == 24
    assert restarted.states.get("sensor.fake_house_radiator").state == "21.0"
    await restarted.services.async_call("climate", "turn_on", {"entity_id": climate}, blocking=True)
    await restarted.async_block_till_done()
    assert restarted.states.get(climate).state == "heat"
