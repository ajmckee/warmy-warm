"""Serializable installation settings and validation, independent of HA."""

import json
from copy import deepcopy
from hashlib import sha256
from math import isfinite


def source_config(config):
    source = deepcopy(config.get("heat_source") or {})
    source.setdefault("name", "Heat pump")
    source.setdefault("zones", [{"id": "main", "name": "Main zone"}])
    source.setdefault("pause_limit_minutes", 30)
    source.setdefault("dwell_minutes", 30)
    source.setdefault("recovery_minutes", 120)
    source.setdefault("recovery_tolerance_c", 0.2)
    source.setdefault("commissioning_allowed", False)
    return source


def flow_key(value):
    return (
        format(float(value), ".2f").rstrip("0").rstrip(".") if value is not None else "unspecified"
    )


def source_fingerprint(config):
    source = source_config(config)
    return sha256(json.dumps(source, sort_keys=True).encode()).hexdigest()


def signature(config):
    """Invalidate models/plans when signal, zone, weight or guard settings change."""
    data = {
        key: config.get(key)
        for key in (
            "heat_source",
            "rooms",
            "outdoor",
            "weather",
            "max_age_minutes",
            "energy",
            "comfort_blocks",
        )
    }
    return sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def validate_settings(config):
    source = source_config(config)
    if not source["name"].strip():
        raise ValueError("Heat-source name is required")
    zones = source["zones"]
    if not 1 <= len(zones) <= 16 or len({z["id"] for z in zones}) != len(zones):
        raise ValueError("Configure one heat pump with unique zone IDs")
    if any(not z["name"].strip() for z in zones):
        raise ValueError("Zone names are required")
    if len({z["name"].strip().casefold() for z in zones}) != len(zones):
        raise ValueError("Zone names must be unique")
    ids = {z["id"] for z in zones}
    if any(room.get("zone") and room["zone"] not in ids for room in config.get("rooms", [])):
        raise ValueError("Assign rooms to an existing heat-pump zone")
    for key, lower, upper in (
        ("pause_limit_minutes", 1, 240),
        ("dwell_minutes", 1, 240),
        ("recovery_minutes", 1, 720),
        ("recovery_tolerance_c", 0, 2),
    ):
        if not lower <= source[key] <= upper:
            raise ValueError(f"Invalid {key}")
    if source.get("flow_setter"):
        if not source.get("flow_readback") or not source.get("direct_mode_confirmed"):
            raise ValueError("A flow setter requires readback and confirmed direct mode")
        values = [source.get(key) for key in ("flow_min", "flow_max", "flow_max_step")]
        if any(not isinstance(v, (float, int)) or not isfinite(v) for v in values):
            raise ValueError("Explicit flow limits and maximum adjustment are required")
        if not values[0] < values[1] or not 0 < values[2] <= values[1] - values[0]:
            raise ValueError("Invalid flow limits")
    for channel in ("electrical", "thermal"):
        energy = config.get("energy", {}).get(channel)
        if energy and energy.get("entity"):
            if energy.get("kind") not in ("power", "counter"):
                raise ValueError("Confirm power or cumulative energy for each channel")
            if energy.get("scope") not in ("space_heating", "heat_pump", "wider"):
                raise ValueError("Confirm what each energy channel covers")
    for room in config.get("rooms", []):
        if not -10 <= room.get("comfort_target", room["comfort_min"]) <= 40:
            raise ValueError("Invalid comfort target")
    bounds = config.get("report_windows_minutes", [0, 60, 120, 240])
    if (
        len(bounds) < 2
        or bounds[0] != 0
        or bounds != sorted(set(bounds))
        or any(
            not isinstance(value, (int, float))
            or not isfinite(value)
            or not 0 <= value <= 1440
            or value % 5
            for value in bounds
        )
    ):
        raise ValueError("Report windows must increase from 0 to at most 1440 minutes")
    if any(
        not isinstance(value, (int, float)) or not isfinite(value) or value <= 0 or value > 20
        for value in config.get("drop_thresholds", [])
    ):
        raise ValueError("Invalid drop thresholds")
    for block in config.get("comfort_blocks", []):
        if block["room"] not in {r["name"] for r in config.get("rooms", [])}:
            raise ValueError("Comfort block refers to a missing room")
        if not block.get("days") or any(day not in range(7) for day in block["days"]):
            raise ValueError("Choose weekdays for each comfort block")
        from datetime import time

        time.fromisoformat(block["start"])
        time.fromisoformat(block["end"])


def all_entities(config):
    entities = {config.get("outdoor"), config.get("weather")}
    for room in config.get("rooms", []):
        entities.update(room["sensors"])
        entities.update(room.get("diagnostics", []))
        entities.add(room.get("humidity"))
    source = source_config(config)
    entities.update(
        source.get(key)
        for key in (
            "climate",
            "space_heating",
            "operating_mode",
            "leaving_water",
            "flow_readback",
            "flow_setter",
        )
    )
    for zone in source["zones"]:
        entities.update(zone.get(key) for key in ("climate", "supply", "return", "pump"))
    entities.update(source.get("disturbances", []))
    for channel in config.get("energy", {}).values():
        entities.add(channel.get("entity"))
    return {entity for entity in entities if entity}
