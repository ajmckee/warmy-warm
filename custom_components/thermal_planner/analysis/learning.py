"""Small effective RC models with episode-level chronological validation.

Coefficients describe temperature response, never unidentified heat capacity or
room kWh. UFH can select a second, effective heating-memory state. No SciPy.
"""

from collections import defaultdict
from math import exp, sqrt
from statistics import mean, pstdev

from ..configuration import flow_key, signature
from .signals import energy_intervals


def _input_average(heat, memory, dt, tau):
    if tau == 0:
        return heat, heat
    decay = exp(-dt / tau)
    return heat + (memory - heat) * tau / dt * (1 - decay), heat + (memory - heat) * decay


def _rows(episodes, room, tau):
    rows = []
    for episode in episodes:
        points = episode["points"]
        memory = episode.get("initial_heat", points[0].get("heat", 1))
        for a, b in zip(points, points[1:]):
            dt = (b["time"] - a["time"]) / 3600
            av, bv = a.get("rooms", {}).get(room), b.get("rooms", {}).get(room)
            if (
                not 0 < dt <= 1 / 6
                or a.get("heat") not in (0, 1)
                or a.get("outdoor") is None
                or av is None
                or bv is None
                or a.get("contaminated")
                or b.get("contaminated")
            ):
                memory = b.get("heat") if b.get("heat") in (0, 1) else 0
                continue
            effective, memory = _input_average(a["heat"], memory, dt, tau)
            rows.append((a["outdoor"] - av, effective, (bv - av) / dt))
    return rows


def _bounded_fit(rows):
    if len(rows) < 40:
        return None
    xx = sum(x * x for x, h, y in rows)
    hh = sum(h * h for x, h, y in rows)
    xh = sum(x * h for x, h, y in rows)
    xy = sum(x * y for x, h, y in rows)
    hy = sum(h * y for x, h, y in rows)
    determinant = xx * hh - xh * xh
    if xx == 0 or hh == 0 or determinant <= xx * hh * 1e-5:
        return None
    k, heating = (xy * hh - hy * xh) / determinant, (hy * xx - xy * xh) / determinant
    if not 0.0001 <= k <= 2 or not 0 < heating <= 20:
        return None
    return k, heating


def predict(model, initial, outdoor, heating, step_hours=1 / 12, initial_memory=1):
    value, memory = initial, initial_memory
    values = [value]
    for outside, heat in zip(outdoor, heating):
        effective, memory = _input_average(heat, memory, step_hours, model["tau_hours"])
        value += step_hours * (
            model["loss_per_hour"] * (outside - value) + model["heating_c_per_hour"] * effective
        )
        values.append(value)
    return values


def uncertainty(model, hours):
    supported = max(1, model.get("validation", {}).get("supported_horizon_hours", 4))
    empirical = model["uncertainty_c"] * max(1, (hours / supported) ** 0.5)
    tau = model["tau_hours"]
    # Unknown initial heating memory may range from zero to its steady input.
    # Bound its integrated deficit without pretending it is measured slab heat.
    return empirical + (model["heating_c_per_hour"] * tau * (1 - exp(-hours / tau)) if tau else 0)


def _crossing(times, values, threshold):
    for i, (a, b) in enumerate(zip(values, values[1:])):
        if a is None or b is None:
            return None
        if a > threshold >= b:
            return times[i] + (a - threshold) / (a - b) * (times[i + 1] - times[i])
    return None


def _score(model, episodes, room, comfort_min):
    errors, baseline, crossings = [], [], []
    coverage, durations, cool_durations, comfort_margins = [], [], [], []
    horizons = defaultdict(list)
    for episode in episodes:
        points = episode["points"]
        if not points or points[0].get("rooms", {}).get(room) is None:
            continue
        initial = points[0]["rooms"][room]
        value, memory = initial, episode.get("initial_heat", 1)
        actual, predictions, times = [initial], [initial], [0]
        for a, b in zip(points, points[1:]):
            dt = (b["time"] - a["time"]) / 3600
            truth = b.get("rooms", {}).get(room)
            if (
                not 0 < dt <= 1 / 6
                or truth is None
                or a.get("outdoor") is None
                or a.get("heat") not in (0, 1)
                or a.get("contaminated")
                or b.get("contaminated")
            ):
                break
            effective, memory = _input_average(a["heat"], memory, dt, model["tau_hours"])
            value += dt * (
                model["loss_per_hour"] * (a["outdoor"] - value)
                + model["heating_c_per_hour"] * effective
            )
            elapsed = (b["time"] - points[0]["time"]) / 3600
            error = abs(value - truth)
            errors.append(error)
            baseline.append(abs(initial - truth))
            horizons[
                "0–1 h"
                if elapsed <= 1
                else "1–2 h"
                if elapsed <= 2
                else "2–4 h"
                if elapsed <= 4
                else "4+ h"
            ].append(error)
            times.append(elapsed)
            actual.append(truth)
            predictions.append(value)
        coverage.append((len(actual) - 1) / max(1, len(points) - 1))
        durations.append(times[-1])
        longest, current = 0, 0
        for a, b in zip(points, points[1:]):
            current = current + (b["time"] - a["time"]) / 3600 if a.get("heat") == 0 else 0
            longest = max(longest, current)
        cool_durations.append(longest)
        comfort_margins.extend(value - comfort_min for value in actual)
        for threshold in (initial - 0.5, initial - 1, comfort_min):
            observed = _crossing(times, actual, threshold)
            if observed is not None:
                predicted = _crossing(times, predictions, threshold)
                crossings.append(abs(predicted - observed) if predicted is not None else times[-1])
    return {
        "mae_c": mean(errors) if errors else None,
        "persistence_mae_c": mean(baseline) if baseline else None,
        "max_error_c": max(errors, default=None),
        "by_horizon_c": {key: mean(value) for key, value in horizons.items()},
        "threshold_observations": len(crossings),
        "threshold_mae_hours": mean(crossings) if crossings else None,
        "samples": len(errors),
        "minimum_episode_coverage": min(coverage, default=0),
        "supported_horizon_hours": min(durations, default=0),
        "observed_pause_hours": min(cool_durations, default=0),
        "minimum_observed_comfort_margin_c": min(comfort_margins, default=0),
    }


def fit_models(config, episodes, previous=None, fitted_at=None):
    """Train/select/test on disjoint complete episodes, retain a better old fit."""
    current_signature = signature(config)
    eligible = sorted(
        {
            episode["id"]: episode
            for episode in [
                episode
                for episode in episodes
                if episode.get("signature") == current_signature
                and episode.get("learning_eligible")
                and len(episode.get("points", [])) >= 12
            ]
        }.values(),
        key=lambda episode: episode["points"][0]["time"],
    )
    groups = defaultdict(list)
    for episode in eligible:
        target = episode.get("flow_target")
        groups[flow_key(target)].append(episode)
    result = {
        "version": 1,
        "signature": current_signature,
        "fitted_at": fitted_at,
        "flows": {},
        "reasons": [],
        "synthetic": any(e.get("synthetic") for e in eligible),
    }
    if not eligible:
        result["reasons"].append(
            "No complete, uncontaminated cooldown/recovery episodes for the saved configuration."
        )
    for flow, group in groups.items():
        if len(group) < 8:
            result["reasons"].append(
                f"Flow {flow}: need at least eight independent complete episodes; have {len(group)}."
            )
            continue
        split = max(4, int(len(group) * 0.6))
        selection_end = min(len(group) - 2, split + max(1, int(len(group) * 0.2)))
        training, selection, holdout = (
            group[:split],
            group[split:selection_end],
            group[selection_end:],
        )
        if not selection:
            selection, training = training[-1:], training[:-1]
        models = {}
        for room in config["rooms"]:
            name = room["name"]
            candidates = []
            for tau in [0, 0.5, 1, 2, 4] if room.get("emitter") in ("underfloor", "mixed") else [0]:
                coefficients = _bounded_fit(_rows(training, name, tau))
                if coefficients is None:
                    continue
                model = {
                    "loss_per_hour": coefficients[0],
                    "heating_c_per_hour": coefficients[1],
                    "tau_hours": tau,
                }
                score = _score(model, selection, name, room["comfort_min"])
                if score["mae_c"] is not None:
                    candidates.append((score["mae_c"] + (0.02 if tau else 0), model))
            if not candidates:
                result["reasons"].append(
                    f"{room['name']} at flow {flow}: insufficient coverage or independent heating/outdoor excitation."
                )
                continue
            model = min(candidates, key=lambda item: item[0])[1]
            validation = _score(model, holdout, name, room["comfort_min"])
            heats = {point.get("heat") for episode in holdout for point in episode["points"]}
            outside = [
                point["outdoor"]
                for episode in group
                for point in episode["points"]
                if point.get("outdoor") is not None
            ]
            validated = (
                validation["samples"] >= 20
                and validation["minimum_episode_coverage"] == 1
                and validation["supported_horizon_hours"] >= 1
                and validation["mae_c"] is not None
                and validation["mae_c"] <= 0.5
                and validation["mae_c"] < validation["persistence_mae_c"]
                and {0, 1} <= heats
                and max(outside, default=0) - min(outside, default=0) >= 2
            )
            model.update(
                validated=validated,
                validation=validation,
                emitter=room.get("emitter", "other"),
                input_kind="effective commanded heating response; heat delivery may be unverified",
                state_kind="air + effective heating memory"
                if model["tau_hours"]
                else "one-state room",
                training_episode_ids=[e["id"] for e in training],
                selection_episode_ids=[e["id"] for e in selection],
                holdout_episode_ids=[e["id"] for e in holdout],
                outside_range_c=[min(outside), max(outside)] if outside else None,
                automatic_eligible=validated
                and (
                    (
                        validation["threshold_observations"] >= 2
                        and validation["threshold_mae_hours"] <= 1
                    )
                    or (
                        validation["observed_pause_hours"] >= 0.5
                        and validation["minimum_observed_comfort_margin_c"]
                        >= max(0.5, 2 * validation["mae_c"])
                    )
                ),
                uncertainty_c=max(0.1, 2 * (validation["mae_c"] or 0)),
            )
            old = (previous or {}).get("flows", {}).get(flow, {}).get("rooms", {}).get(name)
            if (
                old
                and (previous or {}).get("signature") == current_signature
                and old.get("validated")
                and (not validated or old["validation"]["mae_c"] < validation["mae_c"])
            ):
                model = dict(old, retained_previous=True)
            models[name] = model
        if previous and previous.get("signature") == current_signature:
            for name, old in previous.get("flows", {}).get(flow, {}).get("rooms", {}).items():
                if name not in models and old.get("validated"):
                    models[name] = dict(old, retained_previous=True)
        result["flows"][flow] = {
            "rooms": models,
            "episode_count": len(group),
            "approved_trials": sum(bool(e.get("approved_flow_trial")) for e in group),
            "flow_target": group[0].get("flow_target"),
            "operating_kw": operating_power(config, training, holdout),
        }
    # Sparse/degraded new data must not discard an already validated same-config model.
    if previous and previous.get("signature") == current_signature:
        for flow, group in previous.get("flows", {}).items():
            if flow not in result["flows"] and all(
                m.get("validated") for m in group["rooms"].values()
            ):
                result["flows"][flow] = dict(group, retained_previous=True)
    return result


def operating_power(config, episodes, holdout=()):
    definition = config.get("energy", {}).get("electrical", {})
    if not definition.get("entity") or definition.get("scope") != "space_heating":
        return None
    samples = {0: [], 1: []}
    for episode in episodes:
        points = episode["points"]
        intervals = energy_intervals(points, "electrical", definition)["intervals"]
        for a, b, interval in zip(points, points[1:], intervals):
            state = a.get("heat")
            if (
                state in (0, 1)
                and state == b.get("heat")
                and interval["kwh"] is not None
                and interval["seconds"] > 0
                and not a.get("contaminated")
                and not b.get("contaminated")
            ):
                samples[state].append(interval["kwh"] * 3600 / interval["seconds"])
    if len(samples[1]) < 30 or len(samples[0]) < 10:
        return None
    on, off = mean(samples[1]), mean(samples[0])
    if on <= 0 or pstdev(samples[1]) / on > 0.5:
        return None
    errors = []
    for episode in holdout:
        points = episode["points"]
        energy = energy_intervals(points, "electrical", definition)
        if energy["partial"] or not energy["kwh"]:
            return None
        expected = sum(
            (b["time"] - a["time"]) / 3600 * (on if a["heat"] else off)
            for a, b in zip(points, points[1:])
        )
        errors.append(abs(expected - energy["kwh"]) / energy["kwh"])
    if not errors or mean(errors) > 0.2 or max(errors) > 0.3:
        return None
    return {
        "on": on,
        "off": off,
        "scope": "measured space-heating input",
        "sample_count": len(samples[0]) + len(samples[1]),
        "holdout_relative_error": mean(errors),
    }


def heat_demand(windows):
    usable = [
        w
        for w in windows
        if w.get("stable")
        and w.get("coverage") == 1
        and w.get("space_heat_confirmed")
        and w.get("thermal_w") is not None
        and w.get("gap_k", 0) >= 2
    ]
    if len(usable) < 6:
        return {
            "available": False,
            "reason": "Need six stable, covered windows with measured delivered space heat.",
        }
    xs, ys = [w["gap_k"] for w in usable], [w["thermal_w"] for w in usable]
    mx, my = mean(xs), mean(ys)
    spread = sum((x - mx) ** 2 for x in xs)
    if max(xs) - min(xs) < 3 or spread == 0:
        return {"available": False, "reason": "Insufficient indoor–outdoor variation."}
    coefficient = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / spread
    if coefficient <= 0:
        return {"available": False, "reason": "Heat-demand relation is not physically supported."}
    intercept = my - coefficient * mx
    rmse = sqrt(mean((y - coefficient * x - intercept) ** 2 for x, y in zip(xs, ys)))
    return {
        "available": True,
        "w_per_k": coefficient,
        "intercept_w": intercept,
        "rmse_w": rmse,
        "sample_count": len(xs),
        "gap_range_k": [min(xs), max(xs)],
        "scope": "whole-house measured space heat; no room kWh attribution",
    }


def demand_estimate(model, gap_k, hours):
    if not model.get("available") or gap_k < 0 or hours <= 0:
        return {"available": False}
    kw = max(0, model["w_per_k"] * gap_k + model["intercept_w"]) / 1000
    return {
        "available": True,
        "thermal_kw": kw,
        "thermal_kwh": kw * hours,
        "duration_hours": hours,
        "extrapolated": not model["gap_range_k"][0] <= gap_k <= model["gap_range_k"][1],
        "residual_spread_kw": model["rmse_w"] / 1000,
    }


def fit_bundle(config, episodes, previous=None, fitted_at=None):
    models = fit_models(config, episodes, previous, fitted_at)
    windows = []
    definition = config.get("energy", {}).get("thermal", {})
    for episode in episodes:
        points = episode["points"]
        if not points or episode.get("signature") != signature(config) or episode.get("synthetic"):
            continue
        first = points[0]["time"]
        blocks = [
            [point for point in points if start <= point["time"] <= start + 3600]
            for start in range(int(first), int(points[-1]["time"]), 3600)
        ]
        for block in blocks:
            if not block:
                continue
            if block[-1]["time"] - block[0]["time"] < 3540 or any(
                b["time"] - a["time"] > 600 for a, b in zip(block, block[1:])
            ):
                continue
            temps = [
                mean(p["rooms"].values())
                for p in block
                if p.get("rooms") and all(v is not None for v in p["rooms"].values())
            ]
            if len(temps) != len(block) or any(p.get("outdoor") is None for p in block):
                continue
            energy = energy_intervals(block, "thermal", definition)
            duration = (block[-1]["time"] - block[0]["time"]) / 3600
            windows.append(
                {
                    "stable": max(temps) - min(temps) <= 0.3,
                    "coverage": energy["coverage"],
                    "space_heat_confirmed": definition.get("scope") == "space_heating"
                    and all(
                        p.get("classification") == "space_heat_delivered"
                        and not p.get("contaminated")
                        for p in block
                    ),
                    "gap_k": mean(temps) - mean(p["outdoor"] for p in block),
                    "thermal_w": energy["kwh"] * 1000 / duration
                    if energy["kwh"] is not None
                    else None,
                }
            )
    models["heat_demand"] = heat_demand(windows)
    return models
