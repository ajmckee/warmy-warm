"""Recheck the exact approved action against fresh inputs immediately before a write."""

from ..configuration import flow_key, source_config
from . import timestamp
from .learning import _input_average, uncertainty
from .planning import target_at


def action_safe(config, models, current, forecast, action, now, zone):
    if not forecast.get("available") or current.get("mode") != "heat":
        return False, "Fresh forecasts and Heat mode are required at application time."
    flow = current.get("flow_target")
    key = flow_key(flow)
    base = models.get("flows", {}).get(key)
    if not base:
        return False, "Current flow has no matching validated model."
    target = action.get("target") if action["kind"] == "flow" else flow
    chosen_key = flow_key(target)
    chosen = models.get("flows", {}).get(chosen_key)
    duration = (timestamp(action["end"]) - now).total_seconds() / 3600
    if duration <= 0 or not chosen:
        return False, "Action expired or its flow model is unavailable."
    source = source_config(config)
    if action["kind"] == "pause" and duration * 60 > source["pause_limit_minutes"]:
        return False, "Pause exceeds configured limits."
    if action["kind"] == "flow" and (
        flow is None
        or chosen.get("approved_trials", 0) < 2
        or not source.get("flow_setter")
        or not source["flow_min"] <= target <= source["flow_max"]
        or abs(target - flow) > source["flow_max_step"]
    ):
        return False, "Flow change exceeds current limits or lacks approved trials."
    for room in config["rooms"]:
        name = room["name"]
        initial = current.get("rooms", {}).get(name)
        if initial is None:
            return False, f"Fresh temperature missing in {name}."
        model, baseline = chosen["rooms"].get(name), base["rooms"].get(name)
        if not model or not baseline or not model.get("automatic_eligible"):
            return False, f"Validated threshold evidence missing in {name}."
        value, memory = initial, current.get("heat", 1)
        for i, outside in enumerate(forecast["outside"]):
            active = model if i / 12 < duration else baseline
            represented = active["outside_range_c"]
            if not represented[0] <= outside <= represented[1]:
                return False, "Forecast is outside the validated outdoor range."
            required = (
                target_at(config, room, timestamp(forecast["times"][i]), zone)
                if room.get("constrains", True)
                else room["comfort_min"]
            )
            margin = uncertainty(model, i / 12)
            if action["kind"] == "flow" and i / 12 >= duration:
                margin += uncertainty(baseline, i / 12 - duration)
            if value - margin < required:
                return False, f"Fresh prediction breaches the comfort margin in {name}."
            heat = 0 if action["kind"] == "pause" and i / 12 < duration else 1
            effective, memory = _input_average(heat, memory, 1 / 12, active["tau_hours"])
            value += (
                active["loss_per_hour"] * (outside - value)
                + active["heating_c_per_hour"] * effective
            ) / 12
    return True, None
