"""HA entity adapter for one configured heat pump; zones are distribution metadata."""

from homeassistant.helpers import entity_registry

from .analysis.signals import classify, finite, temperature
from .configuration import all_entities, source_config


def identity(hass, entity_id):
    entry = entity_registry.async_get(hass).async_get(entity_id) if entity_id else None
    return (
        {"domain": entry.domain, "platform": entry.platform, "unique_id": entry.unique_id}
        if entry
        else None
    )


def resolve(hass, binding, fallback=None):
    if binding:
        return entity_registry.async_get(hass).async_get_entity_id(
            binding["domain"], binding["platform"], binding["unique_id"]
        )
    return fallback


class HeatSource:
    def __init__(self, hass, config, now):
        self.hass, self.config, self.now = hass, config, now
        self.settings = source_config(config)

    def state(self, entity_id, *, fresh=True):
        state = self.hass.states.get(entity_id) if entity_id else None
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        age = (self.now() - state.last_updated).total_seconds()
        if fresh and (age > self.config["max_age_minutes"] * 60 or age < -60):
            return None
        return state

    def temp(self, entity_id):
        state = self.state(entity_id)
        return (
            temperature(state.state, state.attributes.get("unit_of_measurement")) if state else None
        )

    def flow(self):
        return self.temp(self.settings.get("flow_readback"))

    def room_values(self):
        result = {}
        for room in self.config["rooms"]:
            sources = [
                (self.temp(entity), weight)
                for entity, weight in zip(room["sensors"], room["weights"])
                if weight > 0
            ]
            result[room["name"]] = (
                sum(value * weight for value, weight in sources)
                / sum(weight for _, weight in sources)
                if sources and all(value is not None for value, _ in sources)
                else None
            )
        return result

    def disturbance_reason(self):
        settings = self.settings
        if settings.get("operating_mode"):
            mode = self.state(settings["operating_mode"])
            if mode is None:
                return "Selected operating-mode telemetry is missing or stale."
            if mode.state in settings.get("dhw_states", []):
                return "DHW is active; skip space-heating experiments."
            if mode.state in settings.get("defrost_states", []):
                return "Defrost is active; skip space-heating experiments."
        if settings.get("space_heating") and self.state(settings["space_heating"]) is None:
            return "Selected heat-delivery indication is missing or stale."
        for entity in settings.get("disturbances", []):
            state = self.state(entity)
            if state is None or state.state not in ("off", "closed"):
                return "A selected window/stove disturbance is active or unavailable."
        return None

    def sample(self):
        settings = self.settings
        climate = self.state(settings.get("climate"), fresh=False)
        mode = climate.state if climate else None
        heating = self.state(settings.get("space_heating"))
        operating = self.state(settings.get("operating_mode"))
        category = classify(
            mode,
            heating.state if heating else None,
            operating.state if operating else None,
            settings,
        )
        contaminated = category in ("dhw", "defrost")
        if (settings.get("space_heating") and heating is None) or (
            settings.get("operating_mode") and operating is None
        ):
            contaminated = True
        for entity in settings.get("disturbances", []):
            state = self.state(entity)
            if state is None or state.state not in ("off", "closed"):
                contaminated = True
        outdoor = self.temp(self.config.get("outdoor"))
        outdoor_source = self.config.get("outdoor") if outdoor is not None else None
        if outdoor is None:
            weather = self.state(self.config.get("weather"))
            if weather:
                outdoor = temperature(
                    weather.attributes.get("temperature"),
                    weather.attributes.get("temperature_unit"),
                )
                outdoor_source = self.config.get("weather") if outdoor is not None else None
        point = {
            "time": self.now().timestamp(),
            "rooms": self.room_values(),
            "outdoor": outdoor,
            "outdoor_source": outdoor_source,
            "mode": mode,
            "heat": 1
            if category == "space_heat_delivered"
            else 0
            if category in ("space_heat_stopped", "climate_off")
            else 1
            if mode == "heat"
            else None,
            "classification": category,
            "contaminated": contaminated,
            "flow_target": self.flow(),
            "leaving_water_c": self.temp(settings.get("leaving_water")),
            "humidity": {},
            "radiant": {},
            "zones": {},
        }
        point["delivered_heat"] = point["heat"]
        if mode in ("heat", "off"):
            point["heat"] = 1 if mode == "heat" else 0
            point["input_kind"] = "commanded HVAC response, including observed device lag/cycling"
        else:
            point["input_kind"] = "selected delivery telemetry; no commanded mode available"
            if settings.get("climate"):
                point["contaminated"] = True
        for room in self.config["rooms"]:
            state = self.state(room.get("humidity"))
            value = (
                finite(state.state)
                if state and state.attributes.get("unit_of_measurement") == "%"
                else None
            )
            point["humidity"][room["name"]] = (
                value if value is not None and 0 <= value <= 100 else None
            )
            point["radiant"][room["name"]] = self.temp(room.get("radiant_temperature"))
        for zone in settings["zones"]:
            pump = self.state(zone.get("pump"))
            point["zones"][zone["id"]] = {
                "supply_c": self.temp(zone.get("supply")),
                "return_c": self.temp(zone.get("return")),
                "pump": pump.state if pump else None,
            }
        for name, channel in self.config.get("energy", {}).items():
            state = self.state(channel.get("entity"))
            value = finite(state.state) if state else None
            point[name] = (
                {"value": value, "unit": state.attributes.get("unit_of_measurement")}
                if state and value is not None and value >= 0
                else None
            )
        point["signals"] = {}
        for entity in all_entities(self.config):
            state = self.hass.states.get(entity)
            if state is not None:
                weather = entity.startswith("weather.")
                point["signals"][entity] = {
                    "timestamp": state.last_updated.isoformat(),
                    "value": state.attributes.get("temperature") if weather else state.state,
                    "unit": state.attributes.get("temperature_unit")
                    if weather
                    else state.attributes.get("unit_of_measurement"),
                }
        return point

    def validate_capabilities(self):
        settings = self.settings
        if settings.get("climate"):
            state = self.state(settings["climate"], fresh=False)
            if state is None or not {"heat", "off"} <= set(state.attributes.get("hvac_modes", [])):
                raise ValueError(
                    "Select an available space-heating climate supporting Heat and Off"
                )
        if settings.get("flow_setter"):
            entity = settings["flow_setter"]
            state = self.state(entity)
            if not entity.startswith(("number.", "input_number.")) or state is None:
                raise ValueError("Select an available number entity for the direct flow target")
            if state.attributes.get("unit_of_measurement") != "°C" or self.flow() is None:
                raise ValueError("Direct flow setter/readback must use °C")
            minimum, maximum = state.attributes.get("min"), state.attributes.get("max")
            if (
                minimum is None
                or maximum is None
                or settings["flow_min"] < minimum
                or settings["flow_max"] > maximum
            ):
                raise ValueError("Configured flow limits exceed the setter's advertised range")
        for channel in self.config.get("energy", {}).values():
            if not channel.get("entity"):
                continue
            state = self.state(channel["entity"])
            units = ("W", "kW") if channel["kind"] == "power" else ("Wh", "kWh")
            if state is None or state.attributes.get("unit_of_measurement") not in units:
                raise ValueError("Energy sensor units do not match power/counter selection")
        for room in self.config["rooms"]:
            if room.get("humidity"):
                state = self.state(room["humidity"])
                if state and state.attributes.get("unit_of_measurement") != "%":
                    raise ValueError("Relative humidity must use percent units")
