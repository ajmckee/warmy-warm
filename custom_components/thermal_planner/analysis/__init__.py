"""Deterministic, standard-library-only historical cooldown analysis."""

import csv
import io
import math
from bisect import bisect_right
from copy import deepcopy
from datetime import datetime, timedelta, timezone


def timestamp(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("An explicit UTC offset is required (including during DST transitions)")
    return dt.astimezone(timezone.utc)


def validate_config(config):
    from ..configuration import validate_settings

    validate_settings(config)
    rooms = config["rooms"]
    if not rooms or len({r["name"] for r in rooms}) != len(rooms):
        raise ValueError("Configure at least one room with unique names")
    for room in rooms:
        sources = room["sensors"]
        weights = room["weights"]
        if not sources or len(set(sources)) != len(sources) or len(weights) != len(sources):
            raise ValueError("Select unique sensors with one weight per sensor")
        if any(not math.isfinite(w) or w < 0 for w in weights) or sum(weights) <= 0:
            raise ValueError("Weights must be finite, non-negative and include a positive weight")
        if not room["name"].strip():
            raise ValueError("Room name is required")
        if not math.isfinite(room["floor_weight"]) or room["floor_weight"] < 0:
            raise ValueError("Floor weight must be finite and non-negative")
    if not 60 <= config["max_age_minutes"] * 60 <= 86400:
        raise ValueError("Freshness must be between 1 minute and 24 hours")


def normalize(history):
    result = {}
    for entity, records in history.items():
        points = {}
        for record in records:
            when = timestamp(record["timestamp"]).timestamp()
            try:
                value = float(record["value"])
                unit = record.get("unit")
                if unit == "°F":
                    value = (value - 32) * 5 / 9
                elif unit == "K":
                    value -= 273.15
                elif unit != "°C":
                    value = None
                if value is not None and not math.isfinite(value):
                    value = None
            except ValueError, TypeError:
                value = None
            points[when] = value
        result[entity] = (sorted(points), points)
    return result


def reading(series, entity, when, age):
    times, values = series.get(entity, ([], {}))
    index = bisect_right(times, when) - 1
    if index < 0 or when - times[index] > age:
        return None
    return values[times[index]]


def fuse(series, room, when, age):
    items = [
        (reading(series, entity, when, age), weight)
        for entity, weight in zip(room["sensors"], room["weights"])
        if weight > 0
    ]
    if any(value is None for value, _ in items):
        return None
    return sum(value * weight for value, weight in items) / sum(weight for _, weight in items)


def metrics(times, values, outside):
    valid = [i for i, value in enumerate(values) if value is not None]
    duration = times[-1] - times[0]
    covered = sum(
        times[i + 1] - times[i]
        for i in range(len(times) - 1)
        if values[i] is not None and values[i + 1] is not None
    )
    complete = len(valid) == len(times)
    start, end = values[0], values[-1]
    delta = end - start if complete else None
    pairs = [
        i
        for i in range(len(times) - 1)
        if values[i] is not None
        and values[i + 1] is not None
        and outside[i] is not None
        and outside[i + 1] is not None
    ]
    outdoor_seconds = sum(times[i + 1] - times[i] for i in pairs)
    mean_out = gap = None
    if outdoor_seconds:
        mean_out = (
            sum((outside[i] + outside[i + 1]) / 2 * (times[i + 1] - times[i]) for i in pairs)
            / outdoor_seconds
        )
        gap = (
            sum(
                (values[i] + values[i + 1] - outside[i] - outside[i + 1])
                / 2
                * (times[i + 1] - times[i])
                for i in pairs
            )
            / outdoor_seconds
        )
    slope = delta * 3600 / duration if delta is not None else None
    return dict(
        start_c=start,
        end_c=end,
        delta_c=delta,
        slope_c_per_h=slope,
        coverage=covered / duration,
        outside_coverage=outdoor_seconds / duration,
        mean_outside_c=mean_out,
        mean_gap_k=gap,
        normalized_c_per_h_10k=slope * 10 / gap
        if slope is not None and outdoor_seconds == duration and gap is not None and gap >= 2
        else None,
        quality="complete" if complete else "missing_or_stale",
    )


def thresholds(times, values, drops=(0.5, 1.0)):
    result = {"observed_hours": (times[-1] - times[0]) / 3600}
    for drop in drops:
        crossing = None
        if values[0] is not None:
            target = values[0] - drop
            for i in range(1, len(times)):
                a, b = values[i - 1], values[i]
                if a is not None and b is not None and a > target >= b:
                    crossing = (
                        times[i - 1] - times[0] + (a - target) / (a - b) * (times[i] - times[i - 1])
                    ) / 3600
                    break
        result[str(drop)] = crossing
    return result


def replay(config, history, start, end):
    validate_config(config)
    start, end = timestamp(start), timestamp(end)
    seconds = (end - start).total_seconds()
    if not 60 <= seconds <= 86400:
        raise ValueError("Replay must be between one minute and 24 hours")
    series = normalize(history)
    age = config["max_age_minutes"] * 60
    # Fixed five-minute cadence, including exact window boundaries and final endpoint.
    offsets = sorted(
        set(
            [0, seconds]
            + list(range(300, int(seconds), 300))
            + [x for x in (3600, 7200, 14400) if x < seconds]
        )
    )
    times = [start.timestamp() + x for x in offsets]
    outdoor = config.get("outdoor") or config.get("weather")
    outside = [reading(series, outdoor, t, age) for t in times]
    if not any(value is not None for value in outside) and config.get("weather"):
        outdoor = config["weather"]
        outside = [reading(series, outdoor, t, age) for t in times]
    traces = {r["name"]: [fuse(series, r, t, age) for t in times] for r in config["rooms"]}
    report = dict(
        version=1,
        start=start.isoformat(),
        end=end.isoformat(),
        label="observed indoor change; heat delivery unverified",
        outdoor_source=outdoor,
        configuration=deepcopy(config),
        windows=[],
        rooms={},
        traces={},
    )
    for room in config["rooms"]:
        vals = traces[room["name"]]
        report["rooms"][room["name"]] = dict(
            thresholds=thresholds(
                times, vals, sorted({0.5, 1.0, *config.get("drop_thresholds", [])})
            ),
            comfort_breached=any(v is not None and v < room["comfort_min"] for v in vals),
            comfort_coverage=sum(v is not None for v in vals) / len(vals),
        )
    bounds = config.get("report_windows_minutes", [0, 60, 120, 240])
    for a, b in zip([m * 60 for m in bounds], [m * 60 for m in bounds[1:]]):
        if a >= seconds:
            continue
        indexes = [i for i, x in enumerate(offsets) if a <= x <= min(b, seconds)]
        ts = [times[i] for i in indexes]
        out = [outside[i] for i in indexes]
        window = dict(
            start=(start + timedelta(seconds=a)).isoformat(),
            end=(start + timedelta(seconds=min(b, seconds))).isoformat(),
            rooms={},
            floors={},
        )
        for room in config["rooms"]:
            window["rooms"][room["name"]] = metrics(
                ts, [traces[room["name"]][i] for i in indexes], out
            )
        for floor in sorted({r.get("floor") for r in config["rooms"] if r.get("floor")}):
            members = [
                r for r in config["rooms"] if r.get("floor") == floor and r["floor_weight"] > 0
            ]
            vals = []
            for i in indexes:
                vals.append(
                    sum(traces[r["name"]][i] * r["floor_weight"] for r in members)
                    / sum(r["floor_weight"] for r in members)
                    if members and all(traces[r["name"]][i] is not None for r in members)
                    else None
                )
            window["floors"][floor] = metrics(ts, vals, out)
            window["floors"][floor]["members"] = [r["name"] for r in members]
        report["windows"].append(window)
    constrained = [r for r in config["rooms"] if r["constrains"]]
    candidates = [(report["rooms"][r["name"]]["thresholds"]["0.5"], r["name"]) for r in constrained]
    report["limiting_room"] = min(
        (x for x in candidates if x[0] is not None), default=(None, None)
    )[1]
    # Bounded traces retained in storage only, never entity attributes.
    report["traces"] = dict(
        timestamps=[datetime.fromtimestamp(t, timezone.utc).isoformat() for t in times],
        rooms=traces,
        outside=outside,
        diagnostics={
            entity: [reading(series, entity, t, age) for t in times]
            for room in config["rooms"]
            for entity in room.get("diagnostics", [])
        },
    )
    from .extras import enrich

    enrich(report, history, times, traces, config, age)
    return report


def to_csv(report):
    output = io.StringIO(newline="")
    fields = [
        "kind",
        "name",
        "start",
        "end",
        "start_c",
        "end_c",
        "delta_c",
        "slope_c_per_h",
        "coverage",
        "outside_coverage",
        "mean_outside_c",
        "mean_gap_k",
        "normalized_c_per_h_10k",
        "quality",
    ]
    writer = csv.DictWriter(output, fields, extrasaction="ignore")
    writer.writeheader()
    for window in report["windows"]:
        for kind in ("rooms", "floors"):
            for name, data in window[kind].items():
                # Prevent spreadsheet formula interpretation of user-supplied labels.
                safe_name = "'" + name if name.startswith(("=", "+", "-", "@")) else name
                writer.writerow(
                    dict(data, kind=kind, name=safe_name, start=window["start"], end=window["end"])
                )
    return output.getvalue()
