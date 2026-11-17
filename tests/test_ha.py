"""Tests use real HA flow, entity, storage and Recorder APIs; only I/O is substituted."""

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import ConfigEntries, ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import floor_registry, selector
from voluptuous_serialize import convert

from custom_components.thermal_planner.analysis import timestamp
from custom_components.thermal_planner.config_flow import (
    ThermalPlannerConfigFlow,
    ThermalPlannerOptionsFlow,
    local_timestamp,
)
from custom_components.thermal_planner.const import DOMAIN
from custom_components.thermal_planner.history import async_history
from custom_components.thermal_planner.integration import ReplayManager, ReportView, async_setup


@pytest.fixture
async def hass(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    hass.config.time_zone = "Europe/Warsaw"
    hass.config_entries = ConfigEntries(hass, {})
    await floor_registry.async_load(hass)
    yield hass
    await hass.async_stop()


def entry_for(data):
    return ConfigEntry(
        version=1,
        minor_version=1,
        domain=DOMAIN,
        title="Thermal Planner",
        data=data,
        source="user",
        options={},
        unique_id=None,
        discovery_keys={},
        subentries_data=[],
    )


def attach(flow, hass):
    flow.hass = hass
    flow.flow_id = "test-flow"
    flow.handler = DOMAIN
    flow.context = {"source": "user"}
    return flow


async def test_native_setup_options_and_units(hass, fixture):
    flow = attach(ThermalPlannerConfigFlow(), hass)
    result = await flow.async_step_user()
    assert result["step_id"] == "settings"
    assert isinstance(
        result["data_schema"].schema[
            next(k for k in result["data_schema"].schema if str(k) == "outdoor")
        ],
        selector.EntitySelector,
    )
    result = await flow.async_step_settings({"max_age_minutes": 15})
    assert result["step_id"] == "rooms"
    assert "room" in result["menu_options"] and "heat_source" in result["menu_options"]
    room = deepcopy(fixture["config"]["rooms"][0])
    room["relative_weights"] = ",".join(map(str, room.pop("weights")))
    for entity in room["sensors"]:
        hass.states.async_set(entity, 22, {"unit_of_measurement": "°C"})
    result = await flow.async_step_room(room)
    assert result["type"] == "menu"
    result = await flow.async_step_save()
    assert result["data"]["rooms"][0]["weights"] == [1, 3]
    entry = entry_for(result["data"])
    options = attach(ThermalPlannerOptionsFlow(entry), hass)
    assert (await options.async_step_init())["type"] == "menu"
    await options.async_step_edit_room({"room": "Study"})
    room["relative_weights"] = "1,0"
    await options.async_step_room(room)
    saved = await options.async_step_save()
    assert saved["data"]["rooms"][0]["weights"] == [1, 0]
    assert entry.data["rooms"][0]["weights"] == [1, 3]
    room["relative_weights"] = "-1,2"
    result = await options.async_step_room(room)
    assert result["errors"]["base"] == "invalid_room"
    assert (await options.async_step_remove_room({"room": "Study"}))["errors"][
        "base"
    ] == "last_room"


async def test_room_list_add_edit_remove_and_validation(hass):
    flow = attach(ThermalPlannerConfigFlow(), hass)
    await flow.async_step_user()
    result = await flow.async_step_settings({"max_age_minutes": 15})
    assert result["description_placeholders"]["room_count"] == "0"
    assert "save" not in result["menu_options"]
    for name, sensor in (("Living room", "sensor.living"), ("Kitchen", "sensor.kitchen")):
        hass.states.async_set(sensor, 21, {"unit_of_measurement": "°C"})
        form = await flow.async_step_room()
        # Exercise the actual nested schema/defaults sent by HA's frontend.
        if type(cv.PLATFORM_SCHEMA).__module__.startswith("probatio"):
            from probatio import to_field_list

            serialized = to_field_list(form["data_schema"], custom_serializer=cv.custom_serializer)
        else:
            serialized = convert(form["data_schema"], custom_serializer=cv.custom_serializer)
        advanced = next(field for field in serialized if field["name"] == "advanced")
        assert advanced["type"] == "expandable"
        assert advanced["expanded"] is False
        submitted = form["data_schema"]({"name": name, "sensors": [sensor], "advanced": {}})
        result = await flow.async_step_room(submitted)
        assert result["step_id"] == "rooms"
        assert name in result["description_placeholders"]["room_list"]
    assert result["description_placeholders"]["room_count"] == "2"
    assert {"room", "edit_room", "remove_room", "save"} <= set(result["menu_options"])
    assert all(room["floor"] == room["circuit"] == "" for room in flow.rooms)
    form = await flow.async_step_edit_room({"room": "Living room"})
    submitted = form["data_schema"](
        {"name": "Kitchen", "sensors": ["sensor.living"], "advanced": {}}
    )
    invalid = await flow.async_step_room(submitted)
    assert invalid["errors"]["base"] == "invalid_room"
    assert [r["name"] for r in flow.rooms] == ["Living room", "Kitchen"]
    assert invalid["data_schema"]({"advanced": {}})["name"] == "Kitchen"
    submitted["name"] = "Lounge"
    await flow.async_step_room(submitted)
    assert [r["name"] for r in flow.rooms] == ["Lounge", "Kitchen"]
    await flow.async_step_remove_room({"room": "Kitchen"})
    saved = await flow.async_step_save()
    assert [r["name"] for r in saved["data"]["rooms"]] == ["Lounge"]
    options = attach(ThermalPlannerOptionsFlow(entry_for(saved["data"])), hass)
    assert "rooms" in (await options.async_step_init())["menu_options"]
    assert (await options.async_step_rooms())["description_placeholders"]["room_count"] == "1"


async def test_native_floors_optional_and_legacy_labels(hass, fixture):
    registry = floor_registry.async_get(hass)
    registry.async_create("Upper", level=1)
    registry.async_create("Ground", level=0)
    flow = attach(ThermalPlannerConfigFlow(), hass)
    await flow.async_step_user()
    await flow.async_step_settings({"max_age_minutes": 15})
    form = await flow.async_step_room()
    floor_key = next(key for key in form["data_schema"].schema if str(key) == "floor")
    assert isinstance(floor_key, vol.Optional)
    assert form["data_schema"].schema[floor_key].config["options"] == ["Ground", "Upper"]
    hass.states.async_set("sensor.room", 21, {"unit_of_measurement": "°C"})
    submitted = form["data_schema"](
        {"name": "Study", "sensors": ["sensor.room"], "floor": "Upper", "advanced": {}}
    )
    await flow.async_step_room(submitted)
    assert flow.rooms[0]["floor"] == "Upper"
    form = await flow.async_step_edit_room({"room": "Study"})
    # Removing the optional floor must clear its previous value.
    submitted = form["data_schema"]({"name": "Study", "sensors": ["sensor.room"], "advanced": {}})
    await flow.async_step_room(submitted)
    assert flow.rooms[0]["floor"] == ""
    config = deepcopy(fixture["config"])
    config["rooms"][0]["floor"] = "Legacy custom floor"
    options = attach(ThermalPlannerOptionsFlow(entry_for(config)), hass)
    form = await options.async_step_edit_room({"room": "Study"})
    floor_key = next(key for key in form["data_schema"].schema if str(key) == "floor")
    assert "Legacy custom floor" in form["data_schema"].schema[floor_key].config["options"]
    assert options.rooms[0]["floor"] == "Legacy custom floor"


async def test_edit_preserves_advanced_settings_and_sensor_weights(hass, fixture):
    options = attach(ThermalPlannerOptionsFlow(entry_for(fixture["config"])), hass)
    old = deepcopy(options.rooms[0])
    for sensor in [*old["sensors"], "sensor.new"]:
        hass.states.async_set(sensor, 21, {"unit_of_measurement": "°C"})
    form = await options.async_step_edit_room({"room": "Study"})
    sensors = [*reversed(old["sensors"]), "sensor.new"]
    submitted = form["data_schema"](
        {"name": "Study", "floor": old["floor"], "sensors": sensors, "advanced": {}}
    )
    result = await options.async_step_room(submitted)
    assert result["step_id"] == "rooms"
    assert options.rooms[0]["weights"] == [3, 1, 1]
    assert options.rooms[0]["circuit"] == old["circuit"]
    assert options.rooms[0]["floor_weight"] == old["floor_weight"]
    assert options.rooms[0]["constrains"] == old["constrains"]
    assert options.rooms[0]["emitter"] == old["emitter"]
    assert fixture["config"]["rooms"][0] == old


@pytest.mark.parametrize("when", ["2026-03-29T02:30:00", "2026-10-25T02:30:00"])
def test_dst_rejects_skipped_or_ambiguous(when):
    with pytest.raises(ValueError):
        local_timestamp(when, "Europe/Warsaw")


def test_dst_normal_and_explicit():
    assert local_timestamp("2026-01-01T01:00:00", "Europe/Warsaw") == timestamp(
        "2026-01-01T00:00:00Z"
    )
    assert local_timestamp("2026-10-25T02:30:00+01:00", "Europe/Warsaw") == timestamp(
        "2026-10-25T01:30:00Z"
    )


async def test_recorder_adapter_uses_public_api_and_executor(hass, fixture):
    state = SimpleNamespace(
        last_updated=timestamp(fixture["start"]),
        state="68",
        attributes={"unit_of_measurement": "°F"},
    )
    weather = SimpleNamespace(
        last_updated=timestamp(fixture["start"]),
        state="cloudy",
        attributes={"temperature": 10, "temperature_unit": "°C"},
    )
    config = deepcopy(fixture["config"])
    config["weather"] = "weather.any"
    recorder = MagicMock()

    async def executor(job, *args):
        return job(*args)

    recorder.async_add_executor_job = executor
    with (
        patch("custom_components.thermal_planner.history.get_instance", return_value=recorder),
        patch(
            "custom_components.thermal_planner.history.get_significant_states",
            return_value={"sensor.a91": [state], "weather.any": [weather]},
        ) as query,
    ):
        history = await async_history(
            hass, config, timestamp(fixture["start"]), timestamp(fixture["end"])
        )
    assert history["sensor.a91"][0]["unit"] == "°F"
    assert history["weather.any"][0]["value"] == 10
    assert query.call_args.kwargs["include_start_time_state"] is True
    assert query.call_args.kwargs["significant_changes_only"] is False
    assert query.call_args.args[1] == timestamp(fixture["start"]) - timedelta(
        minutes=15, microseconds=1
    )


async def test_replay_storage_actions_csv_and_no_control(hass, fixture):
    hass.http = MagicMock()
    with patch("custom_components.thermal_planner.panel.async_setup_panel", AsyncMock()):
        await async_setup(hass, {})
    assert {"replay", "export_csv", "run_once", "stop_and_restore", "enable_auto"} <= set(
        hass.services.async_services()[DOMAIN]
    )
    entry = entry_for(fixture["config"])
    manager = ReplayManager(hass, entry)
    hass.data[DOMAIN][entry.entry_id] = manager
    with patch(
        "custom_components.thermal_planner.integration.async_history",
        AsyncMock(return_value=fixture["history"]),
    ):
        result = await hass.services.async_call(
            DOMAIN,
            "replay",
            {"entry_id": entry.entry_id, "start": fixture["start"], "end": fixture["end"]},
            blocking=True,
            return_response=True,
        )
    assert manager.status == "complete"
    assert result["report"]["limiting_room"] == "Bathroom"
    restored = ReplayManager(hass, entry)
    saved = await restored.store.async_load()
    assert saved["reports"][0]["configuration"] == fixture["config"]
    export = await hass.services.async_call(
        DOMAIN, "export_csv", {"entry_id": entry.entry_id}, blocking=True, return_response=True
    )
    assert "Bathroom" in export["csv"]
    view = ReportView()
    request = MagicMock(app={"hass": hass})
    request.get.return_value = SimpleNamespace(is_admin=True)
    response = await view.get(request, entry.entry_id, manager.latest["id"])
    assert response.content_type == "text/csv"
    assert view.requires_auth
    assert response.text == export["csv"]
    assert (
        "traces"
        not in __import__("custom_components.thermal_planner.sensor", fromlist=["ThermalSensor"])
        .ThermalSensor(manager, "result")
        .extra_state_attributes
    )


async def test_concurrency_error_and_future_window(hass, fixture):
    from homeassistant.exceptions import ServiceValidationError

    manager = ReplayManager(hass, entry_for(fixture["config"]))
    async with manager.lock:
        with pytest.raises(ServiceValidationError):
            await manager.async_replay(fixture["start"], fixture["end"])
    with pytest.raises(ServiceValidationError):
        await manager.async_replay("2099-01-01T00:00:00Z", "2099-01-01T04:00:00Z")
    with patch(
        "custom_components.thermal_planner.integration.async_history",
        AsyncMock(side_effect=RuntimeError("recorder unavailable")),
    ):
        with pytest.raises(RuntimeError):
            await manager.async_replay(fixture["start"], fixture["end"])
    assert manager.status == "error"
    assert not manager.lock.locked()
    assert manager.latest is None


async def test_real_sqlite_recorder_replay(hass, fixture):
    """Write fixture state events through Recorder, then query its actual SQLite history."""
    from homeassistant import loader
    from homeassistant.components import recorder
    from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, EVENT_STATE_CHANGED
    from homeassistant.core import State
    from homeassistant.setup import async_setup_component

    loader.async_setup(hass)
    from homeassistant.helpers.recorder import async_initialize_recorder

    async_initialize_recorder(hass)
    hass.config.skip_pip = True
    # Recorder's run must encompass the synthetic timestamps.
    from homeassistant.components.recorder.db_schema import RecorderRuns

    assert await async_setup_component(
        hass,
        "recorder",
        {
            "recorder": {
                "db_url": "sqlite:///" + hass.config.path("test.sqlite3"),
                "auto_purge": False,
                "commit_interval": 0,
            }
        },
    )
    instance = recorder.get_instance(hass)
    try:
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await hass.async_block_till_done()
        await instance.async_block_till_done()

        # Set the test Recorder run's start before the historical fixture using its session.
        # Production uses only the public history API and never accesses the database directly.
        def set_start():
            with instance.get_session() as session:
                run = session.query(RecorderRuns).first()
                run.start = timestamp(fixture["start"]) - timedelta(hours=1)
                session.commit()

        await instance.async_add_executor_job(set_start)
        start = timestamp(fixture["start"])
        fixture["history"]["sensor.steady"] = [
            {"timestamp": (start - timedelta(minutes=45)).isoformat(), "value": 21, "unit": "°C"},
            {
                "timestamp": (start + timedelta(minutes=2)).isoformat(),
                "value": "unavailable",
                "unit": "°C",
            },
            {"timestamp": (start + timedelta(minutes=3)).isoformat(), "value": 21, "unit": "°C"},
        ]
        for entity, records in sorted(
            fixture["history"].items(), key=lambda item: timestamp(item[1][0]["timestamp"])
        ):
            for record in records:
                state = State(
                    entity,
                    str(record["value"]),
                    {"unit_of_measurement": record["unit"]},
                    last_changed=timestamp(record["timestamp"]),
                    last_updated=timestamp(record["timestamp"]),
                )
                hass.bus.async_fire(
                    EVENT_STATE_CHANGED,
                    {"entity_id": entity, "old_state": None, "new_state": state},
                )
        await hass.async_block_till_done()
        await instance.async_block_till_done()
        history = await async_history(
            hass, fixture["config"], timestamp(fixture["start"]), timestamp(fixture["end"])
        )
        from custom_components.thermal_planner.analysis import replay

        report = await hass.async_add_executor_job(
            replay, fixture["config"], history, fixture["start"], fixture["end"]
        )
        assert report["windows"][0]["rooms"]["Study"]["slope_c_per_h"] == -1
        assert report["windows"][0]["floors"]["Ground"]["slope_c_per_h"] == -0.75
        assert report["limiting_room"] == "Bathroom"
        seeded_config = deepcopy(fixture["config"])
        seeded_config["history_policy"] = "until_changed"
        seeded_config["rooms"] = [
            dict(seeded_config["rooms"][0], sensors=["sensor.steady"], weights=[1])
        ]
        seeded = await async_history(hass, seeded_config, start, start + timedelta(hours=1))
        seed_report = replay(seeded_config, seeded, start, start + timedelta(hours=1))
        assert seed_report["traces"]["rooms"]["Study"][0] == 21, (
            seeded,
            instance.states_manager.oldest_ts,
        )
        assert seed_report["windows"][0]["rooms"]["Study"]["coverage"] < 1
        assert seed_report["unavailability_events"][0]["state"] == "unavailable"

    finally:
        await hass.async_stop(force=True)


async def test_retention_snapshot_and_reload(hass, fixture):
    from custom_components.thermal_planner.analysis import replay
    from custom_components.thermal_planner.integration import async_setup_entry, async_unload_entry

    hass.data[DOMAIN] = {}
    entry = entry_for(fixture["config"])
    manager = ReplayManager(hass, entry)
    report = replay(fixture["config"], fixture["history"], fixture["start"], fixture["end"])
    report["id"] = "older"
    manager.reports = [dict(report, id=f"old-{i}") for i in range(10)]
    with patch(
        "custom_components.thermal_planner.integration.async_history",
        AsyncMock(return_value=fixture["history"]),
    ):
        await manager.async_replay(fixture["start"], fixture["end"])
    assert len(manager.reports) == 10
    assert manager.reports[0]["id"] == "old-1"
    with (
        patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock()),
        patch.object(entry, "async_on_unload"),
    ):
        assert await async_setup_entry(hass, entry)
    loaded = hass.data[DOMAIN][entry.entry_id]
    assert loaded.latest == manager.latest
    assert loaded.latest["configuration"] == fixture["config"]
    async with loaded.lock:
        assert not await async_unload_entry(hass, entry)
    with patch.object(hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)):
        assert await async_unload_entry(hass, entry)
    assert entry.entry_id not in hass.data[DOMAIN]
