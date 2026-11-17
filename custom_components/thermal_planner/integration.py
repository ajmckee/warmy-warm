"""Historical observation and development-only controlled measurement."""

import asyncio
from copy import deepcopy
from uuid import uuid4

import voluptuous as vol
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .analysis import replay, timestamp, to_csv, validate_config
from .const import DOMAIN, MAX_REPORTS, PLATFORMS, STORAGE_VERSION
from .history import async_history


class ReplayManager:
    def __init__(self, hass, entry):
        self.hass = hass
        self.entry = entry
        self.store = Store(
            hass,
            STORAGE_VERSION,
            f"{DOMAIN}.{entry.entry_id}",
            private=True,
            serialize_in_event_loop=False,
        )
        self.reports = []
        self.status = "idle"
        self.lock = asyncio.Lock()
        self.report_lock = asyncio.Lock()

    async def async_store_report(self, report):
        async with self.report_lock:
            reports = (self.reports + [report])[-MAX_REPORTS:]
            await self.store.async_save({"reports": reports})
            self.reports = reports
            self.publish()

    @property
    def config(self):
        return self.entry.options or self.entry.data

    @property
    def latest(self):
        return self.reports[-1] if self.reports else None

    def publish(self):
        async_dispatcher_send(self.hass, f"{DOMAIN}_{self.entry.entry_id}")

    async def async_replay(self, start, end, *, assumed_outdoor_c=None, history_policy=None):
        if self.lock.locked():
            raise ServiceValidationError("A replay is already running")
        start, end = timestamp(start), timestamp(end)
        if not 60 <= (end - start).total_seconds() <= 86400 or end > dt_util.utcnow():
            raise ServiceValidationError("Select a past window between one minute and 24 hours")
        async with self.lock:
            config = deepcopy(dict(self.config))
            if assumed_outdoor_c is not None:
                from .analysis.signals import finite

                assumed = finite(assumed_outdoor_c)
                if assumed is None or not -80 <= assumed <= 60:
                    raise ServiceValidationError(
                        "Choose an assumed outdoor temperature between −80 and 60°C."
                    )
                config["assumed_outdoor_c"] = assumed
            if history_policy is not None:
                if history_policy not in ("max_age", "until_changed"):
                    raise ServiceValidationError("Choose a valid history freshness policy.")
                config["history_policy"] = history_policy
            self.status = "replaying"
            self.publish()
            try:
                history = await async_history(self.hass, config, start, end)
                report = await self.hass.async_add_executor_job(replay, config, history, start, end)
                report["id"] = uuid4().hex
                await self.async_store_report(report)
                self.status = "complete"
                return report
            except Exception:
                self.status = "error"
                raise
            finally:
                self.publish()


class ReportView(HomeAssistantView):
    url = "/api/thermal_planner/{entry_id}/{report_id}.csv"
    name = "api:thermal_planner:csv"
    requires_auth = True

    async def get(self, request, entry_id, report_id):
        from .panel import can_read

        manager = request.app["hass"].data.get(DOMAIN, {}).get(entry_id)
        if manager is None:
            raise web.HTTPNotFound()
        report = next((r for r in manager.reports if r["id"] == report_id), None)
        if report is None:
            raise web.HTTPNotFound()
        user = request.get("hass_user")
        if user is None or not can_read(user, report["configuration"]):
            raise web.HTTPForbidden()
        content = await manager.hass.async_add_executor_job(to_csv, report)
        return web.Response(
            text=content,
            content_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="thermal-planner-{report_id}.csv"'
            },
        )


async def async_setup(hass, config):
    from .control import CONTROL_KEY, LabController
    from .panel import async_setup_panel

    hass.data.setdefault(DOMAIN, {})
    hass.http.register_view(ReportView())
    controller = hass.data[CONTROL_KEY] = LabController(hass)
    await controller.async_setup()
    await async_setup_panel(hass)

    def manager_for(call):
        manager = hass.data[DOMAIN].get(call.data["entry_id"])
        if manager is None:
            raise ServiceValidationError("Thermal Planner entry is not loaded")
        return manager

    async def check_reader(call, config):
        from .panel import can_read

        if call.context.user_id:
            user = await hass.auth.async_get_user(call.context.user_id)
            if user is None or not can_read(user, config):
                raise ServiceValidationError(
                    "You do not have access to this report's source entities."
                )

    async def run(call):
        await check_reader(call, manager_for(call).config)
        try:
            report = await manager_for(call).async_replay(
                call.data["start"],
                call.data["end"],
                assumed_outdoor_c=call.data.get("assumed_outdoor_c"),
                history_policy=call.data.get("history_policy"),
            )
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err
        return {"report": report}

    async def export(call):
        manager = manager_for(call)
        report_id = call.data.get("report_id")
        report = (
            next((r for r in manager.reports if r["id"] == report_id), None)
            if report_id
            else manager.latest
        )
        if report is None:
            raise HomeAssistantError("No retained report; replay a historical window first")
        await check_reader(call, report["configuration"])
        return {
            "report_id": report["id"],
            "csv": await hass.async_add_executor_job(to_csv, report),
            "download_path": f"/api/thermal_planner/{manager.entry.entry_id}/{report['id']}.csv",
        }

    hass.services.async_register(
        DOMAIN,
        "replay",
        run,
        schema=vol.Schema(
            {
                vol.Required("entry_id"): str,
                vol.Required("start"): str,
                vol.Required("end"): str,
                vol.Optional("assumed_outdoor_c"): vol.Coerce(float),
                vol.Optional("history_policy"): vol.In(["until_changed", "max_age"]),
            }
        ),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        "export_csv",
        export,
        schema=vol.Schema({vol.Required("entry_id"): str, vol.Optional("report_id"): str}),
        supports_response=SupportsResponse.ONLY,
    )

    operation_names = {
        "run_once": "run",
        "stop_and_restore": "stop",
        "start_campaign": "campaign",
        "fit_models": "fit",
        "refresh_plan": "replan",
        "apply_once": "apply_once",
        "enable_auto": "auto",
        "start_flow_trial": "trial",
        "observe_normal": "observe",
        "commission_source": "commission",
    }

    async def operation(call):
        manager = manager_for(call)
        if call.context.user_id:
            user = await hass.auth.async_get_user(call.context.user_id)
            if user is None or not user.is_admin:
                raise ServiceValidationError("Source operations require an administrator.")
        engine = getattr(manager, "engine", None)
        if engine is None:
            raise ServiceValidationError("Operations are not ready.")
        action = operation_names[call.service]
        if action in ("run", "trial"):
            if action == "trial" and "flow_target" not in call.data:
                raise ServiceValidationError("Flow target is required.")
            await engine.run_once(
                call.data.get("minutes"),
                call.data.get("flow_target") if action == "trial" else None,
            )
        elif action == "stop":
            await engine.stop()
        elif action == "campaign":
            await engine.start_campaign()
        elif action == "fit":
            await engine.fit()
        elif action == "replan":
            await engine.replan()
        elif action in ("apply_once", "auto"):
            await engine.apply(call.data.get("plan_id"), action == "auto")
        elif action == "observe":
            await engine.observe_normal(call.data.get("minutes", 120))
        elif action == "commission":
            await engine.commission(call.data.get("acknowledged", False))
        return engine.snapshot()

    schema = vol.Schema(
        {
            vol.Required("entry_id"): str,
            vol.Optional("minutes"): vol.All(vol.Coerce(int), vol.Range(min=1, max=720)),
            vol.Optional("flow_target"): vol.Coerce(float),
            vol.Optional("plan_id"): str,
            vol.Optional("acknowledged", default=False): bool,
        }
    )
    for name in operation_names:
        hass.services.async_register(
            DOMAIN, name, operation, schema=schema, supports_response=SupportsResponse.OPTIONAL
        )
    return True


async def async_setup_entry(hass, entry):
    from .operations import OperationsEngine

    validate_config(entry.options or entry.data)
    manager = ReplayManager(hass, entry)
    saved = await manager.store.async_load()
    manager.reports = (saved or {}).get("reports", [])[-MAX_REPORTS:]
    manager.status = "complete" if manager.latest else "idle"
    hass.data[DOMAIN][entry.entry_id] = manager
    manager.engine = OperationsEngine(hass, manager)
    await manager.engine.async_setup()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload))
    return True


async def async_reload(hass, entry):
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass, entry):
    from .control import CONTROL_KEY

    manager = hass.data[DOMAIN][entry.entry_id]
    if manager.lock.locked():
        return False
    controller = hass.data.get(CONTROL_KEY)
    if controller and controller.journal and controller.journal["entry_id"] == entry.entry_id:
        await controller.stop(entry.entry_id)
        if controller.journal:
            return False
    if await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        if hasattr(manager, "engine"):
            manager.engine.close()
        hass.data[DOMAIN].pop(entry.entry_id)
        return True
    return False


async def async_remove_entry(hass, entry):
    await Store(
        hass,
        STORAGE_VERSION,
        f"{DOMAIN}.{entry.entry_id}",
        private=True,
        serialize_in_event_loop=False,
    ).async_remove()
    await Store(hass, 1, f"{entry.entry_id}.operations", private=True).async_remove()
