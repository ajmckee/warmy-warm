"""Local report panel and permission-checked HA WebSocket commands."""

from copy import deepcopy
from pathlib import Path

import voluptuous as vol
from homeassistant.auth.permissions.const import POLICY_READ
from homeassistant.components import panel_custom, websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError, Unauthorized

from .config_flow import local_timestamp
from .configuration import all_entities, signature, source_config
from .const import DOMAIN


def can_read(user, config):
    if user.is_admin:
        return True
    entities = all_entities(config)
    return all(user.permissions.check_entity(entity, POLICY_READ) for entity in entities if entity)


def manager_for(hass, connection, msg):
    manager = hass.data[DOMAIN].get(msg["entry_id"])
    if manager is None:
        raise HomeAssistantError("Thermal Planner is not loaded. Add the integration first.")
    if not can_read(connection.user, manager.config):
        raise Unauthorized
    return manager


def summary(report):
    return {
        "id": report["id"],
        "start": report["start"],
        "end": report["end"],
        "limiting_room": report["limiting_room"],
        "room_count": len(report["rooms"]),
        "breaches": [name for name, room in report["rooms"].items() if room["comfort_breached"]],
        "complete": all(
            row["coverage"] == 1 for w in report["windows"] for row in w["rooms"].values()
        ),
    }


@websocket_api.websocket_command({vol.Required("type"): "thermal_planner/reports"})
@websocket_api.async_response
async def ws_reports(hass, connection, msg):
    entries = [
        {
            "entry_id": manager.entry.entry_id,
            "title": manager.entry.title,
            "status": manager.status,
            "room_count": len(manager.config["rooms"]),
            "heat_source": source_config(manager.config),
            "outdoor": manager.config.get("outdoor"),
            "max_age_minutes": manager.config["max_age_minutes"],
            "history_policy": manager.config.get("history_policy", "until_changed"),
            "reports": [
                summary(report)
                for report in reversed(manager.reports)
                if can_read(connection.user, report["configuration"])
            ],
        }
        for manager in hass.data[DOMAIN].values()
        if can_read(connection.user, manager.config)
    ]
    connection.send_result(msg["id"], {"entries": entries})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "thermal_planner/report",
        vol.Required("entry_id"): str,
        vol.Required("report_id"): str,
    }
)
@websocket_api.async_response
async def ws_report(hass, connection, msg):
    manager = manager_for(hass, connection, msg)
    report = next((r for r in manager.reports if r["id"] == msg["report_id"]), None)
    if report is None:
        raise HomeAssistantError("Report no longer retained. Refresh the report list.")
    if not can_read(connection.user, report["configuration"]):
        raise Unauthorized
    connection.send_result(msg["id"], report)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "thermal_planner/analyse",
        vol.Required("entry_id"): str,
        vol.Required("start"): str,
        vol.Required("end"): str,
        vol.Optional("assumed_outdoor_c"): vol.Coerce(float),
        vol.Optional("history_policy"): vol.In(["max_age", "until_changed"]),
    }
)
@websocket_api.async_response
async def ws_analyse(hass, connection, msg):
    manager = manager_for(hass, connection, msg)
    try:
        report = await manager.async_replay(
            local_timestamp(msg["start"], hass.config.time_zone),
            local_timestamp(msg["end"], hass.config.time_zone),
            assumed_outdoor_c=msg.get("assumed_outdoor_c"),
            history_policy=msg.get("history_policy"),
        )
    except ValueError as err:
        raise ServiceValidationError(str(err)) from err
    connection.send_result(msg["id"], report)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "thermal_planner/export",
        vol.Required("entry_id"): str,
        vol.Required("report_id"): str,
    }
)
@websocket_api.async_response
async def ws_export(hass, connection, msg):
    from .analysis import to_csv

    manager = manager_for(hass, connection, msg)
    report = next((r for r in manager.reports if r["id"] == msg["report_id"]), None)
    if report is None:
        raise HomeAssistantError("Report no longer retained.")
    if not can_read(connection.user, report["configuration"]):
        raise Unauthorized
    connection.send_result(msg["id"], await hass.async_add_executor_job(to_csv, report))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "thermal_planner/lab",
        vol.Required("entry_id"): str,
        vol.Optional("action", default="status"): vol.In(["status", "start", "stop"]),
        vol.Optional("minutes", default=5): vol.All(int, vol.Range(min=1, max=60)),
    }
)
@websocket_api.async_response
async def ws_lab(hass, connection, msg):
    from .control import CONTROL_KEY

    manager = manager_for(hass, connection, msg)
    controller = hass.data[CONTROL_KEY]
    if not connection.user.is_admin:
        raise Unauthorized
    if msg.get("action") == "start":
        if hasattr(manager, "engine"):
            await manager.engine.run_once(msg.get("minutes", 5))
        else:
            await controller.start(manager, msg.get("minutes", 5))
    elif msg.get("action") == "stop":
        if hasattr(manager, "engine"):
            await manager.engine.stop()
        else:
            await controller.stop(manager.entry.entry_id)
    connection.send_result(msg["id"], controller.snapshot())


@websocket_api.websocket_command(
    {
        vol.Required("type"): "thermal_planner/operations",
        vol.Required("entry_id"): str,
        vol.Optional("action", default="status"): vol.In(
            [
                "status",
                "run",
                "trial",
                "campaign",
                "stop",
                "fit",
                "replan",
                "apply_once",
                "auto",
                "commission",
                "seed_demo",
                "run_detail",
                "observe",
                "setup",
            ]
        ),
        vol.Optional("minutes"): vol.All(int, vol.Range(min=1, max=720)),
        vol.Optional("flow_target"): vol.All(vol.Coerce(float), vol.Range(min=0, max=90)),
        vol.Optional("plan_id"): str,
        vol.Optional("run_id"): str,
        vol.Optional("acknowledged", default=False): bool,
        vol.Optional("setup"): dict,
    }
)
@websocket_api.async_response
async def ws_operations(hass, connection, msg):
    manager = manager_for(hass, connection, msg)
    engine = getattr(manager, "engine", None)
    if engine is None:
        raise HomeAssistantError("Operations engine is not ready.")
    action = msg.get("action", "status")
    if action == "run_detail":
        run = next((run for run in engine.runs if run["id"] == msg.get("run_id")), None)
        if run is None or not can_read(connection.user, run["configuration"]):
            raise Unauthorized
        connection.send_result(msg["id"], run)
        return
    if action != "status" and not connection.user.is_admin:
        raise Unauthorized
    if action == "setup":
        await save_measurement_setup(hass, manager, msg.get("setup", {}))
    elif action in ("run", "trial"):
        if action == "trial" and "flow_target" not in msg:
            raise ServiceValidationError("Choose the explicitly approved direct flow target.")
        await engine.run_once(
            msg.get("minutes"), msg.get("flow_target") if action == "trial" else None
        )
    elif action == "observe":
        await engine.observe_normal(msg.get("minutes", 120))
    elif action == "commission":
        await engine.commission(msg.get("acknowledged", False))
    elif action == "campaign":
        await engine.start_campaign()
    elif action == "stop":
        await engine.stop()
    elif action == "fit":
        await engine.fit()
    elif action == "replan":
        await engine.replan()
    elif action in ("apply_once", "auto"):
        await engine.apply(msg.get("plan_id"), automatic=action == "auto")
    elif action == "seed_demo":
        from .analysis.demo import demo_episodes
        from .source import HeatSource

        if (
            not engine.controller.simulated(manager.config)
            or engine.controller.journal
            or engine.observation
        ):
            raise ServiceValidationError(
                "Synthetic fixtures are available only for an idle, explicitly configured development source."
            )
        baseline = HeatSource(hass, manager.config, engine.now).flow()
        source = source_config(manager.config)
        alternate = (
            max(source.get("flow_min", 0), baseline - source.get("flow_max_step", 0))
            if baseline is not None and source.get("flow_setter")
            else None
        )
        data = await hass.async_add_executor_job(
            demo_episodes, deepcopy(dict(manager.config)), baseline, alternate
        )
        incoming = {run["id"] for run in data}
        engine.runs = ([run for run in engine.runs if run["id"] not in incoming] + data)[-40:]
        await engine.fit()
    state = engine.snapshot()
    visible = {run["id"] for run in engine.runs if can_read(connection.user, run["configuration"])}
    state["runs"] = [run for run in state["runs"] if run["id"] in visible]
    if engine.models.get("signature") != signature(manager.config):
        state["models"] = {"reasons": ["No model matches the saved configuration."]}
    if not connection.user.is_admin:
        state["control"] = None
        state["events"] = []
    connection.send_result(msg["id"], state)


async def async_setup_panel(hass):
    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(
                "/thermal_planner_frontend", str(Path(__file__).parent / "frontend"), False
            )
        ]
    )
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path="thermal-planner",
        webcomponent_name="thermal-planner-panel",
        sidebar_title="Thermal Planner",
        sidebar_icon="mdi:home-thermometer-outline",
        module_url="/thermal_planner_frontend/panel.js?v=0.3.2",
    )
    for command in (ws_reports, ws_report, ws_analyse, ws_export, ws_lab, ws_operations):
        websocket_api.async_register_command(hass, command)


async def save_measurement_setup(hass, manager, values):
    """Save only explicitly reviewed setup selections; never send heat commands."""
    from .analysis import validate_config
    from .source import HeatSource

    engine = manager.engine
    if (
        engine.baseline
        or engine.observation
        or engine.mode != "advisory"
        or (engine.campaign and engine.campaign.get("active"))
        or (engine.controller and engine.controller.journal)
    ):
        raise ServiceValidationError(
            "Stop the current measurement or schedule before changing setup."
        )
    allowed = {
        "climate",
        "outdoor",
        "operating_mode",
        "dhw_states",
        "defrost_indicator",
        "leaving_water",
        "flow_readback",
        "commissioning_allowed",
        "max_age_minutes",
    }
    if set(values) - allowed:
        raise ServiceValidationError("Unsupported setup setting.")
    config = deepcopy(dict(manager.config))
    source = source_config(config)
    for key, value in values.items():
        if key in ("outdoor", "max_age_minutes"):
            config[key] = value
        else:
            source[key] = value
    if not isinstance(source.get("commissioning_allowed"), bool):
        raise ServiceValidationError("Confirm supervised commissioning using the checkbox.")
    if not isinstance(source.get("dhw_states", []), list) or any(
        not isinstance(v, str) for v in source.get("dhw_states", [])
    ):
        raise ServiceValidationError("Choose the hot-water state labels.")
    config["heat_source"] = source
    for key in (
        "climate",
        "operating_mode",
        "defrost_indicator",
        "leaving_water",
        "flow_readback",
        "outdoor",
    ):
        entity = config.get(key) if key == "outdoor" else source.get(key)
        if not entity:
            continue
        if (
            not isinstance(entity, str)
            or entity.split(".")[0]
            not in (
                ("climate",)
                if key == "climate"
                else ("sensor", "binary_sensor", "select", "number", "input_number")
            )
            or hass.states.get(entity) is None
        ):
            raise ServiceValidationError(
                f"Choose an existing Home Assistant entity for {key.replace('_', ' ')}."
            )
        if key in ("outdoor", "leaving_water", "flow_readback") and hass.states.get(
            entity
        ).attributes.get("unit_of_measurement") not in ("°C", "°F", "K"):
            raise ServiceValidationError("Choose a temperature entity, not a water-flow rate.")
    try:
        validate_config(config)
        HeatSource(hass, config, engine.now).validate_capabilities()
    except (ValueError, TypeError) as err:
        raise ServiceValidationError(str(err)) from err
    hass.config_entries.async_update_entry(manager.entry, options=config)
