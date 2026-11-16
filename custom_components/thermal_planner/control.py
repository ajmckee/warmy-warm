"""One persisted control owner for the configured heat pump and direct-flow setter."""

import asyncio
from copy import deepcopy
from datetime import timedelta
from math import isfinite
from uuid import uuid4

from homeassistant.const import (
    EVENT_CALL_SERVICE,
    EVENT_HOMEASSISTANT_STARTED,
    EVENT_HOMEASSISTANT_STOP,
    EVENT_STATE_CHANGED,
)
from homeassistant.core import Context, CoreState, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .analysis import timestamp
from .analysis.signals import recovery_summary
from .configuration import source_config, source_fingerprint
from .const import DOMAIN
from .source import HeatSource, identity, resolve

# Keep the previous storage key so an interrupted v0.2 lab pause is reconciled.
CONTROL_KEY = f"{DOMAIN}_lab_control"
MAX_RETRIES = 3


class SourceController:
    def __init__(self, hass, now=None):
        self.hass = hass
        self.now = now or dt_util.utcnow
        self.store = Store(
            hass, 1, CONTROL_KEY, private=True, atomic_writes=True, serialize_in_event_loop=False
        )
        self.lock = asyncio.Lock()
        self.journal = None
        self.last_run = None
        self.commissioned = {}
        self.last_switch = None
        self.cancel = []
        self.context = Context()
        self.override_requested = False

    def config(self):
        if self.journal:
            return self.journal["guards"]
        managers = self.hass.data.get(DOMAIN, {})
        return (
            next(iter(managers.values())).config
            if managers
            else {"rooms": [], "max_age_minutes": 30}
        )

    def climate_entity(self):
        config = self.config()
        selected = source_config(config).get("climate")
        if self.journal and not selected:
            binding = identity(self.hass, self.journal["entity_id"])
            if binding and binding["platform"] == "fake_house":
                config["heat_source"] = {
                    "climate": self.journal["entity_id"],
                    "pause_limit_minutes": 60,
                }
                self.journal["source_binding"] = binding
                selected = self.journal["entity_id"]
        binding = (
            self.journal.get("source_binding") if self.journal else identity(self.hass, selected)
        )
        return resolve(self.hass, binding, selected)

    def simulated(self, config=None):
        config = config or self.config()
        binding = identity(self.hass, source_config(config).get("climate"))
        return bool(
            binding and binding["platform"] == "fake_house" and "fake_house" in self.hass.data
        )

    def allowed(self, config=None):
        config = config or self.config()
        selected = source_config(config).get("climate")
        binding = identity(self.hass, selected)
        return bool(
            binding
            and (
                self.simulated(config)
                or self.commissioned.get(source_fingerprint(config)) == binding
            )
        )

    def snapshot(self):
        config = self.config()
        return deepcopy(
            {
                "available": self.allowed(config),
                "configured": bool(source_config(config).get("climate")),
                "simulated": self.simulated(config),
                "source": source_config(config),
                "commissioned": self.allowed(config),
                "status": self.journal["phase"] if self.journal else "idle",
                "journal": {k: v for k, v in self.journal.items() if k not in ("points", "guards")}
                if self.journal
                else None,
                "last_run": {
                    k: v for k, v in self.last_run.items() if k not in ("points", "guards")
                }
                if self.last_run
                else None,
                "last_switch": self.last_switch,
            }
        )

    async def save(self):
        await self.store.async_save(
            {
                "journal": self.journal,
                "last_run": self.last_run,
                "commissioned": self.commissioned,
                "last_switch": self.last_switch,
            }
        )
        for entry_id in self.hass.data.get(DOMAIN, {}):
            async_dispatcher_send(self.hass, f"{DOMAIN}_{entry_id}")

    async def load(self):
        saved = await self.store.async_load() or {}
        self.journal, self.last_run = saved.get("journal"), saved.get("last_run")
        self.commissioned, self.last_switch = (
            saved.get("commissioned", {}),
            saved.get("last_switch"),
        )
        if self.journal and not self.journal["guards"].get("heat_source"):
            # The only pre-v0.3 controller was simulator-only. Recover its saved
            # entity, never infer a production source from an entity name.
            binding = identity(self.hass, self.journal["entity_id"])
            if binding and binding["platform"] == "fake_house":
                self.journal["source_binding"] = binding
                self.journal["guards"]["heat_source"] = {
                    "climate": self.journal["entity_id"],
                    "pause_limit_minutes": 60,
                }

    async def async_setup(self):
        await self.load()

        @callback
        def changed(event):
            if self.journal and not self.lock.locked():
                watched = {self.journal["entity_id"]}
                watched.update(
                    sensor for room in self.journal["guards"]["rooms"] for sensor in room["sensors"]
                )
                if self.journal.get("flow"):
                    watched.add(self.journal["flow"]["readback"])
                if event.data["entity_id"] in watched:
                    self.hass.async_create_task(self.monitor())

        @callback
        def command(event):
            if event.context.id == self.context.id:
                return
            domain, service = event.data["domain"], event.data["service"]
            climate = domain == "climate" and service in (
                "set_hvac_mode",
                "set_temperature",
                "turn_on",
                "turn_off",
                "toggle",
                "set_preset_mode",
            )
            source = source_config(self.config())
            flow = self.journal.get("flow") if self.journal else None
            number = (
                (flow or source.get("flow_setter"))
                and domain in ("number", "input_number")
                and service == "set_value"
            )
            if not climate and not number:
                return
            targets = event.data.get("service_data", {}).get("entity_id", [])
            targets = [targets] if isinstance(targets, str) else targets
            owned = (
                {self.climate_entity(), *(zone.get("climate") for zone in source["zones"])}
                if climate
                else {flow["setter"] if flow else source.get("flow_setter")}
            )
            owned.discard(None)
            if owned and (not targets or "all" in targets or owned.intersection(targets)):
                self.override_requested = bool(self.journal)
                for manager in self.hass.data.get(DOMAIN, {}).values():
                    if hasattr(manager, "engine"):
                        self.hass.async_create_task(manager.engine.external_override())
                if self.journal and not self.lock.locked():
                    self.hass.async_create_task(self.monitor())

        @callback
        def stopped(event):
            self.close()

        self.cancel.extend(
            [
                self.hass.bus.async_listen(EVENT_STATE_CHANGED, changed),
                self.hass.bus.async_listen(EVENT_CALL_SERVICE, command),
                self.hass.bus.async_listen(EVENT_HOMEASSISTANT_STOP, stopped),
                async_track_time_interval(self.hass, self.monitor, timedelta(seconds=2)),
            ]
        )
        if self.hass.state == CoreState.running:
            await self.monitor(restart=True)
        else:

            async def started(event):
                await self.monitor(restart=True)

            self.cancel.append(self.hass.bus.async_listen(EVENT_HOMEASSISTANT_STARTED, started))

    def close(self):
        for cancel in self.cancel:
            cancel()
        self.cancel.clear()

    def guard_reason(self, config, *, minimums=True):
        adapter = HeatSource(self.hass, config, self.now)
        if reason := adapter.disturbance_reason():
            return reason
        values = adapter.room_values()
        for room in config["rooms"]:
            if self.simulated(config):
                for sensor, weight in zip(room["sensors"], room["weights"]):
                    binding = identity(self.hass, sensor)
                    if weight > 0 and (not binding or binding["platform"] != "fake_house"):
                        return "Lab runs require only fake-house room temperature sensors."
            value = values[room["name"]]
            if value is None:
                return f"Missing or stale temperature in {room['name']}."
            minimum = room["comfort_min"]
            if (
                self.journal
                and self.journal.get("purpose", "").startswith("planner")
                and room.get("constrains", True)
            ):
                from .analysis.planning import target_at

                minimum = max(
                    minimum, target_at(config, room, self.now(), self.hass.config.time_zone)
                )
            if minimums and value <= minimum:
                return f"Minimum temperature reached in {room['name']}."
        return None

    def state(self):
        entity = self.climate_entity()
        if self.journal and entity and entity != self.journal["entity_id"]:
            self.journal["entity_id"] = entity
        state = self.hass.states.get(entity) if entity else None
        return (
            state if state is not None and state.state not in ("unknown", "unavailable") else None
        )

    def flow_value(self):
        flow = self.journal.get("flow") if self.journal else None
        if not flow:
            return None
        entity = resolve(self.hass, flow.get("readback_binding"), flow["readback"])
        flow["readback"] = entity or flow["readback"]
        return HeatSource(self.hass, self.journal["guards"], self.now).temp(entity)

    async def capture(self, phase=None, force=False):
        if not self.journal:
            return
        points = self.journal.setdefault("points", [])
        if not force and points and self.now().timestamp() - points[-1]["time"] < 60:
            return
        point = HeatSource(self.hass, self.journal["guards"], self.now).sample()
        point["phase"] = phase or (
            "recovery" if self.journal["phase"] == "recovering" else "cooldown"
        )
        points.append(point)
        self.journal["points"] = points[-2000:]
        await self.save()

    async def set_mode(self, mode):
        previous = self.state()
        reported = previous.last_reported if previous is not None else None
        self.journal["unacknowledged_mode"] = mode
        await self.save()
        await self.hass.services.async_call(
            "climate",
            "set_hvac_mode",
            {"entity_id": self.journal["entity_id"], "hvac_mode": mode},
            blocking=True,
            context=self.context,
        )
        await asyncio.sleep(0)
        self.last_switch = self.now().isoformat()
        attempts = 1 if self.simulated(self.journal["guards"]) else 30
        for attempt in range(attempts):
            state = self.state()
            if (
                state is not None
                and state.state == mode
                and (
                    self.simulated(self.journal["guards"])
                    or reported is None
                    or state.last_reported > reported
                )
            ):
                self.journal["unacknowledged_mode"] = None
                await self.save()
                return
            if attempt < attempts - 1:
                await asyncio.sleep(0.5)
        raise HomeAssistantError("Climate command readback timed out")

    async def set_flow(self, value):
        flow = self.journal["flow"]
        entity = resolve(self.hass, flow.get("setter_binding"), flow["setter"])
        if entity is None:
            raise HomeAssistantError("Flow setter unavailable")
        flow["setter"] = entity
        before = self.hass.states.get(flow["readback"])
        reported = before.last_reported if before is not None else None
        root_before = self.state()
        root_reported = root_before.last_reported if root_before is not None else None
        flow["write_pending"] = value
        await self.save()
        await self.hass.services.async_call(
            entity.split(".")[0],
            "set_value",
            {"entity_id": entity, "value": value},
            blocking=True,
            context=self.context,
        )
        await asyncio.sleep(0)
        attempts = 1 if self.simulated(self.journal["guards"]) else 30
        for attempt in range(attempts):
            reading = self.flow_value()
            state = self.hass.states.get(flow["readback"])
            fresh = (
                self.simulated(self.journal["guards"])
                or reported is None
                or (state is not None and state.last_reported > reported)
            )
            if flow.get("target_follows_flow"):
                root = self.state()
                expected = (
                    value * 9 / 5 + 32
                    if str(self.hass.config.units.temperature_unit) == "°F"
                    else value
                )
                fresh = (
                    fresh
                    and root is not None
                    and abs(root.attributes.get("temperature", float("inf")) - expected) <= 0.1
                    and (
                        self.simulated(self.journal["guards"])
                        or root_reported is None
                        or root.last_reported > root_reported
                    )
                )
            if reading is not None and abs(reading - value) <= 0.1 and fresh:
                flow["write_pending"] = None
                await self.save()
                return
            if attempt < attempts - 1:
                await asyncio.sleep(0.5)
        raise HomeAssistantError("Flow readback failed")

    async def start(
        self,
        manager,
        minutes,
        *,
        commissioning=False,
        trial_flow=None,
        purpose="manual",
        flow_only=False,
    ):
        async with self.lock:
            if self.journal:
                raise ServiceValidationError(
                    "A run already owns the heat source or needs restoration."
                )
            config = deepcopy(dict(manager.config))
            source = source_config(config)
            limit = source["recovery_minutes"] if flow_only else source["pause_limit_minutes"]
            if not isinstance(minutes, int) or not 1 <= minutes <= limit:
                raise ServiceValidationError("Pause exceeds the configured heat-source limit.")
            if not self.allowed(config):
                if (
                    not commissioning
                    or not source.get("commissioning_allowed")
                    or not identity(self.hass, source.get("climate"))
                ):
                    raise ServiceValidationError(
                        "Select a registered heat source and complete supervised commissioning; development control requires a registered simulator."
                    )
            if (
                self.last_switch
                and not commissioning
                and self.now()
                < timestamp(self.last_switch) + timedelta(minutes=source["dwell_minutes"])
            ):
                raise ServiceValidationError("Heat-source minimum dwell time has not elapsed.")
            state = self.state()
            if (
                state is None
                or state.state != "heat"
                or not {"heat", "off"} <= set(state.attributes.get("hvac_modes", []))
            ):
                raise ServiceValidationError(
                    "Start with an available heat source in Heat mode, with OFF supported."
                )
            target = state.attributes.get("temperature")
            if not isinstance(target, (int, float)) or not isfinite(target):
                raise ServiceValidationError(
                    "A valid target temperature is required for restore verification."
                )
            if reason := self.guard_reason(config):
                raise ServiceValidationError(reason)
            adapter = HeatSource(self.hass, config, self.now)
            flow = None
            if trial_flow is not None:
                adapter.validate_capabilities()
                previous = adapter.flow()
                if (
                    commissioning
                    or not source.get("flow_setter")
                    or previous is None
                    or not source["flow_min"] <= trial_flow <= source["flow_max"]
                    or abs(trial_flow - previous) > source["flow_max_step"]
                ):
                    raise ServiceValidationError(
                        "Flow trial exceeds the configured direct-mode bounds/step or lacks readback."
                    )
                flow = {
                    "setter": source["flow_setter"],
                    "setter_binding": identity(self.hass, source["flow_setter"]),
                    "readback": source["flow_readback"],
                    "readback_binding": identity(self.hass, source["flow_readback"]),
                    "previous": previous,
                    "applied": trial_flow,
                    "restored": False,
                    "root_previous": previous * 9 / 5 + 32
                    if str(self.hass.config.units.temperature_unit) == "°F"
                    else previous,
                    "root_applied": trial_flow * 9 / 5 + 32
                    if str(self.hass.config.units.temperature_unit) == "°F"
                    else trial_flow,
                }
                flow["target_follows_flow"] = source.get("climate_target_is_flow", False)
            self.override_requested = False
            self.context = Context()
            self.journal = {
                "run_id": uuid4().hex,
                "entry_id": manager.entry.entry_id,
                "entity_id": state.entity_id,
                "source_binding": identity(self.hass, state.entity_id),
                "started": self.now().isoformat(),
                "deadline": (self.now() + timedelta(minutes=minutes)).isoformat(),
                "previous_mode": state.state,
                "previous_target": target,
                "applied_target": target,
                "intended_mode": "off",
                "phase": "prepared",
                "attempts": 0,
                "reason": None,
                "guards": config,
                "events": [],
                "points": [],
                "starting": adapter.room_values(),
                "initial_heat": 1,
                "flow_target": trial_flow if flow else adapter.flow(),
                "flow": flow,
                "flow_only": flow_only,
                "commissioning": commissioning,
                "purpose": purpose,
            }
            await self.record(
                "Original settings, intended writes and absolute restore deadline saved."
            )
            try:
                current = self.state()
                if (
                    self.override_requested
                    or current is None
                    or current.state != state.state
                    or current.attributes.get("temperature") != target
                ):
                    await self.complete(
                        "external_override", "Climate changed before pause; control released."
                    )
                    raise ServiceValidationError(
                        "Climate changed before pause; no OFF command sent."
                    )
                if flow:
                    await self.set_flow(trial_flow)
                    self.journal["applied_target"] = (
                        flow["root_applied"]
                        if flow["target_follows_flow"]
                        else self.state().attributes.get("temperature")
                    )
                    await self.record("Bounded trial flow accepted and read back before cooldown.")
                if flow_only:
                    if not flow:
                        raise ServiceValidationError(
                            "Flow-only operation requires a configured direct flow target."
                        )
                    self.journal.update(
                        phase="recovering",
                        resumed_at=self.now().isoformat(),
                        recovery_deadline=self.journal["deadline"],
                        intended_mode="heat",
                    )
                    await self.capture("recovery", force=True)
                    await self.record(
                        "Bounded flow-only observation started; no OFF command was issued."
                    )
                    return
                await self.set_mode("off")
                readback = self.state()
                if readback is None or readback.state != "off":
                    raise HomeAssistantError("OFF readback failed.")
                self.journal["phase"] = "paused"
                self.journal["off_verified"] = True
                await self.capture(force=True)
                await self.record(
                    "Climate OFF confirmed; selected delivery telemetry is recorded separately."
                )
                if reason := self.guard_reason(config):
                    await self.restore(reason)
            except HomeAssistantError as err:
                if self.journal:
                    await self.restore(f"Pause failed: {err}")
                raise ServiceValidationError("Pause failed; inspect restoration status.") from err

    async def record(self, message):
        self.journal["events"].append({"at": self.now().isoformat(), "message": message})
        await self.save()

    async def complete(self, outcome, reason):
        if self.journal.get("points"):
            await self.capture("finished", force=True)
        self.journal.update(outcome=outcome, reason=reason, ended=self.now().isoformat())
        if (
            outcome == "restored"
            and self.journal.get("commissioning")
            and self.journal.get("off_verified")
        ):
            self.commissioned[source_fingerprint(self.journal["guards"])] = self.journal[
                "source_binding"
            ]
        await self.record(reason)
        self.last_run = deepcopy(self.journal)
        self.journal = None
        await self.save()

    async def restore(self, reason):
        if self.override_requested:
            await self.complete(
                "external_override", "External control command; ownership released."
            )
            return
        state = self.state()
        if state is None:
            self.journal.update(phase="waiting_for_entity", reason=reason)
            await self.save()
            return
        target = state.attributes.get("temperature")
        if not isinstance(target, (int, float)) or not isfinite(target):
            self.journal.update(
                phase="waiting_for_entity", reason="Waiting for valid climate target readback."
            )
            await self.save()
            return
        flow = self.journal.get("flow")
        expected_targets = {
            self.journal["previous_target"],
            self.journal.get("applied_target", self.journal["previous_target"]),
        }
        if flow and self.journal["phase"] in ("prepared", "restoring", "restore_failed"):
            expected_targets.add(flow.get("root_applied", flow["applied"]))
        if target not in expected_targets:
            await self.complete("external_override", "Target changed externally; control released.")
            return
        if state.state not in ("off", self.journal["previous_mode"]):
            await self.complete("external_override", "Mode changed externally; control released.")
            return
        if flow and not flow["restored"]:
            value = self.flow_value()
            if value is None:
                self.journal.update(
                    phase="waiting_for_entity", reason="Waiting for flow readback before restore."
                )
                await self.save()
                if state.state == "off" and self.journal["attempts"] < MAX_RETRIES:
                    self.journal["attempts"] += 1
                    await self.record("Restoring Heat mode while flow readback is unavailable.")
                    try:
                        await self.set_mode(self.journal["previous_mode"])
                    except HomeAssistantError as err:
                        self.journal.update(phase="restore_failed", error=str(err))
                        await self.save()
                return
            if abs(value - flow["previous"]) <= 0.1 and flow.get("write_pending") is None:
                flow["restored"] = True
            elif abs(value - flow["applied"]) > 0.1 and abs(value - flow["previous"]) > 0.1:
                await self.complete(
                    "external_override", "Flow changed externally; control released."
                )
                return
        if (
            (not flow or flow["restored"])
            and state.state == self.journal["previous_mode"]
            and target == self.journal["previous_target"]
            and self.journal.get("unacknowledged_mode") in (None, self.journal["previous_mode"])
        ):
            await self.complete("restored", reason)
            return
        if self.journal["attempts"] >= MAX_RETRIES:
            return
        self.journal.update(phase="restoring", reason=reason)
        self.journal["attempts"] += 1
        await self.record(f"Restore attempt {self.journal['attempts']} saved before command.")
        try:
            if flow and not flow["restored"]:
                await self.set_flow(flow["previous"])
                flow["restored"] = True
                await self.save()
            if self.state().state != self.journal["previous_mode"] or self.journal.get(
                "unacknowledged_mode"
            ):
                await self.set_mode(self.journal["previous_mode"])
            readback = self.state()
            if (
                readback is None
                or readback.state != self.journal["previous_mode"]
                or readback.attributes.get("temperature") != self.journal["previous_target"]
            ):
                raise HomeAssistantError("Restore readback failed.")
            await self.complete("restored", reason)
        except HomeAssistantError as err:
            self.journal.update(phase="restore_failed", error=str(err))
            await self.save()

    async def resume_trial(self, reason):
        try:
            self.journal["phase"] = "resuming"
            await self.record(
                "Resume at trial flow saved; original flow remains owned until observation ends."
            )
            await self.set_mode("heat")
            if self.state() is None or self.state().state != "heat":
                raise HomeAssistantError("Resume readback failed")
            self.journal.update(
                phase="recovering",
                resumed_at=self.now().isoformat(),
                recovery_deadline=(
                    self.now()
                    + timedelta(minutes=source_config(self.journal["guards"])["recovery_minutes"])
                ).isoformat(),
            )
            await self.capture("recovery", force=True)
            await self.record(reason)
        except HomeAssistantError:
            await self.restore("Trial resume failed; restoring original settings.")

    async def stop(self, entry_id):
        async with self.lock:
            if self.journal:
                if entry_id != self.journal["entry_id"]:
                    raise ServiceValidationError("The active run belongs to another configuration.")
                self.journal["attempts"] = 0
                await self.restore("Stopped by user.")

    async def monitor(self, now=None, *, restart=False):
        if self.lock.locked() or self.journal is None:
            return
        async with self.lock:
            state = self.state()
            if self.override_requested:
                await self.complete(
                    "external_override", "External control command; ownership released."
                )
            elif restart or self.journal["phase"] not in ("paused", "recovering"):
                await self.restore(
                    self.journal.get("reason") or "Interrupted run restored after restart."
                )
            elif state is None:
                await self.restore("Climate became unavailable.")
            elif state.state != (
                "heat" if self.journal["phase"] == "recovering" else "off"
            ) or state.attributes.get("temperature") != self.journal.get(
                "applied_target", self.journal["previous_target"]
            ):
                await self.complete(
                    "external_override", "Climate changed externally; control released."
                )
            elif self.journal.get("flow") and self.flow_value() is None:
                await self.restore("Flow readback became unavailable or stale.")
            elif (
                self.journal.get("flow")
                and self.flow_value() is not None
                and abs(self.flow_value() - self.journal["flow"]["applied"]) > 0.1
            ):
                await self.complete(
                    "external_override", "Flow changed externally; control released."
                )
            else:
                await self.capture()
                if self.journal["phase"] == "recovering":
                    recovery = recovery_summary(
                        self.journal["points"],
                        self.journal["guards"]["rooms"],
                        self.journal["starting"],
                        source_config(self.journal["guards"])["recovery_tolerance_c"],
                    )
                    if (
                        recovery["complete"] and not self.journal.get("flow_only")
                    ) or self.now() >= timestamp(self.journal["recovery_deadline"]):
                        self.journal["recovery"] = recovery
                        await self.restore(
                            "Flow trial recovery observation complete."
                            if recovery["complete"]
                            else "Flow trial recovery observation ended partial."
                        )
                    elif reason := self.guard_reason(self.journal["guards"]):
                        await self.restore(f"Recovery safety cutoff: {reason}")
                else:
                    reason = (
                        "Pause deadline reached."
                        if self.now() >= timestamp(self.journal["deadline"])
                        else self.guard_reason(self.journal["guards"])
                    )
                    if reason:
                        if self.journal.get("flow"):
                            await self.resume_trial(reason)
                        else:
                            await self.restore(reason)


# Compatibility for the v0.2 lab test/import surface. No entity is a runtime default.
LabController = SourceController
