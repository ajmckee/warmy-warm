"""Historical assumptions, real outages and guided measurement setup."""

from copy import deepcopy
from datetime import timedelta

import pytest

from custom_components.thermal_planner.analysis import replay, timestamp, to_csv
from custom_components.thermal_planner.source import HeatSource


def one_room(fixture):
    config = deepcopy(fixture["config"])
    config["rooms"] = [dict(config["rooms"][0], sensors=["sensor.steady"], weights=[1])]
    config["history_policy"] = "until_changed"
    return config


def test_constant_pre_window_state_and_true_outage(fixture):
    config = one_room(fixture)
    start = timestamp(fixture["start"])
    history = {
        "sensor.steady": [
            {"timestamp": (start - timedelta(hours=8)).isoformat(), "value": 21, "unit": "°C"},
            {
                "timestamp": (start + timedelta(minutes=2)).isoformat(),
                "value": "unavailable",
                "unit": "°C",
            },
            {"timestamp": (start + timedelta(minutes=3)).isoformat(), "value": 21, "unit": "°C"},
        ]
    }
    result = replay(config, history, start, start + timedelta(hours=1))
    room = result["windows"][0]["rooms"][config["rooms"][0]["name"]]
    assert result["traces"]["rooms"][config["rooms"][0]["name"]][0] == 21
    assert room["coverage"] < 1 and room["delta_c"] is None
    assert result["unavailability_events"][0]["state"] == "unavailable"
    assert None in result["traces"]["rooms"][config["rooms"][0]["name"]]


def test_recorder_seed_unknown_age_only_accepted_for_carry(fixture):
    config = one_room(fixture)
    start = timestamp(fixture["start"])
    history = {
        "sensor.steady": [
            {
                "timestamp": (start - timedelta(minutes=15)).isoformat(),
                "value": 21,
                "unit": "°C",
                "original_timestamp_unknown": True,
            }
        ]
    }
    result = replay(config, history, start, start + timedelta(hours=1))
    assert result["windows"][0]["rooms"][config["rooms"][0]["name"]]["coverage"] == 1
    config["history_policy"] = "max_age"
    result = replay(config, history, start, start + timedelta(hours=1))
    assert result["windows"][0]["rooms"][config["rooms"][0]["name"]]["coverage"] == 0


def test_assumed_outside_is_run_scoped_and_exported(fixture):
    config = deepcopy(fixture["config"])
    config["assumed_outdoor_c"] = 5
    result = replay(config, fixture["history"], fixture["start"], fixture["end"])
    assert result["outdoor_source"] == "assumed_constant"
    assert set(result["traces"]["outside"]) == {5}
    assert "Assumed constant outdoor temperature: 5°C" in to_csv(result)
    assert "assumed_outdoor_c" not in fixture["config"]
    config["assumed_outdoor_c"] = float("nan")
    with pytest.raises(ValueError, match="assumed outdoor"):
        replay(config, fixture["history"], fixture["start"], fixture["end"])


async def test_live_repeated_reading_uses_last_reported(simulator_factory, simulated_cooldown):
    hass = await simulator_factory()
    config = deepcopy(simulated_cooldown["config"])
    config["max_age_minutes"] = 1
    entity = config["rooms"][0]["sensors"][0]
    state = hass.states.get(entity)
    # HA can report the same value without changing last_updated.
    state.last_updated -= timedelta(hours=1)
    assert HeatSource(hass, config, lambda: state.last_reported).temp(entity) is not None
    assert (
        HeatSource(hass, config, lambda: state.last_reported + timedelta(minutes=2)).temp(entity)
        is None
    )
