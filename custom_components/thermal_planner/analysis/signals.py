"""Optional energy, humidity and delivery classification with explicit coverage."""

from math import isfinite, log


def finite(value):
    try:
        result = float(value)
        return result if isfinite(result) else None
    except ValueError, TypeError:
        return None


def temperature(value, unit):
    value = finite(value)
    if value is None:
        return None
    if unit == "°C":
        return value
    if unit == "°F":
        return (value - 32) * 5 / 9
    if unit == "K":
        return value - 273.15
    return None


def dew_point(temperature_c, humidity):
    humidity = finite(humidity)
    if (
        temperature_c is None
        or not -80 <= temperature_c <= 80
        or humidity is None
        or not 0 < humidity <= 100
    ):
        return None
    gamma = log(humidity / 100) + 17.62 * temperature_c / (243.12 + temperature_c)
    return 243.12 * gamma / (17.62 - gamma)


def classify(mode, space_heat=None, operating_mode=None, mapping=None):
    mapping = mapping or {}
    if operating_mode is not None and str(operating_mode).casefold() in [
        str(s).casefold() for s in mapping.get("dhw_states", [])
    ]:
        return "dhw"
    if operating_mode is not None and str(operating_mode).casefold() in [
        str(s).casefold() for s in mapping.get("defrost_states", [])
    ]:
        return "defrost"
    if space_heat == "off":
        return "space_heat_stopped"
    if space_heat == "on":
        return "space_heat_delivered"
    if mode == "off":
        return "climate_off"
    return "delivery_unverified"


def energy_intervals(points, channel, definition, max_gap_seconds=600):
    """Return per-interval kWh; missing/reset intervals are never bridged."""
    kind = definition.get("kind")
    result = []
    resets = 0
    for a, b in zip(points, points[1:]):
        dt = b["time"] - a["time"]
        start, end = a.get(channel), b.get(channel)
        kwh = None
        if 0 < dt <= max_gap_seconds and start and end:
            va, vb = finite(start.get("value")), finite(end.get("value"))
            ua, ub = start.get("unit"), end.get("unit")
            units = {"W": 0.001, "kW": 1} if kind == "power" else {"Wh": 0.001, "kWh": 1}
            if (
                va is not None
                and vb is not None
                and va >= 0
                and vb >= 0
                and ua in units
                and ub in units
            ):
                va, vb = va * units[ua], vb * units[ub]
                if kind == "power":
                    kwh = (va + vb) / 2 * dt / 3600
                elif vb >= va:
                    kwh = vb - va
                else:
                    resets += 1
        result.append({"seconds": max(dt, 0), "kwh": kwh})
    total = sum(row["seconds"] for row in result)
    covered = sum(row["seconds"] for row in result if row["kwh"] is not None)
    return {
        "kwh": sum(row["kwh"] for row in result if row["kwh"] is not None) if covered else None,
        "coverage": covered / total if total else 0,
        "resets": resets,
        "scope": definition.get("scope", "unknown"),
        "partial": covered < total or not total,
        "intervals": result,
    }


def energy_summary(points, definitions, max_gap_seconds=600):
    channels = {
        name: energy_intervals(points, name, definition, max_gap_seconds)
        for name, definition in definitions.items()
        if definition.get("entity")
    }
    electric, thermal = channels.get("electrical"), channels.get("thermal")
    cop = None
    if (
        electric
        and thermal
        and electric["scope"] == thermal["scope"]
        and electric["scope"] != "wider"
    ):
        if (
            not electric["partial"]
            and not thermal["partial"]
            and electric["kwh"] > 0
            and thermal["kwh"] > 0
        ):
            cop = thermal["kwh"] / electric["kwh"]
    return {
        "channels": {
            name: {k: v for k, v in data.items() if k != "intervals"}
            for name, data in channels.items()
        },
        "cop": cop,
    }


def recovery_summary(points, rooms, starting, tolerance=0.2):
    recovery = [point for point in points if point.get("phase") == "recovery"]
    result = {}
    if not recovery:
        return {"complete": False, "rooms": result, "observed_minutes": 0}
    start = recovery[0]["time"]
    for room in rooms:
        name = room["name"]
        target = max(
            room["comfort_min"],
            starting.get(name, room.get("comfort_target", room["comfort_min"])) - tolerance,
        )
        reached = next(
            (
                point["time"]
                for point in recovery
                if point.get("rooms", {}).get(name) is not None and point["rooms"][name] >= target
            ),
            None,
        )
        valid = sum(point.get("rooms", {}).get(name) is not None for point in recovery)
        duration = recovery[-1]["time"] - recovery[0]["time"]
        covered = sum(
            b["time"] - a["time"]
            for a, b in zip(recovery, recovery[1:])
            if 0 < b["time"] - a["time"] <= 600
            and a.get("rooms", {}).get(name) is not None
            and b.get("rooms", {}).get(name) is not None
        )
        coverage = covered / duration if duration > 0 else valid / len(recovery)
        result[name] = {
            "target_c": target,
            "minutes": (reached - start) / 60 if reached is not None else None,
            "coverage": coverage,
            "complete": reached is not None and valid == len(recovery) and coverage == 1,
        }
    return {
        "complete": bool(result) and all(row["complete"] for row in result.values()),
        "rooms": result,
        "observed_minutes": (recovery[-1]["time"] - start) / 60,
    }
