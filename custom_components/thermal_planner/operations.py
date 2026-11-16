"""Persistent campaigns, recovery observations, model fitting and guarded planning."""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CoreState, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from .analysis import replay, timestamp
from .analysis.learning import fit_bundle
from .analysis.planning import forecast_grid, local_instant, make_plan
from .analysis.signals import energy_summary, recovery_summary
from .configuration import signature, source_config, source_fingerprint
from .control import CONTROL_KEY
from .source import HeatSource

MAX_RUNS = 40


class OperationsEngine:
    def __init__(self, hass, manager, now=None):
        from homeassistant.util import dt as dt_util

        self.hass, self.manager = hass, manager
        self.now = now or dt_util.utcnow
        self.store = Store(
            hass,
            1,
            f"{self.manager.entry.entry_id}.operations",
            private=True,
            atomic_writes=True,
            serialize_in_event_loop=False,
        )
        self.lock = asyncio.Lock()
        self.runs = []
        self.observation = None
        self.baseline = None
        self.campaign = None
        self.models = {}
        self.plan = None
        self.mode = "advisory"
        self.pending = None
        self.baseline_settings = None
        self.last_replan = None
        self.forecast = {"available": False, "reason": "Forecast not fetched yet."}
        self.events = []
        self.cancel = []

    @property
    def controller(self):
        return self.hass.data.get(CONTROL_KEY)

    @property
    def config(self):
        return self.manager.config

    async def save(self):
        await self.store.async_save(
            {
                key: getattr(self, key)
                for key in (
                    "runs",
                    "observation",
                    "baseline",
                    "campaign",
                    "models",
                    "plan",
                    "mode",
                    "pending",
                    "baseline_settings",
                    "last_replan",
                    "events",
                )
            }
        )
        self.manager.publish()

    async def load(self):
        saved = await self.store.async_load() or {}
        for key in (
            "runs",
            "observation",
            "baseline",
            "campaign",
            "models",
            "plan",
            "mode",
            "pending",
            "baseline_settings",
            "last_replan",
            "events",
        ):
            if key in saved:
                setattr(self, key, saved[key])
        if self.plan and self.plan.get("signature") != signature(self.config):
            self.plan, self.pending, self.mode = None, None, "advisory"
        if self.pending and self.pending.get("signature") != signature(self.config):
            self.pending, self.mode = None, "advisory"

    async def async_setup(self):
        await self.load()

        @callback
        def stopped(event):
            self.close()

        self.cancel.append(self.hass.bus.async_listen(EVENT_HOMEASSISTANT_STOP, stopped))
        self.cancel.append(async_track_time_interval(self.hass, self.tick, timedelta(seconds=30)))
        if self.hass.state == CoreState.running:
            await self.tick()
        else:

            async def started(event):
                await self.tick()

            self.cancel.append(self.hass.bus.async_listen(EVENT_HOMEASSISTANT_STARTED, started))

    def close(self):
        for cancel in self.cancel:
            cancel()
        self.cancel.clear()

    async def event(self, message):
        self.events.append({"at": self.now().isoformat(), "message": message})
        self.events = self.events[-100:]
        await self.save()

    async def external_override(self):
        self.mode, self.pending = "advisory", None
        if self.campaign:
            self.campaign["active"] = False
        await self.event(
            "External source/zone command cancelled scheduled ownership; manual changes are preserved."
        )

    def snapshot(self):
        return {
            "source": source_config(self.config),
            "rooms": [
                {
                    key: room.get(key)
                    for key in ("name", "zone", "emitter", "comfort_min", "comfort_target")
                }
                for room in self.config["rooms"]
            ],
            "campaign_settings": self.config.get("campaign"),
            "baseline": {"started": self.baseline["started"], "deadline": self.baseline["deadline"]}
            if self.baseline
            else None,
            "current": {
                key: value
                for key, value in HeatSource(self.hass, self.config, self.now).sample().items()
                if key in ("rooms", "outdoor", "flow_target", "mode", "classification")
            },
            "control": self.controller.snapshot() if self.controller else None,
            "mode": self.mode,
            "campaign": deepcopy(self.campaign),
            "observation": {
                "run_id": self.observation["run_id"],
                "deadline": self.observation["recovery_deadline"],
            }
            if self.observation
            else None,
            "runs": [
                {
                    key: run.get(key)
                    for key in (
                        "id",
                        "started",
                        "ended",
                        "outcome",
                        "reason",
                        "recovery",
                        "energy",
                        "learning_eligible",
                        "flow_target",
                        "approved_flow_trial",
                        "report_id",
                        "synthetic",
                    )
                }
                for run in reversed(self.runs)
            ],
            "models": deepcopy(self.models),
            "plan": deepcopy(self.plan),
            "forecast": {
                key: value
                for key, value in self.forecast.items()
                if key not in ("outside", "times")
            },
            "pending": deepcopy(self.pending),
            "events": deepcopy(self.events[-20:]),
        }

    async def run_once(self, minutes=None, trial_flow=None, *, purpose="manual"):
        if self.observation or self.baseline:
            raise ServiceValidationError(
                "Recovery observation is still active. Stop it before another run."
            )
        if self.controller is None:
            raise ServiceValidationError("Controller is not available.")
        minutes = int(minutes or source_config(self.config)["pause_limit_minutes"])
        await self.controller.start(self.manager, minutes, trial_flow=trial_flow, purpose=purpose)

    async def commission(self, acknowledged=False):
        if not acknowledged:
            raise ServiceValidationError(
                "Supervised on-site confirmation is required for commissioning."
            )
        if self.observation or self.baseline:
            raise ServiceValidationError("Stop current observation before commissioning.")
        await self.controller.start(self.manager, 1, commissioning=True, purpose="commissioning")
        await self.event(
            "Commissioning pause started. Stop and verify restoration while observing the installation."
        )

    async def stop(self):
        self.mode, self.pending = "advisory", None
        if self.campaign:
            self.campaign["active"] = False
        await self.save()
        if self.controller:
            await self.controller.stop(self.manager.entry.entry_id)
        if self.observation:
            await self.finish_observation("Recovery observation stopped by user.")
        if self.baseline:
            await self.finish_baseline(partial=True)
        await self.event(
            "Scheduled actions stopped; any owned settings were submitted for restoration."
        )

    async def start_campaign(self):
        settings = self.config.get("campaign")
        if not settings:
            raise ServiceValidationError("Configure campaign time, count and end date first.")
        if self.campaign and self.campaign.get("active"):
            raise ServiceValidationError("A campaign is already active.")
        if not self.controller or not self.controller.allowed(self.config):
            raise ServiceValidationError(
                "Commission the selected source before starting a campaign."
            )
        local = self.now().astimezone(ZoneInfo(self.hass.config.time_zone))
        self.campaign = {
            "id": uuid4().hex,
            "active": True,
            "settings": deepcopy(settings),
            "signature": signature(self.config),
            "next_date": (
                local.date() + timedelta(days=int(local.strftime("%H:%M") > settings["start_time"]))
            ).isoformat(),
            "completed": 0,
            "skips": [],
            "claimed_date": None,
        }
        self.mode, self.pending = "advisory", None
        await self.event(
            "Campaign started with saved settings; missed/unsafe nights will be recorded, never caught up blindly."
        )

    async def campaign_tick(self):
        campaign = self.campaign
        if not campaign or not campaign["active"]:
            return
        if campaign["signature"] != signature(self.config):
            campaign["active"] = False
            await self.event("Campaign stopped because source/room configuration changed.")
            return
        settings = campaign["settings"]
        if (
            campaign["completed"] >= settings["nights"]
            or campaign["next_date"] > settings["end_date"]
            or self.now().astimezone(ZoneInfo(self.hass.config.time_zone)).date().isoformat()
            > settings["end_date"]
        ):
            campaign["active"] = False
            await self.event("Campaign finished: target count or end date reached.")
            return
        day = datetime.fromisoformat(campaign["next_date"]).date()
        wall = f"{day.isoformat()}T{settings['start_time']}"
        try:
            due = local_instant(wall, self.hass.config.time_zone)
        except ValueError:
            campaign["skips"].append(
                {"date": day.isoformat(), "reason": "Ambiguous/skipped DST start time."}
            )
            campaign["next_date"] = (day + timedelta(days=1)).isoformat()
            await self.save()
            return
        if self.now() < due:
            return
        campaign["next_date"] = (day + timedelta(days=1)).isoformat()
        campaign["claimed_date"] = day.isoformat()
        # Claim the night before a control command. A restart cannot start it twice.
        await self.save()
        reason = None
        if self.now() - due > timedelta(minutes=5):
            reason = "Scheduled start was missed while HA was offline or delayed."
        elif self.observation or self.baseline or (self.controller and self.controller.journal):
            reason = "Heat source or recovery observation already busy."
        else:
            try:
                await self.run_once(
                    int(settings["pause_minutes"]), purpose=f"campaign:{campaign['id']}"
                )
            except HomeAssistantError as err:
                reason = str(err)
        if reason:
            campaign["skips"].append({"date": day.isoformat(), "reason": reason})
            campaign["skips"] = campaign["skips"][-90:]
            await self.save()

    async def ingest_completed_control(self):
        run = self.controller.last_run if self.controller else None
        if not run or run["entry_id"] != self.manager.entry.entry_id:
            return
        if any(saved["id"] == run["run_id"] for saved in self.runs) or (
            self.observation and self.observation["run_id"] == run["run_id"]
        ):
            return
        record = deepcopy(run)
        if (
            record["outcome"] != "restored"
            or record.get("commissioning")
            or not record.get("points")
        ):
            await self.finish_record(record, "Run ended without a complete learning observation.")
            if record["outcome"] == "external_override":
                self.mode, self.pending = "advisory", None
                if self.campaign:
                    self.campaign["active"] = False
                await self.event("Manual takeover stopped automatic ownership and the campaign.")
            return
        if record.get("recovery"):
            await self.finish_record(record, record["reason"])
            return
        settings = source_config(record["guards"])
        record["resumed_at"] = record["ended"]
        record["recovery_deadline"] = (
            timestamp(record["ended"]) + timedelta(minutes=settings["recovery_minutes"])
        ).isoformat()
        record["points"].append(
            dict(HeatSource(self.hass, record["guards"], self.now).sample(), phase="recovery")
        )
        self.observation = record
        await self.event("Heat restored; recording recovery independently of Recorder retention.")

    async def observe_tick(self):
        if not self.observation:
            return
        record = self.observation
        points = record["points"]
        if self.now().timestamp() - points[-1]["time"] >= 60:
            points.append(
                dict(HeatSource(self.hass, record["guards"], self.now).sample(), phase="recovery")
            )
            record["points"] = points[-2000:]
            await self.save()
        recovery = recovery_summary(
            record["points"],
            record["guards"]["rooms"],
            record["starting"],
            source_config(record["guards"])["recovery_tolerance_c"],
        )
        if recovery["complete"] or self.now() >= timestamp(record["recovery_deadline"]):
            await self.finish_observation(
                "Recovery targets reached."
                if recovery["complete"]
                else "Recovery observation ended partial."
            )
        elif points[-1].get("mode") not in ("heat", None):
            await self.finish_observation(
                "Recovery changed externally; observation marked partial."
            )

    async def finish_observation(self, reason):
        record = self.observation
        self.observation = None
        await self.finish_record(record, reason)

    async def finish_record(self, record, reason):
        config = record["guards"]
        points = [p for p in record.get("points", []) if p.get("phase") != "finished"]
        recovery = record.get("recovery") or recovery_summary(
            points,
            config["rooms"],
            record.get("starting", {}),
            source_config(config)["recovery_tolerance_c"],
        )
        valid = (
            bool(points)
            and all(
                p.get("outdoor") is not None
                and not p.get("contaminated")
                and all(
                    p.get("rooms", {}).get(room["name"]) is not None for room in config["rooms"]
                )
                for p in points
            )
            and all(0 < b["time"] - a["time"] <= 600 for a, b in zip(points, points[1:]))
        )
        # Temperature learning needs no energy input; neither channel is mandatory.
        valid = valid and all(p.get("heat") in (0, 1) for p in points)
        flow_target = record.get("flow_target")
        if flow_target is not None:
            valid = valid and all(
                p.get("flow_target") is not None and abs(p["flow_target"] - flow_target) <= 0.2
                for p in points
            )
        result = {
            "id": record["run_id"],
            "started": record["started"],
            "ended": self.now().isoformat(),
            "cooldown_ended": record.get("resumed_at") or record.get("ended"),
            "outcome": record.get("outcome"),
            "reason": reason,
            "signature": signature(config),
            "configuration": deepcopy(config),
            "source_fingerprint": source_fingerprint(config),
            "flow_target": flow_target,
            "approved_flow_trial": bool(record.get("flow")) and recovery["complete"],
            "initial_heat": record.get("initial_heat", 1),
            "points": points,
            "recovery": recovery,
            "events": record.get("events", []),
            "energy": energy_summary(points, config.get("energy", {})),
            "learning_eligible": valid
            and recovery["complete"]
            and record.get("outcome") == "restored"
            and not record.get("commissioning")
            and len(points) >= 12,
            "purpose": record.get("purpose", "manual"),
        }
        if (
            points
            and record.get("ended")
            and timestamp(record["ended"]) - timestamp(record["started"]) >= timedelta(minutes=1)
        ):
            history = {}
            for point in points:
                for entity, reading in point.get("signals", {}).items():
                    history.setdefault(entity, {})[reading["timestamp"]] = reading
            history = {entity: list(readings.values()) for entity, readings in history.items()}
            if history:
                report = await self.hass.async_add_executor_job(
                    replay, config, history, record["started"], result["cooldown_ended"]
                )
                report["id"] = uuid4().hex
                report["measurement_run_id"] = result["id"]
                await self.manager.async_store_report(report)
                result["report_id"] = report["id"]
        self.runs.append(result)
        self.runs = self.runs[-MAX_RUNS:]
        if (
            self.campaign
            and result["purpose"] == f"campaign:{self.campaign['id']}"
            and result["outcome"] == "restored"
        ):
            self.campaign["completed"] += 1
        await self.event(reason)

    async def fit(self):
        self.models = await self.hass.async_add_executor_job(
            fit_bundle,
            deepcopy(dict(self.config)),
            list(self.runs),
            self.models,
            self.now().isoformat(),
        )
        await self.event(
            "Model fitting finished. Held-out errors and withholding reasons are available below."
        )
        return self.models

    async def fetch_forecast(self):
        weather = self.config.get("weather")
        if not weather:
            self.forecast = {
                "available": False,
                "reason": "No weather entity selected. Reports/current observations remain available.",
            }
            return
        state = self.hass.states.get(weather)
        if state is None or state.state in ("unknown", "unavailable"):
            self.forecast = {"available": False, "reason": "Weather entity unavailable."}
            return
        try:
            async with asyncio.timeout(15):
                response = await self.hass.services.async_call(
                    "weather",
                    "get_forecasts",
                    {"entity_id": weather, "type": "hourly"},
                    blocking=True,
                    return_response=True,
                )
            adapter = HeatSource(self.hass, self.config, self.now)
            sample = adapter.sample()
            self.forecast = forecast_grid(
                (response or {}).get(weather, {}).get("forecast", []),
                state.attributes.get("temperature_unit"),
                self.now(),
                sample["outdoor"],
            )
            self.forecast["fetched_at"] = self.now().isoformat()
        except (
            HomeAssistantError,
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            TimeoutError,
        ) as err:
            self.forecast = {"available": False, "reason": f"Hourly forecast failed: {err}"}

    async def replan(self):
        if self.mode == "once" and self.pending:
            self.pending, self.mode = None, "advisory"
        await self.fetch_forecast()
        current = HeatSource(self.hass, self.config, self.now).sample()
        current["last_switch"] = self.controller.last_switch if self.controller else None
        self.plan = await self.hass.async_add_executor_job(
            make_plan,
            deepcopy(dict(self.config)),
            self.models,
            current,
            self.forecast,
            self.now(),
            self.hass.config.time_zone,
        )
        self.last_replan = self.now().isoformat()
        if self.plan["status"] != "ready" or not self.plan.get("automatic_eligible"):
            self.pending = None
        await self.save()
        return self.plan

    async def apply(self, plan_id, automatic=False):
        if self.models.get("synthetic") and not self.controller.simulated(self.config):
            raise ServiceValidationError("Synthetic models cannot control a real heat source.")
        if self.plan and self.plan.get("model_fitted_at") != self.models.get("fitted_at"):
            raise ServiceValidationError("Model fit changed; refresh the plan.")
        if (
            not self.plan
            or self.plan["id"] != plan_id
            or self.plan.get("signature") != signature(self.config)
        ):
            raise ServiceValidationError("Plan is stale. Refresh the plan before applying.")
        if self.now() - timestamp(self.plan["created_at"]) > timedelta(minutes=20):
            raise ServiceValidationError("Plan/forecast is stale. Refresh it before applying.")
        if not self.plan.get("automatic_eligible") or not self.controller.allowed(self.config):
            raise ServiceValidationError(
                "Apply requires commissioned control and validated comfort/forecast evidence."
            )
        if (
            self.observation
            or self.baseline
            or self.controller.journal
            or (self.campaign and self.campaign["active"])
        ):
            raise ServiceValidationError("Another source operation or campaign is active.")
        self.mode = "auto" if automatic else "once"
        if self.pending and self.pending["plan_id"] == plan_id:
            await self.save()
            return
        self.baseline_settings = self.source_settings()
        self.pending = {
            "plan_id": plan_id,
            "signature": signature(self.config),
            "actions": deepcopy(self.plan["actions"]),
            "claimed": [],
        }
        await self.event(
            "Automatic planning enabled."
            if automatic
            else "One validated plan queued for bounded application."
        )

    def source_settings(self):
        """Persist commandable settings so manual changes during downtime cancel Auto."""
        source = source_config(self.config)
        entities = [source.get("climate"), source.get("flow_setter")]
        entities.extend(zone.get("climate") for zone in source["zones"])
        result = {}
        for entity in entities:
            if entity:
                state = self.hass.states.get(entity)
                result[entity] = (
                    [state.state, state.attributes.get("temperature")] if state else None
                )
        return result

    async def planner_tick(self):
        journal = self.controller.journal
        if journal and journal.get("purpose", "").startswith("planner"):
            fetched = self.forecast.get("fetched_at")
            if not fetched or self.now() - timestamp(fetched) >= timedelta(minutes=15):
                await self.fetch_forecast()
                if not self.forecast.get("available"):
                    self.mode, self.pending = "advisory", None
                    await self.controller.stop(self.manager.entry.entry_id)
                    await self.event(
                        "Forecast lost during a planned action; owned settings submitted for restoration."
                    )
            return
        if self.mode == "advisory":
            return
        if (
            self.observation
            or self.baseline
            or self.controller.journal
            or (self.campaign and self.campaign["active"])
        ):
            return
        if self.baseline_settings != self.source_settings():
            await self.external_override()
            return
        if self.mode == "auto" and (
            self.last_replan is None
            or self.now() - timestamp(self.last_replan) >= timedelta(minutes=15)
        ):
            plan = await self.replan()
            if plan.get("automatic_eligible"):
                self.pending = {
                    "plan_id": plan["id"],
                    "signature": signature(self.config),
                    "actions": deepcopy(plan["actions"]),
                    "claimed": [],
                }
        if not self.pending:
            return
        if (
            self.pending["signature"] != signature(self.config)
            or not self.plan
            or self.pending["plan_id"] != self.plan["id"]
            or self.plan.get("model_fitted_at") != self.models.get("fitted_at")
        ):
            self.mode, self.pending = "advisory", None
            await self.event("Queued plan cancelled because its configuration/forecast is stale.")
            return
        for index, action in enumerate(self.pending["actions"]):
            if index in self.pending["claimed"] or self.now() < timestamp(action["start"]):
                continue
            self.pending["claimed"].append(index)
            await self.save()
            if self.now() - timestamp(action["start"]) > timedelta(minutes=5):
                await self.event(
                    "Planned action missed; skipped rather than caught up after downtime."
                )
                continue
            try:
                pending_id = self.pending["plan_id"]
                await self.fetch_forecast()
                if (
                    not self.pending
                    or self.pending["plan_id"] != pending_id
                    or self.mode == "advisory"
                ):
                    return
                from .analysis.application import action_safe

                current = HeatSource(self.hass, self.config, self.now).sample()
                safe, reason = await self.hass.async_add_executor_job(
                    action_safe,
                    deepcopy(dict(self.config)),
                    self.models,
                    current,
                    self.forecast,
                    action,
                    self.now(),
                    self.hass.config.time_zone,
                )
                if not safe:
                    raise ServiceValidationError(reason)
                if action["kind"] == "pause":
                    minutes = int((timestamp(action["end"]) - self.now()).total_seconds() / 60)
                    if minutes < 1:
                        raise ServiceValidationError("Approved pause window expired.")
                    await self.run_once(minutes, purpose="planner")
                elif action["kind"] == "flow":
                    minutes = int((timestamp(action["end"]) - self.now()).total_seconds() / 60)
                    if minutes <= 0:
                        raise ServiceValidationError("Flow action window has expired.")
                    await self.controller.start(
                        self.manager,
                        minutes,
                        trial_flow=action["target"],
                        purpose="planner_flow",
                        flow_only=True,
                    )
            except HomeAssistantError as err:
                self.mode, self.pending = "advisory", None
                await self.event(f"Apply stopped safely: {err}")
            break
        if (
            self.pending
            and len(self.pending["claimed"]) == len(self.pending["actions"])
            and self.mode == "once"
        ):
            self.mode, self.pending = "advisory", None
            await self.save()

    async def tick(self, now=None):
        if self.lock.locked():
            return
        async with self.lock:
            await self.ingest_completed_control()
            await self.observe_tick()
            await self.baseline_tick()
            await self.campaign_tick()
            if self.controller:
                await self.planner_tick()

    async def observe_normal(self, minutes):
        if self.mode != "advisory" or (self.campaign and self.campaign["active"]):
            raise ServiceValidationError("Stop scheduled control before a read-only observation.")
        if self.observation or self.baseline or (self.controller and self.controller.journal):
            raise ServiceValidationError("A measurement or observation is already active.")
        if not 30 <= minutes <= 720:
            raise ServiceValidationError(
                "Choose a normal-operation observation of 30 to 720 minutes."
            )
        point = HeatSource(self.hass, self.config, self.now).sample()
        self.baseline = {
            "id": uuid4().hex,
            "started": self.now().isoformat(),
            "deadline": (self.now() + timedelta(minutes=minutes)).isoformat(),
            "configuration": deepcopy(dict(self.config)),
            "points": [dict(point, phase="normal operation")],
        }
        await self.event(
            "Read-only normal-operation observation started. No heating commands were sent."
        )

    async def baseline_tick(self):
        if not self.baseline:
            return
        data = self.baseline
        if self.now().timestamp() - data["points"][-1]["time"] >= 60:
            point = HeatSource(self.hass, data["configuration"], self.now).sample()
            point.pop("signals", None)  # Immutable reduced samples suffice for models/energy.
            data["points"].append(dict(point, phase="normal operation"))
            data["points"] = data["points"][-2000:]
            await self.save()
        if self.now() >= timestamp(data["deadline"]):
            await self.finish_baseline()

    async def finish_baseline(self, partial=False):
        data = self.baseline
        self.baseline = None
        points = data["points"]
        config = data["configuration"]
        valid = (
            not partial
            and all(
                p.get("heat") in (0, 1)
                and p.get("outdoor") is not None
                and not p.get("contaminated")
                and all(v is not None for v in p["rooms"].values())
                for p in points
            )
            and all(0 < b["time"] - a["time"] <= 600 for a, b in zip(points, points[1:]))
        )
        result = dict(
            data,
            ended=self.now().isoformat(),
            outcome="observed",
            reason="Read-only normal operation; "
            + ("observation stopped partial." if partial else "observation complete."),
            signature=signature(config),
            source_fingerprint=source_fingerprint(config),
            learning_eligible=valid,
            recovery={"complete": False, "rooms": {}},
            energy=energy_summary(points, config.get("energy", {})),
            flow_target=points[0].get("flow_target"),
            initial_heat=points[0].get("heat", 1),
            approved_flow_trial=False,
            purpose="passive",
            events=[],
        )
        self.runs.append(result)
        self.runs = self.runs[-MAX_RUNS:]
        await self.event(result["reason"])
