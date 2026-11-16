"""Exact optional-channel checks and independently held-out synthetic physics."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.thermal_planner.analysis import replay
from custom_components.thermal_planner.analysis.application import action_safe
from custom_components.thermal_planner.analysis.comfort import comfort
from custom_components.thermal_planner.analysis.demo import demo_episodes
from custom_components.thermal_planner.analysis.learning import fit_models, heat_demand
from custom_components.thermal_planner.analysis.planning import (
    forecast_grid,
    local_instant,
    make_plan,
    target_at,
)
from custom_components.thermal_planner.analysis.signals import classify, dew_point, energy_summary
from custom_components.thermal_planner.configuration import signature, validate_settings


@pytest.fixture
def installation(fixture):
    config = deepcopy(fixture["config"])
    config["heat_source"] = {
        "name": "One source",
        "climate": "climate.arbitrary_name",
        "zones": [{"id": "one", "name": "Whole house"}],
        "pause_limit_minutes": 60,
        "dwell_minutes": 30,
        "flow_setter": "number.arbitrary_flow",
        "flow_readback": "sensor.flow_readback",
        "direct_mode_confirmed": True,
        "flow_min": 30,
        "flow_max": 50,
        "flow_max_step": 2,
    }
    config["energy"] = {
        "electrical": {"entity": "sensor.electric", "kind": "power", "scope": "space_heating"},
        "thermal": {"entity": "sensor.thermal", "kind": "power", "scope": "space_heating"},
    }
    for room in config["rooms"]:
        room["zone"] = "one"
    return config


def test_one_pump_one_zone_can_mix_emitters(installation):
    validate_settings(installation)
    assert {r["emitter"] for r in installation["rooms"]} >= {"radiator", "underfloor"}
    assert {r["zone"] for r in installation["rooms"]} == {"one"}
    installation["rooms"][0]["zone"] = "not_configured"
    with pytest.raises(ValueError, match="existing"):
        validate_settings(installation)


def test_power_energy_cop_and_scope(installation):
    points = [
        {
            "time": i * 300,
            "electrical": {"value": 2000, "unit": "W"},
            "thermal": {"value": 6, "unit": "kW"},
        }
        for i in range(13)
    ]
    result = energy_summary(points, installation["energy"])
    assert result["channels"]["electrical"]["kwh"] == pytest.approx(2)
    assert result["channels"]["thermal"]["kwh"] == pytest.approx(6)
    assert result["cop"] == pytest.approx(3)
    installation["energy"]["electrical"]["scope"] = "heat_pump"
    assert energy_summary(points, installation["energy"])["cop"] is None


def test_counter_resets_and_gaps_are_partial():
    definition = {
        "electrical": {"entity": "sensor.any", "kind": "counter", "scope": "space_heating"}
    }
    points = [
        {"time": i * 300, "electrical": {"value": value, "unit": "kWh"}}
        for i, value in enumerate([10, 10.5, 0.1, 0.2])
    ]
    channel = energy_summary(points, definition)["channels"]["electrical"]
    assert channel["kwh"] == pytest.approx(0.6)
    assert channel["coverage"] == pytest.approx(2 / 3)
    assert channel["resets"] == 1 and channel["partial"]
    points[-1]["time"] += 7200
    assert energy_summary(points, definition)["channels"]["electrical"]["coverage"] < 0.1


@pytest.mark.parametrize("humidity", [None, -1, 101, float("nan"), "unavailable"])
def test_bad_humidity_does_not_create_comfort(humidity):
    assert comfort(25, humidity)["pmv"] is None


def test_pmv_published_reference_and_assumptions():
    # Published pythermalcomfort ISO reference: 25/25 °C, 50% RH, 0.1 m/s,
    # 1.4 met, 0.5 clo => PMV 0.41, PPD 8.5%.
    value = comfort(25, 50, {"met": 1.4, "clo": 0.5, "air_speed": 0.1})
    assert value["pmv"] == pytest.approx(0.41, abs=0.01)
    assert value["ppd_percent"] == pytest.approx(8.5, abs=0.1)
    assert value["dew_point_c"] == pytest.approx(13.85, abs=0.02)
    assert "proxy" in value["assumptions"]["radiant_source"]
    assert (
        comfort(25, 50, {"met": 1.4, "clo": 0.5, "air_speed": 0.1}, radiant_c=28)["pmv"]
        > value["pmv"]
    )
    assert comfort(-235, 50)["pmv"] is None
    assert dew_point(-243.12, 50) is None


def test_stale_humidity_does_not_remove_temperature_report(fixture):
    config = deepcopy(fixture["config"])
    config["rooms"][0].update(humidity="sensor.rh", comfort_estimate=True)
    fixture["history"]["sensor.rh"] = [{"timestamp": fixture["start"], "value": 50, "unit": "%"}]
    report = replay(config, fixture["history"], fixture["start"], fixture["end"])
    assert report["windows"][0]["rooms"]["Study"]["coverage"] == 1
    assert report["rooms"]["Study"]["humidity"]["end_percent"] is None
    assert report["rooms"]["Study"]["comfort_estimate"]["pmv"] is None


def test_delivery_classification_does_not_infer_dhw_off():
    mapping = {"dhw_states": ["water"], "defrost_states": ["ice"]}
    assert classify("off", "off", "water", mapping) == "dhw"
    assert classify("heat", "off", "ice", mapping) == "defrost"
    assert classify("off") == "climate_off"
    assert classify("heat") == "delivery_unverified"


def test_model_holdout_and_ufh_memory(installation):
    episodes = demo_episodes(installation, 40)
    models = fit_models(installation, episodes, fitted_at="2026-02-01T00:00:00+00:00")
    group = models["flows"]["40"]
    for name, model in group["rooms"].items():
        assert model["validated"] and model["automatic_eligible"]
        assert model["validation"]["mae_c"] < 0.1
        assert model["validation"]["mae_c"] < model["validation"]["persistence_mae_c"]
        assert set(model["training_episode_ids"]).isdisjoint(model["holdout_episode_ids"])
        assert set(model["selection_episode_ids"]).isdisjoint(model["holdout_episode_ids"])
    assert group["rooms"]["Kitchen"]["tau_hours"] > 0
    assert group["rooms"]["Study"]["tau_hours"] == 0
    assert group["operating_kw"]["on"] == pytest.approx(2)
    duplicate = fit_models(installation, episodes + episodes)
    assert duplicate["flows"]["40"]["episode_count"] == 12
    degraded = deepcopy(episodes)
    for episode in degraded:
        for point in episode["points"]:
            point["heat"] = None
    retained = fit_models(installation, degraded, previous=models)
    assert retained["flows"]["40"]["rooms"]["Study"]["retained_previous"]


def test_sparse_missing_outdoor_and_changed_config_withhold(installation):
    episodes = demo_episodes(installation, 40)
    assert not fit_models(installation, episodes[:3])["flows"]
    for episode in episodes:
        for point in episode["points"]:
            point["outdoor"] = None
    assert not fit_models(installation, episodes)["flows"]["40"]["rooms"]
    changed = deepcopy(installation)
    changed["rooms"][0]["weights"] = [2, 1]
    assert signature(changed) != signature(installation)
    assert not fit_models(changed, episodes)["flows"]


def _forecast(now, missing=False):
    raw = [
        {"datetime": (now + timedelta(hours=i)).isoformat(), "temperature": 8}
        for i in range(25)
        if not missing or i != 4
    ]
    return forecast_grid(raw, "°C", now, 8)


def test_forecast_plan_and_fresh_application_guard(installation):
    now = datetime(2026, 2, 1, tzinfo=timezone.utc)
    models = fit_models(installation, demo_episodes(installation, 40), fitted_at=now.isoformat())
    current = {
        "rooms": {room["name"]: 22 for room in installation["rooms"]},
        "mode": "heat",
        "heat": 1,
        "flow_target": 40,
    }
    forecast = _forecast(now)
    plan = make_plan(installation, models, current, forecast, now, "Europe/Warsaw")
    assert plan["status"] == "ready" and plan["automatic_eligible"]
    assert plan["actions"][0]["kind"] == "pause"
    assert plan["heating_hours"] < plan["baseline_heating_hours"]
    assert action_safe(
        installation, models, current, forecast, plan["actions"][0], now, "Europe/Warsaw"
    )[0]
    cold = deepcopy(current)
    cold["rooms"]["Study"] = 18.1
    assert not action_safe(
        installation, models, cold, forecast, plan["actions"][0], now, "Europe/Warsaw"
    )[0]
    assert not _forecast(now, missing=True)["available"]
    assert (
        make_plan(installation, models, current, {"available": False}, now)["status"] == "withheld"
    )
    current["flow_target"] = 35
    assert make_plan(installation, models, current, forecast, now)["status"] == "withheld"


def test_forecast_units_and_unobserved_flow(installation):
    now = datetime(2026, 2, 1, tzinfo=timezone.utc)
    data = [
        {"datetime": (now + timedelta(hours=i)).isoformat(), "temperature": 50} for i in range(25)
    ]
    assert forecast_grid(data, "°F", now, 10)["outside"] == pytest.approx([10] * 289)
    models = fit_models(installation, demo_episodes(installation, 40))
    current = {
        "rooms": {r["name"]: 22 for r in installation["rooms"]},
        "mode": "heat",
        "heat": 1,
        "flow_target": 40,
    }
    assert make_plan(installation, models, current, _forecast(now), now)["flow_target"] == 40
    installation["heat_source"]["pause_limit_minutes"] = 1
    models = fit_models(installation, demo_episodes(installation, 40, 38))
    result = make_plan(installation, models, current, _forecast(now), now)
    assert result["flow_target"] == 38
    assert result["actions"][0]["kind"] == "flow" and len(result["actions"]) == 1
    assert result["min_margin_c"] >= 0  # Includes worst-case initial UFH memory uncertainty.


def test_dst_and_overnight_comfort_blocks(installation):
    for wall in ("2026-03-29T02:30:00", "2026-10-25T02:30:00"):
        with pytest.raises(ValueError, match="Ambiguous"):
            local_instant(wall, "Europe/Warsaw")
    room = installation["rooms"][0]
    installation["comfort_blocks"] = [
        {"room": room["name"], "days": [0], "start": "22:00", "end": "06:00", "target_c": 22}
    ]
    assert (
        target_at(
            installation, room, local_instant("2026-02-03T02:00", "Europe/Warsaw"), "Europe/Warsaw"
        )
        == 22
    )
    assert (
        target_at(
            installation, room, local_instant("2026-02-03T12:00", "Europe/Warsaw"), "Europe/Warsaw"
        )
        == room["comfort_min"]
    )


def test_whole_house_demand_requires_measured_stable_heat():
    windows = [
        {
            "stable": True,
            "coverage": 1,
            "space_heat_confirmed": True,
            "gap_k": gap,
            "thermal_w": gap * 100 + 200,
        }
        for gap in [4, 6, 8, 10, 12, 14]
    ]
    model = heat_demand(windows)
    assert model["w_per_k"] == pytest.approx(100)
    assert model["intercept_w"] == pytest.approx(200)
    assert "no room kWh" in model["scope"]
    windows[0]["space_heat_confirmed"] = False
    assert not heat_demand(windows)["available"]


def test_ufh_uncertainty_covers_unknown_initial_storage():
    from custom_components.thermal_planner.analysis.learning import predict, uncertainty

    model = {"loss_per_hour": 0.05, "heating_c_per_hour": 0.8, "tau_hours": 2, "uncertainty_c": 0.1}
    empty = predict(model, 22, [5] * 48, [1] * 48, initial_memory=0)
    warm = predict(model, 22, [5] * 48, [1] * 48, initial_memory=1)
    for step, (a, b) in enumerate(zip(empty, warm)):
        assert abs(a - b) <= uncertainty(model, step / 12)
