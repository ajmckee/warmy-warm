"""Native configuration, room editing and historical replay dialogs."""

import logging
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import floor_registry, selector

from .analysis import timestamp, validate_config
from .configuration import source_config, validate_settings
from .const import DOMAIN
from .source import HeatSource

_LOGGER = logging.getLogger(__name__)


def entity(domain="sensor", multiple=False):
    return selector.EntitySelector(selector.EntitySelectorConfig(domain=domain, multiple=multiple))


def local_timestamp(value, zone):
    dt = datetime.fromisoformat(value)
    if dt.tzinfo:
        return timestamp(dt)
    tz = ZoneInfo(zone)
    candidates = {
        dt.replace(tzinfo=tz, fold=fold).astimezone(timezone.utc)
        for fold in (0, 1)
        if dt.replace(tzinfo=tz, fold=fold)
        .astimezone(timezone.utc)
        .astimezone(tz)
        .replace(tzinfo=None)
        == dt
    }
    if len(candidates) != 1:
        raise ValueError(
            "Ambiguous or skipped local time; use the replay action with explicit UTC offsets"
        )
    return candidates.pop()


class ConfigurationSteps:
    """Shared setup/options forms; all persisted values are JSON serializable."""

    async def async_step_settings(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                outdoor = user_input.get("outdoor")
                if outdoor:
                    self.check_units([outdoor])
                self.settings.update(user_input)
                self.settings["outdoor"] = user_input.get("outdoor")
                self.settings["weather"] = user_input.get("weather")
                self.settings.setdefault("outdoor", None)
                self.settings.setdefault("weather", None)
                self.settings.setdefault("rooms", deepcopy(self.rooms))
                return await self.async_step_finish()
            except ValueError:
                errors["base"] = "invalid_units"
        schema = {
            vol.Required(
                "history_policy", default=self.settings.get("history_policy", "until_changed")
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        {
                            "value": "until_changed",
                            "label": "Keep the last known reading until it changes or becomes unavailable",
                        },
                        {
                            "value": "max_age",
                            "label": "Stop using readings older than the freshness limit",
                        },
                    ]
                )
            ),
            vol.Optional(
                "outdoor", description={"suggested_value": self.settings.get("outdoor")}
            ): entity(),
            vol.Optional(
                "weather", description={"suggested_value": self.settings.get("weather")}
            ): entity("weather"),
            vol.Required(
                "max_age_minutes", default=self.settings.get("max_age_minutes", 30)
            ): selector.NumberSelector(selector.NumberSelectorConfig(min=1, max=1440, mode="box")),
        }
        return self.async_show_form(
            step_id="settings", data_schema=vol.Schema(schema), errors=errors
        )

    async def async_step_heat_source(self, user_input=None):
        old = source_config(self.settings)
        errors = {}
        if user_input is not None:
            values = dict(user_input)
            values.update(values.pop("advanced", {}))
            source = dict(old, **values)
            for key in (
                "climate",
                "space_heating",
                "operating_mode",
                "defrost_indicator",
                "leaving_water",
                "flow_readback",
                "flow_setter",
            ):
                source[key] = values.get(key)
            for key in ("dhw_states", "defrost_states"):
                source[key] = [
                    item.strip() for item in values.get(key, "").split(",") if item.strip()
                ]
            for key in ("pause_limit_minutes", "dwell_minutes", "recovery_minutes"):
                source[key] = int(source[key])
            try:
                candidate = dict(self.settings, rooms=self.rooms, heat_source=source)
                validate_settings(candidate)
                HeatSource(
                    self.hass, candidate, lambda: datetime.now(timezone.utc)
                ).validate_capabilities()
                self.settings["heat_source"] = source
                return await self.async_step_finish()
            except ValueError:
                errors["base"] = "invalid_heat_source"
                old = source

        def optional(key):
            return vol.Optional(key, description={"suggested_value": old.get(key)})

        advanced = {
            optional("space_heating"): entity("binary_sensor"),
            optional("defrost_indicator"): entity(["binary_sensor", "sensor"]),
            optional("operating_mode"): entity(["sensor", "select"]),
            vol.Optional("dhw_states", default=",".join(old.get("dhw_states", []))): str,
            vol.Optional("defrost_states", default=",".join(old.get("defrost_states", []))): str,
            optional("leaving_water"): entity(),
            optional("flow_readback"): entity(["sensor", "number", "input_number"]),
            optional("flow_setter"): entity(["number", "input_number"]),
            vol.Optional(
                "direct_mode_confirmed", default=old.get("direct_mode_confirmed", False)
            ): bool,
            vol.Optional(
                "climate_target_is_flow", default=old.get("climate_target_is_flow", False)
            ): bool,
            vol.Optional("disturbances", default=old.get("disturbances", [])): entity(
                "binary_sensor", multiple=True
            ),
            vol.Required(
                "pause_limit_minutes", default=old["pause_limit_minutes"]
            ): selector.NumberSelector(selector.NumberSelectorConfig(min=1, max=240, mode="box")),
            vol.Required("dwell_minutes", default=old["dwell_minutes"]): selector.NumberSelector(
                selector.NumberSelectorConfig(min=1, max=240, mode="box")
            ),
            vol.Required(
                "recovery_minutes", default=old["recovery_minutes"]
            ): selector.NumberSelector(selector.NumberSelectorConfig(min=1, max=720, mode="box")),
            vol.Required(
                "recovery_tolerance_c", default=old["recovery_tolerance_c"]
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(min=0, max=2, step=0.1, mode="box")
            ),
            vol.Optional("commissioning_allowed", default=old["commissioning_allowed"]): bool,
        }
        for key in ("flow_min", "flow_max", "flow_max_step"):
            advanced[optional(key)] = selector.NumberSelector(
                selector.NumberSelectorConfig(min=0.1, max=90, step=0.1, mode="box")
            )
        return self.async_show_form(
            step_id="heat_source",
            data_schema=vol.Schema(
                {
                    vol.Required("name", default=old["name"]): str,
                    optional("climate"): entity("climate"),
                    vol.Required("advanced"): section(vol.Schema(advanced), {"collapsed": True}),
                }
            ),
            errors=errors,
        )

    async def async_step_zones(self, user_input=None):
        source = source_config(self.settings)
        self.editing_zone = None
        return self.async_show_menu(
            step_id="zones",
            menu_options=[
                "zone",
                "edit_zone",
                "remove_zone",
                "init" if hasattr(self, "entry_id") else "rooms",
            ],
            description_placeholders={
                "zone_list": "\n".join(f"- {z['name']}" for z in source["zones"])
            },
        )

    async def async_step_zone(self, user_input=None):
        source = source_config(self.settings)
        old = getattr(self, "editing_zone", None) or {}
        errors = {}
        if user_input is not None:
            zone = dict(user_input, id=old.get("id") or uuid4().hex[:12])
            zones = (
                [zone if z["id"] == old.get("id") else z for z in source["zones"]]
                if old
                else [*source["zones"], zone]
            )
            try:
                validate_settings(
                    dict(self.settings, rooms=self.rooms, heat_source=dict(source, zones=zones))
                )
                for key in ("supply", "return"):
                    if zone.get(key):
                        self.check_units([zone[key]])
                self.settings["heat_source"] = dict(source, zones=zones)
                return await self.async_step_zones()
            except ValueError:
                errors["base"] = "invalid_zone"
        schema = {vol.Required("name", default=old.get("name", "")): str}
        for key, domain in (
            ("climate", "climate"),
            ("supply", "sensor"),
            ("return", "sensor"),
            ("pump", "binary_sensor"),
        ):
            schema[vol.Optional(key, description={"suggested_value": old.get(key)})] = entity(
                domain
            )
        return self.async_show_form(step_id="zone", data_schema=vol.Schema(schema), errors=errors)

    async def async_step_edit_zone(self, user_input=None):
        if user_input:
            self.editing_zone = next(
                z for z in source_config(self.settings)["zones"] if z["id"] == user_input["zone"]
            )
            return await self.async_step_zone()
        return self.zone_picker("edit_zone")

    async def async_step_remove_zone(self, user_input=None):
        errors = {}
        source = source_config(self.settings)
        if user_input:
            used = any(
                room.get("zone", source["zones"][0]["id"]) == user_input["zone"]
                for room in self.rooms
            )
            if len(source["zones"]) > 1 and not used:
                self.settings["heat_source"] = dict(
                    source, zones=[z for z in source["zones"] if z["id"] != user_input["zone"]]
                )
                return await self.async_step_zones()
            errors["base"] = "zone_in_use"
        return self.zone_picker("remove_zone", errors)

    def zone_picker(self, step, errors=None):
        return self.async_show_form(
            step_id=step,
            data_schema=vol.Schema(
                {
                    vol.Required("zone"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                {"value": z["id"], "label": z["name"]}
                                for z in source_config(self.settings)["zones"]
                            ]
                        )
                    )
                }
            ),
            errors=errors or {},
        )

    async def async_step_energy(self, user_input=None):
        old = self.settings.get("energy", {})
        errors = {}
        if user_input is not None:
            energy = {}
            for name in ("electrical", "thermal"):
                data = user_input.get(name, {})
                if data.get("entity"):
                    energy[name] = data
            try:
                candidate = dict(self.settings, rooms=self.rooms, energy=energy)
                validate_settings(candidate)
                HeatSource(
                    self.hass, candidate, lambda: datetime.now(timezone.utc)
                ).validate_capabilities()
                self.settings["energy"] = energy
                return await self.async_step_finish()
            except ValueError:
                errors["base"] = "invalid_energy"
        schema = {}
        for name in ("electrical", "thermal"):
            values = old.get(name, {})
            schema[vol.Required(name)] = section(
                vol.Schema(
                    {
                        vol.Optional(
                            "entity", description={"suggested_value": values.get("entity")}
                        ): entity(),
                        vol.Required(
                            "kind", default=values.get("kind", "power")
                        ): selector.SelectSelector(
                            selector.SelectSelectorConfig(
                                options=[
                                    {"value": "power", "label": "Instantaneous power (W or kW)"},
                                    {"value": "counter", "label": "Cumulative energy (Wh or kWh)"},
                                ]
                            )
                        ),
                        vol.Required(
                            "scope", default=values.get("scope", "heat_pump")
                        ): selector.SelectSelector(
                            selector.SelectSelectorConfig(
                                options=[
                                    {"value": "space_heating", "label": "Space heating only"},
                                    {
                                        "value": "heat_pump",
                                        "label": "Whole heat pump, including DHW",
                                    },
                                    {
                                        "value": "wider",
                                        "label": "Wider electrical circuit / mixed scope",
                                    },
                                ]
                            )
                        ),
                    }
                ),
                {"collapsed": not bool(values)},
            )
        return self.async_show_form(step_id="energy", data_schema=vol.Schema(schema), errors=errors)

    async def async_step_campaign_settings(self, user_input=None):
        old = self.settings.get("campaign", {})
        errors = {}
        if user_input is not None:
            try:
                settings = dict(user_input)
                settings["nights"] = int(settings["nights"])
                datetime.fromisoformat(settings["end_date"])
                datetime.strptime(settings["start_time"][:5], "%H:%M")
                settings["pause_minutes"] = int(settings["pause_minutes"])
                if settings["pause_minutes"] > source_config(self.settings)["pause_limit_minutes"]:
                    raise ValueError("Campaign pause exceeds source limit")
                bounds = [
                    int(v.strip())
                    for v in settings.pop("report_windows", "0,60,120,240").split(",")
                ]
                drops = [
                    float(v.strip()) for v in settings.pop("drop_thresholds", "0.5,1").split(",")
                ]
                validate_settings(
                    dict(
                        self.settings,
                        rooms=self.rooms,
                        report_windows_minutes=bounds,
                        drop_thresholds=drops,
                    )
                )
                self.settings.update(report_windows_minutes=bounds, drop_thresholds=drops)
                self.settings["campaign"] = settings
                return await self.async_step_finish()
            except ValueError:
                errors["base"] = "invalid_campaign"
        return self.async_show_form(
            step_id="campaign_settings",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "start_time", default=old.get("start_time", "02:00:00")
                    ): selector.TimeSelector(),
                    vol.Required("nights", default=old.get("nights", 5)): selector.NumberSelector(
                        selector.NumberSelectorConfig(min=1, max=90, mode="box")
                    ),
                    vol.Required(
                        "end_date",
                        default=old.get(
                            "end_date",
                            (datetime.now(timezone.utc) + timedelta(days=14)).date().isoformat(),
                        ),
                    ): selector.DateSelector(),
                    vol.Optional(
                        "report_windows",
                        default=",".join(
                            str(v)
                            for v in self.settings.get("report_windows_minutes", [0, 60, 120, 240])
                        ),
                    ): str,
                    vol.Optional(
                        "drop_thresholds",
                        default=",".join(
                            str(v) for v in self.settings.get("drop_thresholds", [0.5, 1])
                        ),
                    ): str,
                    vol.Required(
                        "pause_minutes",
                        default=old.get(
                            "pause_minutes", source_config(self.settings)["pause_limit_minutes"]
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(min=1, max=240, mode="box")
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_comfort_blocks(self, user_input=None):
        if user_input is not None:
            if user_input.get("remove"):
                self.settings["comfort_blocks"] = [
                    b
                    for b in self.settings.get("comfort_blocks", [])
                    if b["id"] != user_input["remove"]
                ]
            else:
                block = dict(user_input, id=uuid4().hex)
                block["days"] = [int(day) for day in block["days"]]
                candidate = dict(
                    self.settings,
                    rooms=self.rooms,
                    comfort_blocks=[*self.settings.get("comfort_blocks", []), block],
                )
                try:
                    validate_settings(candidate)
                except ValueError:
                    self.comfort_errors = {"base": "invalid_comfort_block"}
                    return await self.async_step_comfort_blocks()
                self.comfort_errors = {}
                self.settings["comfort_blocks"] = candidate["comfort_blocks"]
            return await self.async_step_comfort_schedule()
        return self.async_show_form(
            step_id="comfort_blocks",
            errors=getattr(self, "comfort_errors", {}),
            data_schema=vol.Schema(
                {
                    vol.Required("room"): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=[r["name"] for r in self.rooms])
                    ),
                    vol.Required(
                        "days", default=[str(i) for i in range(7)]
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                {"value": str(i), "label": day}
                                for i, day in enumerate(
                                    [
                                        "Monday",
                                        "Tuesday",
                                        "Wednesday",
                                        "Thursday",
                                        "Friday",
                                        "Saturday",
                                        "Sunday",
                                    ]
                                )
                            ],
                            multiple=True,
                        )
                    ),
                    vol.Required("start", default="06:00:00"): selector.TimeSelector(),
                    vol.Required("end", default="22:00:00"): selector.TimeSelector(),
                    vol.Required("target_c", default=20): selector.NumberSelector(
                        selector.NumberSelectorConfig(min=5, max=35, step=0.5, mode="box")
                    ),
                }
            ),
        )

    async def async_step_comfort_schedule(self, user_input=None):
        return self.async_show_menu(
            step_id="comfort_schedule",
            menu_options=[
                "comfort_blocks",
                *(["remove_comfort_block"] if self.settings.get("comfort_blocks") else []),
                "init",
            ],
            description_placeholders={
                "blocks": "\n".join(
                    f"- {b['room']}: {b['start']}–{b['end']}, {b['target_c']}°C"
                    for b in self.settings.get("comfort_blocks", [])
                )
                or "No time blocks; room comfort targets apply throughout the day."
            },
        )

    async def async_step_remove_comfort_block(self, user_input=None):
        if user_input:
            return await self.async_step_comfort_blocks(user_input)
        return self.async_show_form(
            step_id="remove_comfort_block",
            data_schema=vol.Schema(
                {
                    vol.Required("remove"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                {"value": b["id"], "label": f"{b['room']} {b['start']}–{b['end']}"}
                                for b in self.settings.get("comfort_blocks", [])
                            ]
                        )
                    )
                }
            ),
        )

    def check_units(self, entities):
        for name in entities:
            state = self.hass.states.get(name)
            if state is None or state.attributes.get("unit_of_measurement") not in (
                "°C",
                "°F",
                "K",
            ):
                raise ValueError("Select an available temperature entity with supported units")

    async def async_step_room(self, user_input=None):
        errors = {}
        old = getattr(self, "editing", None) or {}
        values = dict(old)
        if user_input is not None:
            submitted = dict(user_input)
            submitted.update(submitted.pop("advanced", {}))
            values.update(submitted)
            try:
                room = dict(
                    floor="",
                    circuit="",
                    emitter="radiator",
                    diagnostics=[],
                    floor_weight=1,
                    comfort_min=18,
                    constrains=True,
                )
                room.update(old)
                room.update(submitted)
                room["name"] = room["name"].strip()
                # Omitted optional floor means no floor, including when clearing it.
                room["floor"] = submitted.get("floor", "").strip()
                room["circuit"] = room.get("circuit", "").strip()
                room["humidity"] = submitted.get("humidity")
                room["radiant_temperature"] = submitted.get("radiant_temperature")
                if room["radiant_temperature"]:
                    self.check_units([room["radiant_temperature"]])
                zones = source_config(self.settings)["zones"]
                room["zone"] = room.get("zone") or zones[0]["id"]
                room["comfort_assumptions"] = {
                    key: room.pop(
                        "assumed_" + key, room.get("comfort_assumptions", {}).get(key, default)
                    )
                    for key, default in (("met", 1.1), ("clo", 1.0), ("air_speed", 0.1))
                }
                if room.get("humidity"):
                    state = self.hass.states.get(room["humidity"])
                    if state and state.attributes.get("unit_of_measurement") != "%":
                        raise ValueError("Humidity must use % units")
                self.check_units(room["sensors"] + room.get("diagnostics", []))
                weights = room.pop("relative_weights", "").strip()
                old_weights = ",".join(str(w) for w in old.get("weights", []))
                if old and weights == old_weights and room["sensors"] != old["sensors"]:
                    # Hidden defaults must not attach a previous sensor's weight
                    # to a different sensor after a selection is changed/reordered.
                    by_sensor = dict(zip(old["sensors"], old["weights"]))
                    room["weights"] = [by_sensor.get(sensor, 1.0) for sensor in room["sensors"]]
                else:
                    room["weights"] = (
                        [float(w.strip()) for w in weights.split(",")]
                        if weights
                        else [1.0] * len(room["sensors"])
                    )
                if any(r["name"] == room["name"] and r is not old for r in self.rooms):
                    raise ValueError("Room names must be unique")
                rooms = (
                    [room if r is old else r for r in self.rooms] if old else [*self.rooms, room]
                )
                blocks = [
                    dict(b, room=room["name"]) if old and b["room"] == old["name"] else b
                    for b in self.settings.get("comfort_blocks", [])
                ]
                validate_config(dict(self.settings, rooms=rooms, comfort_blocks=blocks))
                if blocks:
                    self.settings["comfort_blocks"] = blocks
                self.rooms = rooms
                self.editing = None
                return await self.async_step_finish()
            except ValueError, KeyError:
                errors["base"] = "invalid_room"

        def required(key, default=None):
            return (
                vol.Required(key, default=values.get(key, default))
                if key in values or default is not None
                else vol.Required(key)
            )

        floors = sorted(
            {floor.name for floor in floor_registry.async_get(self.hass).floors.values()}
        )
        # Keep existing custom floor labels editable, including installations
        # created before native floors were available. No registry writes.
        if values.get("floor") and values["floor"] not in floors:
            floors.append(values["floor"])
        floor_selector = (
            selector.SelectSelector(selector.SelectSelectorConfig(options=floors, mode="dropdown"))
            if floors
            else selector.TextSelector()
        )
        schema = {
            required("name"): str,
            required("sensors"): entity(multiple=True),
            vol.Optional(
                "floor", description={"suggested_value": values.get("floor") or None}
            ): floor_selector,
            required(
                "comfort_target", max(20, values.get("comfort_min", 18))
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(min=-10, max=40, step=0.5, mode="box")
            ),
            required("comfort_min", 18): selector.NumberSelector(
                selector.NumberSelectorConfig(min=-10, max=40, mode="box")
            ),
            vol.Required("advanced"): section(
                vol.Schema(
                    {
                        vol.Optional("circuit", default=values.get("circuit", "")): str,
                        vol.Optional(
                            "humidity", description={"suggested_value": values.get("humidity")}
                        ): entity(),
                        vol.Optional(
                            "radiant_temperature",
                            description={"suggested_value": values.get("radiant_temperature")},
                        ): entity(),
                        vol.Optional(
                            "comfort_estimate", default=values.get("comfort_estimate", False)
                        ): bool,
                        vol.Required(
                            "assumed_met",
                            default=values.get("comfort_assumptions", {}).get("met", 1.1),
                        ): selector.NumberSelector(
                            selector.NumberSelectorConfig(min=0.81, max=3.99, step=0.1, mode="box")
                        ),
                        vol.Required(
                            "assumed_clo",
                            default=values.get("comfort_assumptions", {}).get("clo", 1.0),
                        ): selector.NumberSelector(
                            selector.NumberSelectorConfig(min=0.01, max=1.99, step=0.1, mode="box")
                        ),
                        vol.Required(
                            "assumed_air_speed",
                            default=values.get("comfort_assumptions", {}).get("air_speed", 0.1),
                        ): selector.NumberSelector(
                            selector.NumberSelectorConfig(min=0.01, max=0.99, step=0.01, mode="box")
                        ),
                        required("emitter", "radiator"): selector.SelectSelector(
                            selector.SelectSelectorConfig(
                                options=["radiator", "underfloor", "mixed", "other"]
                            )
                        ),
                        vol.Optional(
                            "relative_weights",
                            default=values.get(
                                "relative_weights",
                                ",".join(str(w) for w in values.get("weights", [])),
                            ),
                        ): str,
                        vol.Optional("diagnostics", default=values.get("diagnostics", [])): entity(
                            multiple=True
                        ),
                        required("floor_weight", 1): selector.NumberSelector(
                            selector.NumberSelectorConfig(min=0, max=1000, mode="box")
                        ),
                        required("constrains", True): bool,
                    }
                ),
                {"collapsed": True},
            ),
        }
        zones = source_config(self.settings)["zones"]
        if len(zones) > 1:
            schema[required("zone", zones[0]["id"])] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[{"value": z["id"], "label": z["name"]} for z in zones]
                )
            )
        return self.async_show_form(
            step_id="room",
            data_schema=vol.Schema(schema),
            errors=errors,
            description_placeholders={"room_action": "Edit room" if old else "Add room"},
        )

    async def async_step_rooms(self, user_input=None):
        self.editing = None
        menu = ["room", "heat_source", "zones", "energy"]
        if self.rooms:
            menu.extend(["edit_room", "remove_room", "save"])
        if hasattr(self, "entry_id"):
            menu.append("init")
        summary = (
            "\n".join(
                f"- {r['name'].replace(chr(10), ' ')} · {len(r['sensors'])} temperature sensor(s) · "
                f"{r.get('floor') or 'No floor'}"
                for r in self.rooms
            )
            or "No rooms yet. Choose Add room to get started."
        )
        return self.async_show_menu(
            step_id="rooms",
            menu_options=menu,
            description_placeholders={
                "room_list": summary,
                "room_count": str(len(self.rooms)),
                "add_room_label": "Add another room" if self.rooms else "Add room",
            },
        )

    async def async_step_finish(self, user_input=None):
        return await self.async_step_rooms()

    async def async_step_edit_room(self, user_input=None):
        if user_input:
            self.editing = next(r for r in self.rooms if r["name"] == user_input["room"])
            return await self.async_step_room()
        return self.async_show_form(
            step_id="edit_room",
            data_schema=vol.Schema(
                {
                    vol.Required("room"): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=[r["name"] for r in self.rooms])
                    )
                }
            ),
        )

    async def async_step_remove_room(self, user_input=None):
        errors = {}
        if user_input:
            if len(self.rooms) > 1:
                self.rooms = [r for r in self.rooms if r["name"] != user_input["room"]]
                self.settings["comfort_blocks"] = [
                    b
                    for b in self.settings.get("comfort_blocks", [])
                    if b["room"] != user_input["room"]
                ]
                self.editing = None
                return await self.async_step_rooms()
            errors["base"] = "last_room"
        return self.async_show_form(
            step_id="remove_room",
            data_schema=vol.Schema(
                {
                    vol.Required("room"): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=[r["name"] for r in self.rooms])
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_save(self, user_input=None):
        data = dict(self.settings, rooms=self.rooms)
        validate_config(data)
        return self.async_create_entry(title="Thermal Planner", data=data)


class ThermalPlannerConfigFlow(ConfigurationSteps, config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        self.rooms = []
        self.settings = {}
        return await self.async_step_settings(user_input)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return ThermalPlannerOptionsFlow(config_entry)


class ThermalPlannerOptionsFlow(ConfigurationSteps, config_entries.OptionsFlow):
    def __init__(self, entry):
        self.settings = deepcopy(dict(entry.options or entry.data))
        self.rooms = self.settings.pop("rooms")
        self.entry_id = entry.entry_id

    async def async_step_init(self, user_input=None):
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "rooms",
                "heat_source",
                "zones",
                "energy",
                "campaign_settings",
                "comfort_schedule",
                "settings",
                "save",
            ],
        )

    async def async_step_replay(self, user_input=None):
        errors = {}
        if user_input:
            try:
                # Only saved configuration is replayed; unsaved edits never alter evidence.
                manager = self.hass.data[DOMAIN][self.entry_id]
                await manager.async_replay(
                    local_timestamp(user_input["start"], self.hass.config.time_zone),
                    local_timestamp(user_input["end"], self.hass.config.time_zone),
                )
                return await self.async_step_report()
            except ValueError, ServiceValidationError:
                errors["base"] = "invalid_window"
            except Exception:
                _LOGGER.exception("Historical replay failed")
                errors["base"] = "replay_failed"
        return self.async_show_form(
            step_id="replay",
            data_schema=vol.Schema(
                {
                    vol.Required("start"): selector.DateTimeSelector(),
                    vol.Required("end"): selector.DateTimeSelector(),
                }
            ),
            errors=errors,
        )

    async def async_step_report(self, user_input=None):
        from .report import markdown_report

        if user_input is not None:
            return await self.async_step_init()
        manager = self.hass.data[DOMAIN][self.entry_id]
        text = markdown_report(manager.latest, self.hass.config.time_zone)
        if manager.latest:
            from homeassistant.components.http.auth import async_sign_path

            path = f"/api/thermal_planner/{self.entry_id}/{manager.latest['id']}.csv"
            signed = async_sign_path(self.hass, path, timedelta(minutes=5))
            text += f"\n\n[Download CSV (link valid for 5 minutes)]({signed})"
        return self.async_show_form(
            step_id="report",
            data_schema=vol.Schema({}),
            description_placeholders={"report": text, "entry_id": self.entry_id},
        )
