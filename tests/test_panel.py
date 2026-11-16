"""Report API permissions, historical snapshots, summaries and panel registration."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError, Unauthorized

from custom_components.thermal_planner.analysis import replay
from custom_components.thermal_planner.const import DOMAIN
from custom_components.thermal_planner.integration import ReplayManager, ReportView
from custom_components.thermal_planner.panel import (
    async_setup_panel,
    ws_analyse,
    ws_export,
    ws_lab,
    ws_report,
    ws_reports,
)


@pytest.fixture
async def report_api(simulator_factory, fixture):
    hass = await simulator_factory()
    entry = SimpleNamespace(
        entry_id="reports-entry", title="Thermal Planner", data=fixture["config"], options={}
    )
    manager = ReplayManager(hass, entry)
    report = replay(fixture["config"], fixture["history"], fixture["start"], fixture["end"])
    report["id"] = "saved-report"
    manager.reports = [report]
    hass.data[DOMAIN] = {entry.entry_id: manager}
    connection = SimpleNamespace(user=SimpleNamespace(is_admin=True), send_result=MagicMock())
    return hass, manager, connection


async def test_list_detail_and_csv_without_bulky_summaries(report_api):
    hass, manager, connection = report_api
    await ws_reports.__wrapped__(hass, connection, {"id": 1})
    result = connection.send_result.call_args.args[1]
    saved = result["entries"][0]["reports"][0]
    assert saved["id"] == "saved-report"
    assert saved["breaches"] == ["Bathroom"]
    assert "traces" not in saved and "configuration" not in saved
    msg = {"id": 2, "entry_id": manager.entry.entry_id, "report_id": "saved-report"}
    await ws_report.__wrapped__(hass, connection, msg)
    assert connection.send_result.call_args.args[1]["traces"]["rooms"]["Study"]
    await ws_export.__wrapped__(hass, connection, msg)
    assert "Bathroom" in connection.send_result.call_args.args[1]
    msg["report_id"] = "removed"
    with pytest.raises(HomeAssistantError, match="retained"):
        await ws_report.__wrapped__(hass, connection, msg)


async def test_entity_permissions_also_cover_historical_inputs(report_api):
    hass, manager, connection = report_api
    connection.user = SimpleNamespace(
        is_admin=False, permissions=SimpleNamespace(check_entity=lambda *args: False)
    )
    await ws_reports.__wrapped__(hass, connection, {"id": 1})
    assert connection.send_result.call_args.args[1] == {"entries": []}
    msg = {"id": 2, "entry_id": manager.entry.entry_id, "report_id": "saved-report"}
    with pytest.raises(Unauthorized):
        await ws_report.__wrapped__(hass, connection, msg)
    connection.user.permissions.check_entity = lambda entity, _: (
        entity != "sensor.restricted_old_input"
    )
    historical = deepcopy(manager.reports[0])
    historical["configuration"]["rooms"][0]["diagnostics"] = ["sensor.restricted_old_input"]
    manager.reports = [historical]
    await ws_reports.__wrapped__(hass, connection, {"id": 3})
    assert connection.send_result.call_args.args[1]["entries"][0]["reports"] == []
    for handler in (ws_report, ws_export):
        with pytest.raises(Unauthorized):
            await handler.__wrapped__(hass, connection, msg)
    request = MagicMock(app={"hass": hass})
    request.get.return_value = connection.user
    with pytest.raises(web.HTTPForbidden):
        await ReportView().get(request, manager.entry.entry_id, "saved-report")


async def test_analyse_uses_ha_timezone_and_saves_report(report_api, fixture):
    hass, manager, connection = report_api
    with patch(
        "custom_components.thermal_planner.integration.async_history",
        AsyncMock(return_value=fixture["history"]),
    ):
        await ws_analyse.__wrapped__(
            hass,
            connection,
            {
                "id": 1,
                "entry_id": manager.entry.entry_id,
                "start": "2026-01-01T01:00:00",
                "end": "2026-01-01T05:00:00",
            },
        )
    report = connection.send_result.call_args.args[1]
    assert report["start"] == "2026-01-01T00:00:00+00:00"
    assert manager.latest["id"] == report["id"]
    with pytest.raises(ServiceValidationError, match="Ambiguous"):
        await ws_analyse.__wrapped__(
            hass,
            connection,
            {
                "id": 2,
                "entry_id": manager.entry.entry_id,
                "start": "2026-10-25T02:30:00",
                "end": "2026-10-25T03:30:00",
            },
        )


async def test_lab_requires_admin_even_for_status(report_api):
    hass, manager, connection = report_api
    from custom_components.thermal_planner.control import CONTROL_KEY, LabController

    hass.data[CONTROL_KEY] = LabController(hass)
    connection.user = SimpleNamespace(
        is_admin=False, permissions=SimpleNamespace(check_entity=lambda *args: True)
    )
    with pytest.raises(Unauthorized):
        await ws_lab.__wrapped__(hass, connection, {"id": 1, "entry_id": manager.entry.entry_id})


async def test_local_panel_registration(report_api):
    hass, _, _ = report_api
    hass.http = SimpleNamespace(async_register_static_paths=AsyncMock())
    with patch(
        "custom_components.thermal_planner.panel.panel_custom.async_register_panel", AsyncMock()
    ) as register:
        await async_setup_panel(hass)
    assert register.call_args.kwargs["frontend_url_path"] == "thermal-planner"
    assert register.call_args.kwargs["module_url"].startswith("/thermal_planner_frontend/")
    paths = hass.http.async_register_static_paths.call_args.args[0]
    assert paths[0].url_path == "/thermal_planner_frontend"
