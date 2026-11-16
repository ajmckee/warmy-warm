"""Deterministic shared-source plans; comfort and evidence precede on-time/energy."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from ..configuration import flow_key, signature, source_config
from . import timestamp
from .learning import _input_average, predict, uncertainty
from .signals import temperature


def local_instant(value, zone):
    wall = datetime.fromisoformat(value)
    if wall.tzinfo:
        return timestamp(wall)
    tz = ZoneInfo(zone)
    candidates = {
        wall.replace(tzinfo=tz, fold=fold).astimezone(timezone.utc)
        for fold in (0, 1)
        if wall.replace(tzinfo=tz, fold=fold)
        .astimezone(timezone.utc)
        .astimezone(tz)
        .replace(tzinfo=None)
        == wall
    }
    if len(candidates) != 1:
        raise ValueError(
            "Ambiguous or skipped local time; choose a different time or explicit UTC offset"
        )
    return candidates.pop()


def target_at(config, room, instant, zone):
    local = instant.astimezone(ZoneInfo(zone))
    clock = local.strftime("%H:%M")
    blocks = [block for block in config.get("comfort_blocks", []) if block["room"] == room["name"]]
    target = room["comfort_min"] if blocks else room.get("comfort_target", room["comfort_min"])
    for block in blocks:
        start, end = block["start"][:5], block["end"][:5]
        if start < end:
            active = local.weekday() in block["days"] and start <= clock < end
        else:
            active = (local.weekday() in block["days"] and clock >= start) or (
                (local.weekday() - 1) % 7 in block["days"] and clock < end
            )
        if active:
            target = max(target, block["target_c"])
    return target


def forecast_grid(raw, unit, now, current_outside, max_hours=24):
    points = sorted(
        {
            timestamp(row["datetime"]).timestamp(): temperature(row.get("temperature"), unit)
            for row in raw
            if row.get("datetime")
        }.items()
    )
    points = [
        (when, value)
        for when, value in points
        if value is not None and when >= now.timestamp() - 3600
    ]
    if current_outside is not None:
        points = [(now.timestamp(), current_outside)] + [
            (t, v) for t, v in points if t > now.timestamp()
        ]
    if not points or points[0][0] > now.timestamp() or points[-1][0] - now.timestamp() < 4 * 3600:
        return {
            "available": False,
            "reason": "Need fresh hourly forecasts covering the current period and at least four future hours.",
        }
    end = min(now.timestamp() + max_hours * 3600, points[-1][0])
    count = int((end - now.timestamp()) // 300)
    values, times = [], []
    for step in range(count + 1):
        when = now.timestamp() + step * 300
        pair = next(((a, b) for a, b in zip(points, points[1:]) if a[0] <= when <= b[0]), None)
        if pair is None or pair[1][0] - pair[0][0] > 5400:
            return {
                "available": False,
                "reason": "Forecast has an uncovered gap longer than 90 minutes.",
            }
        (ta, va), (tb, vb) = pair
        values.append(va + (vb - va) * (when - ta) / (tb - ta))
        times.append(datetime.fromtimestamp(when, timezone.utc).isoformat())
    return {
        "available": True,
        "times": times,
        "outside": values,
        "hours": count / 12,
        "partial": count < 288,
    }


def make_plan(config, models, current, forecast, now, zone="UTC"):
    result = {
        "id": uuid4().hex,
        "created_at": now.isoformat(),
        "signature": signature(config),
        "status": "withheld",
        "reasons": [],
        "automatic_eligible": False,
        "actions": [],
        "source_name": source_config(config)["name"],
        "mode": "advisory",
        "model_fitted_at": models.get("fitted_at"),
    }
    if not forecast.get("available"):
        result["reasons"] = [forecast.get("reason", "Hourly forecast unavailable.")]
        return result
    if models.get("signature") != result["signature"]:
        result["reasons"] = ["Models do not match the saved room/source configuration."]
        return result
    source = source_config(config)
    baseline_flow = current.get("flow_target")
    if source.get("flow_readback") and baseline_flow is None:
        result["reasons"] = ["Configured requested-flow readback is missing or stale."]
        return result
    baseline_key = flow_key(baseline_flow)
    group = models.get("flows", {}).get(baseline_key)
    if not group:
        result["reasons"] = ["No validated model at the currently observed flow target."]
        return result
    if any(current.get("rooms", {}).get(room["name"]) is None for room in config["rooms"]):
        result["reasons"] = ["Fresh room temperatures are required."]
        return result
    times, outside = forecast["times"], forecast["outside"]
    required = [room for room in config["rooms"] if room.get("constrains", True)]
    if not required:
        result["reasons"] = ["Include at least one room in planning."]
        return result
    candidates = []
    for key, flow_group in models.get("flows", {}).items():
        flow = flow_group.get("flow_target")
        if key != baseline_key:
            if (
                not source.get("flow_setter")
                or flow is None
                or baseline_flow is None
                or flow_group.get("approved_trials", 0) < 2
            ):
                continue
            if (
                not source["flow_min"] <= flow <= source["flow_max"]
                or abs(flow - baseline_flow) > source["flow_max_step"]
            ):
                continue
        room_models = flow_group.get("rooms", {})
        if any(not room_models.get(room["name"], {}).get("validated") for room in config["rooms"]):
            continue
        represented = all(
            model.get("outside_range_c")
            and min(outside) >= model["outside_range_c"][0]
            and max(outside) <= model["outside_range_c"][1]
            for room in config["rooms"]
            for model in [room_models[room["name"]]]
        )
        if not represented:
            continue
        count = len(outside) - 1
        supported_steps = min(
            48,
            count,
            int(
                min(m["validation"].get("supported_horizon_hours", 4) for m in room_models.values())
                * 12
            ),
        )
        schedules = [(None, 0)]
        dwell = source["dwell_minutes"]
        remaining_dwell = (
            max(
                0,
                (timestamp(current["last_switch"]) + timedelta(minutes=dwell) - now).total_seconds()
                / 60,
            )
            if current.get("last_switch")
            else 0
        )
        flow_steps = min(48, count, source["recovery_minutes"] // 5)
        if key != baseline_key and flow_steps * 5 < dwell:
            continue
        if key != baseline_key and remaining_dwell > 0:
            continue
        # Never extrapolate an unobserved threshold into a longer automatic pause.
        pause_limit = source["pause_limit_minutes"]
        for model in room_models.values():
            validation = model["validation"]
            if validation.get("threshold_observations", 0) < 2:
                pause_limit = min(pause_limit, int(validation.get("observed_pause_hours", 0) * 60))
        # One bounded shared-source pause per plan, with dwell on either side.
        for start in range(0, supported_steps, 6) if key == baseline_key else []:
            if 0 < start * 5 < dwell:
                continue
            if start * 5 < remaining_dwell:
                continue
            for duration in range(max(6, (dwell + 4) // 5), pause_limit // 5 + 1, 6):
                if start + duration <= supported_steps:
                    schedules.append((start, duration))
        for start, duration in schedules:
            inputs = [
                0 if start is not None and start <= i < start + duration else 1
                for i in range(count)
            ]
            traces, min_margin = {}, float("inf")
            for room in config["rooms"]:
                model = room_models.get(room["name"])
                if model is None:
                    continue
                values = predict(
                    model,
                    current["rooms"][room["name"]],
                    outside[:-1],
                    inputs,
                    initial_memory=current.get("heat", 1),
                )
                if key != baseline_key:
                    baseline_model = group["rooms"].get(room["name"])
                    if not baseline_model or not baseline_model.get("validated"):
                        values = []
                    else:
                        value, memory = current["rooms"][room["name"]], current.get("heat", 1)
                        values = [value]
                        for i, (ambient, heat) in enumerate(zip(outside[:-1], inputs)):
                            active = model if i < flow_steps else baseline_model
                            effective, memory = _input_average(
                                heat, memory, 1 / 12, active["tau_hours"]
                            )
                            value += (
                                active["loss_per_hour"] * (ambient - value)
                                + active["heating_c_per_hour"] * effective
                            ) / 12
                            values.append(value)
                    if not values:
                        min_margin = float("-inf")
                        continue
                traces[room["name"]] = values
                if model:
                    for i, value in enumerate(values):
                        margin = uncertainty(model, i / 12)
                        if key != baseline_key and i >= flow_steps:
                            base_model = group["rooms"][room["name"]]
                            margin += uncertainty(base_model, (i - flow_steps) / 12)
                        min_margin = min(
                            min_margin,
                            value
                            - margin
                            - (
                                target_at(config, room, timestamp(times[i]), zone)
                                if room.get("constrains", True)
                                else room["comfort_min"]
                            ),
                        )
            hours = sum(inputs) / 12
            operating = flow_group.get("operating_kw")
            kwh = (
                hours * operating["on"] + (count / 12 - hours) * operating["off"]
                if operating
                else None
            )
            if key != baseline_key:
                base_power = group.get("operating_kw")
                kwh = (
                    flow_steps / 12 * operating["on"] + (count - flow_steps) / 12 * base_power["on"]
                    if operating and base_power
                    else None
                )
            candidates.append(
                {
                    "flow_target": flow,
                    "start": start,
                    "duration": duration,
                    "traces": traces,
                    "min_margin_c": min_margin,
                    "heating_hours": hours,
                    "estimated_electrical_kwh": kwh,
                    "baseline": start is None and key == baseline_key,
                    "flow_group": key,
                    "flow_steps": flow_steps,
                }
            )
    feasible = [candidate for candidate in candidates if candidate["min_margin_c"] >= 0]
    if not feasible:
        result["reasons"] = [
            "No supported candidate meets room targets including validation uncertainty. More suitable data or normal heating is needed."
        ]
        return result
    all_energy = all(candidate["estimated_electrical_kwh"] is not None for candidate in feasible)
    best = min(
        feasible,
        key=lambda candidate: (
            candidate["estimated_electrical_kwh"] if all_energy else candidate["heating_hours"],
            0 if candidate["flow_group"] == baseline_key else 1,
            candidate["start"] or 0,
        ),
    )
    baseline = next((candidate for candidate in candidates if candidate["baseline"]), None)
    chosen_models = models["flows"][best["flow_group"]]["rooms"]
    automatic = all(
        chosen_models[room["name"]].get("automatic_eligible") for room in config["rooms"]
    )
    result.update(
        status="ready",
        times=times,
        outside=outside,
        horizon_hours=forecast["hours"],
        partial_forecast=forecast["partial"],
        predictions=best["traces"],
        flow_target=best["flow_target"],
        baseline_flow=baseline_flow,
        heating_hours=best["heating_hours"],
        baseline_heating_hours=baseline["heating_hours"] if baseline else None,
        min_margin_c=best["min_margin_c"],
        estimated_electrical_kwh=best["estimated_electrical_kwh"],
        baseline_electrical_kwh=baseline["estimated_electrical_kwh"] if baseline else None,
        ranking="estimated electrical energy"
        if all_energy
        else "heating on-time proxy; no kWh or cost saving claim",
        automatic_eligible=automatic and current.get("mode") == "heat",
        reasons=[
            "Comfort targets and chronological validation uncertainty were checked before ranking.",
            "All rooms share one source command; radiator/UFH types do not imply independent valves.",
            "Outlook beyond four hours extrapolates the validated short-horizon response; control actions are bounded to the next four hours.",
        ],
        model_validation={
            room["name"]: chosen_models[room["name"]]["validation"] for room in config["rooms"]
        },
        baseline_predictions=baseline["traces"] if baseline else {},
    )
    if best["start"] is not None:
        result["actions"].append(
            {
                "kind": "pause",
                "start": times[best["start"]],
                "end": times[best["start"] + best["duration"]],
                "minutes": best["duration"] * 5,
            }
        )
    if best["flow_target"] is not None and best["flow_target"] != baseline_flow:
        result["actions"].append(
            {
                "kind": "flow",
                "start": times[0],
                "target": best["flow_target"],
                "end": times[best["flow_steps"]],
            }
        )
    if not automatic:
        result["reasons"].append(
            "Automatic apply withheld: multi-hour threshold validation is not sufficient."
        )
    if current.get("mode") != "heat":
        result["automatic_eligible"] = False
        result["reasons"].append("Source is not currently in Heat mode; preserve manual operation.")
    return result
