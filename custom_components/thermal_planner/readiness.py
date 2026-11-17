"""Read-only measurement checks and concrete next steps for the dashboard."""

from math import isfinite

from .configuration import source_config
from .source import HeatSource


def measurement_readiness(hass, config, controller, now):
    source = source_config(config)
    adapter = HeatSource(hass, config, now)
    checks = []
    rooms_ok = True
    values = adapter.room_values()
    for room in config["rooms"]:
        value = values.get(room["name"])
        valid = value is not None and value > room["comfort_min"]
        rooms_ok &= valid
        checks.append(
            {
                "title": room["name"],
                "ready": valid,
                "message": (
                    f"{value:.1f}°C · minimum {room['comfort_min']:g}°C"
                    if valid
                    else f"Only {value:.1f}°C. Let normal heating raise this room above its {room['comfort_min']:g}°C minimum before pausing."
                )
                if value is not None
                else "No recent room reading. Check the sensor or increase the live freshness limit if it reports infrequently.",
                "action": "sensor" if not valid else None,
                "entity_id": next(iter(room["sensors"]), None),
            }
        )
    entity = source.get("climate")
    state = adapter.state(entity, fresh=False)
    capable = bool(state and {"heat", "off"} <= set(state.attributes.get("hvac_modes", [])))
    target = state.attributes.get("temperature") if state else None
    target_ok = isinstance(target, (int, float)) and isfinite(target)
    heating = capable and state.state == "heat" and target_ok
    checks.append(
        {
            "title": "Heat pump",
            "ready": heating,
            "message": "Ready to pause and restore."
            if heating
            else "Choose the Home Assistant heating control."
            if not entity
            else "The heat pump is unavailable or does not offer Heat and Off."
            if not capable
            else "Switch to Heat using the heat pump's normal control before a cooldown."
            if state.state != "heat"
            else "The heating control has no target temperature to verify restoration. Choose the correct climate entity.",
            "action": "setup"
            if not capable or not target_ok
            else "source"
            if not heating
            else None,
            "entity_id": entity,
        }
    )
    commissioned = bool(controller and controller.allowed(config))
    checks.append(
        {
            "title": "Pause and restore check",
            "ready": commissioned,
            "message": "Verified for these source settings."
            if commissioned
            else "First, verify pause and restoration with someone present. Normal observation does not need this check.",
            "action": None if commissioned else "commission",
        }
    )
    disturbance = adapter.disturbance_reason()
    checks.append(
        {
            "title": "Heating conditions",
            "ready": not disturbance,
            "message": disturbance
            or "No selected hot-water, defrost or disturbance signal is blocking the test.",
            "action": None if not disturbance else "wait",
        }
    )
    dwell = False
    if controller and controller.last_switch:
        from datetime import timedelta

        from .analysis import timestamp

        dwell = now() < timestamp(controller.last_switch) + timedelta(
            minutes=source["dwell_minutes"]
        )
    if dwell:
        checks.append(
            {
                "title": "Rest between tests",
                "ready": False,
                "message": f"Wait for the {source['dwell_minutes']:g}-minute rest after the last heating change.",
                "action": "wait",
            }
        )
    return {
        "checks": checks,
        "can_pause": bool(heating and rooms_ok and commissioned and not disturbance and not dwell),
        "can_commission": bool(heating and rooms_ok and not disturbance),
        "can_observe": all(v is not None for v in values.values()),
        "configured": bool(entity),
    }
