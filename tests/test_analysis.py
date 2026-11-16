import csv
import io

import pytest

from custom_components.thermal_planner.analysis import normalize, reading, replay, timestamp, to_csv


def run(f):
    return replay(f["config"], f["history"], f["start"], f["end"])


def test_exact_fixture(fixture):
    r = run(fixture)
    expected = fixture["expected"]
    for w in r["windows"]:
        assert w["rooms"]["Study"]["slope_c_per_h"] == expected["study_slope_c_per_h"]
        assert w["rooms"]["Kitchen"]["slope_c_per_h"] == expected["kitchen_slope_c_per_h"]
        assert w["floors"]["Ground"]["slope_c_per_h"] == expected["ground_slope_c_per_h"]
        assert w["rooms"]["Study"]["coverage"] == 1
        assert w["floors"]["Ground"]["members"] == ["Study", "Kitchen"]
    assert r["windows"][0]["rooms"]["Study"]["start_c"] == expected["study_start_c"]
    assert r["windows"][0]["floors"]["Ground"]["start_c"] == expected["ground_start_c"]
    assert r["rooms"]["Bathroom"]["thresholds"]["0.5"] == expected["bathroom_drop_0.5_hours"]
    assert r["rooms"]["Bathroom"]["thresholds"]["1.0"] == expected["bathroom_drop_1.0_hours"]
    assert r["limiting_room"] == expected["limiting_room"]
    assert r["rooms"]["Bathroom"]["comfort_breached"]
    assert r["windows"][0]["rooms"]["Study"]["normalized_c_per_h_10k"] == pytest.approx(-10 / 13)
    rows = list(csv.DictReader(io.StringIO(to_csv(r))))
    assert len(rows) == 18
    assert {row["name"] for row in rows} == {
        "Study",
        "Kitchen",
        "Bathroom",
        "Bedroom",
        "Ground",
        "Upper",
    }


def test_missing_constituent_never_reweights(fixture):
    fixture["history"]["sensor.z17"] = fixture["history"]["sensor.z17"][:3]
    r = run(fixture)
    w = r["windows"][0]
    assert w["rooms"]["Study"]["delta_c"] is None
    assert w["rooms"]["Study"]["coverage"] < 1
    assert w["floors"]["Ground"]["slope_c_per_h"] is None
    assert w["rooms"]["Kitchen"]["slope_c_per_h"] == -0.25


def test_missing_outdoor_and_no_energy(fixture):
    fixture["history"].pop("sensor.ambient_73")
    r = run(fixture)
    row = r["windows"][0]["rooms"]["Study"]
    assert row["slope_c_per_h"] == -1
    assert row["normalized_c_per_h_10k"] is None
    assert row["mean_outside_c"] is None


def test_rooms_without_floors_keep_individual_results(fixture):
    for room in fixture["config"]["rooms"]:
        room.pop("floor")
        room.pop("circuit")
    result = run(fixture)
    assert all(window["floors"] == {} for window in result["windows"])
    assert result["windows"][0]["rooms"]["Study"]["slope_c_per_h"] == -1
    assert result["rooms"]["Bathroom"]["comfort_breached"]
    assert result["limiting_room"] == "Bathroom"


def test_unassigned_room_is_not_in_floor_average(fixture):
    fixture["config"]["rooms"][0].pop("floor")
    result = run(fixture)
    for window in result["windows"]:
        assert window["floors"]["Ground"]["members"] == ["Kitchen"]
        assert window["floors"]["Ground"]["slope_c_per_h"] == -0.25
        assert window["rooms"]["Study"]["slope_c_per_h"] == -1


@pytest.mark.parametrize("unit,value", [("°F", 68), ("K", 293.15), ("°C", 20)])
def test_units_and_invalid_states(unit, value):
    records = [
        {"timestamp": "2026-01-01T00:00:00Z", "value": value, "unit": unit},
        {"timestamp": "2026-01-01T00:10:00Z", "value": "unavailable", "unit": unit},
    ]
    series = normalize({"sensor.random": records})
    start = timestamp(records[0]["timestamp"]).timestamp()
    assert reading(series, "sensor.random", start, 60) == pytest.approx(20)
    assert reading(series, "sensor.random", start + 61, 60) is None
    assert reading(series, "sensor.random", start + 600, 900) is None


def test_weight_changes_and_snapshot(fixture):
    before = run(fixture)
    fixture["config"]["rooms"][0]["weights"] = [1, 0]
    after = run(fixture)
    assert before["configuration"]["rooms"][0]["weights"] == [1, 3]
    assert before["windows"][0]["rooms"]["Study"]["start_c"] == 23.5
    assert after["windows"][0]["rooms"]["Study"]["start_c"] == 22


@pytest.mark.parametrize("weights", [[0, 0], [-1, 2], [float("nan"), 1], [1]])
def test_invalid_weights(fixture, weights):
    fixture["config"]["rooms"][0]["weights"] = weights
    with pytest.raises(ValueError):
        run(fixture)


def test_no_threshold_interpolation_over_gap(fixture):
    fixture["history"]["sensor.x03"] = (
        fixture["history"]["sensor.x03"][:1] + fixture["history"]["sensor.x03"][12:]
    )
    fixture["config"]["max_age_minutes"] = 1
    r = run(fixture)
    assert r["rooms"]["Bathroom"]["thresholds"]["0.5"] is None
    assert r["rooms"]["Bathroom"]["thresholds"]["observed_hours"] == 4


def test_unobserved_threshold_and_bad_units(fixture):
    fixture["history"]["sensor.q42"] = [dict(x, value=22) for x in fixture["history"]["sensor.q42"]]
    r = run(fixture)
    assert r["rooms"]["Kitchen"]["thresholds"]["1.0"] is None
    assert r["rooms"]["Kitchen"]["thresholds"]["observed_hours"] == 4
    fixture["history"]["sensor.q42"] = [dict(x, unit="W") for x in fixture["history"]["sensor.q42"]]
    assert run(fixture)["windows"][0]["rooms"]["Kitchen"]["coverage"] == 0


def test_arbitrary_renamed_ids(fixture):
    fixture["history"]["sensor.renamed_883"] = fixture["history"].pop("sensor.a91")
    fixture["config"]["rooms"][0]["sensors"][0] = "sensor.renamed_883"
    assert run(fixture)["windows"][0]["rooms"]["Study"]["start_c"] == 23.5


def test_utc_offsets_required():
    with pytest.raises(ValueError):
        timestamp("2026-10-25T02:30:00")
    assert timestamp("2026-10-25T02:30:00+02:00") != timestamp("2026-10-25T02:30:00+01:00")


def test_recorded_weather_fallback_and_diagnostics(fixture):
    fixture["config"]["weather"] = "weather.unrelated"
    fixture["history"]["weather.unrelated"] = fixture["history"].pop("sensor.ambient_73")
    fixture["config"]["rooms"][0]["diagnostics"] = ["sensor.diagnostic"]
    fixture["history"]["sensor.diagnostic"] = [
        dict(p, value=99) for p in fixture["history"]["sensor.a91"]
    ]
    report = run(fixture)
    assert report["outdoor_source"] == "weather.unrelated"
    assert report["windows"][0]["rooms"]["Study"]["slope_c_per_h"] == -1
    assert report["traces"]["diagnostics"]["sensor.diagnostic"][0] == 99


def test_short_window_and_csv_label_escaping(fixture):
    fixture["end"] = "2026-01-01T00:10:00Z"
    fixture["config"]["rooms"][0]["name"] = "=untrusted"
    report = run(fixture)
    assert len(report["windows"]) == 1
    assert report["windows"][0]["rooms"]["=untrusted"]["coverage"] == 1
    assert "'=untrusted" in to_csv(report)
