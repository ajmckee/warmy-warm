"""Explicit synthetic learning episodes for the local development heat source."""

from datetime import datetime, timedelta, timezone
from math import pi, sin

from ..configuration import signature, source_fingerprint
from .learning import predict


def demo_episodes(config, baseline_flow=None, alternate_flow=None, count=12):
    groups = [baseline_flow] + ([alternate_flow] if alternate_flow is not None else [])
    episodes = []
    for group, flow in enumerate(groups):
        ratio = flow / baseline_flow if flow is not None and baseline_flow else 1
        for day in range(count):
            start = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=group * count + day)
            heats = [0] * 48 + [1] * 96
            outside = [2 + day % 8 * 1.5 + 2 * sin(i / 144 * pi * 2) for i in range(145)]
            traces = {}
            for room in config["rooms"]:
                ufh = room.get("emitter") in ("underfloor", "mixed")
                loss = 0.025 if ufh else 0.08
                model = {
                    "loss_per_hour": loss,
                    "heating_c_per_hour": loss * 25 * ratio,
                    "tau_hours": 1.5 if ufh else 0,
                }
                traces[room["name"]] = predict(
                    model,
                    max(22, room["comfort_min"] + 2),
                    outside[:-1],
                    heats,
                    initial_memory=0.25,
                )
            points = [
                {
                    "time": (start + timedelta(minutes=5 * i)).timestamp(),
                    "rooms": {name: values[i] for name, values in traces.items()},
                    "outdoor": outside[i],
                    "heat": heats[min(i, len(heats) - 1)],
                    "mode": "off" if i < 48 else "heat",
                    "phase": "cooldown" if i < 48 else "recovery",
                    "flow_target": flow,
                    "contaminated": False,
                    "classification": "space_heat_stopped" if i < 48 else "space_heat_delivered",
                    "electrical": {"value": 0.05 if i < 48 else 2 * ratio**2, "unit": "kW"},
                    "thermal": {"value": 0 if i < 48 else 6 * ratio, "unit": "kW"},
                }
                for i in range(145)
            ]
            episodes.append(
                {
                    "id": f"synthetic-{group}-{day}",
                    "started": start.isoformat(),
                    "ended": (start + timedelta(hours=12)).isoformat(),
                    "signature": signature(config),
                    "source_fingerprint": source_fingerprint(config),
                    "configuration": config,
                    "flow_target": flow,
                    "approved_flow_trial": group > 0,
                    "initial_heat": 0.25,
                    "learning_eligible": True,
                    "synthetic": True,
                    "points": points,
                    "outcome": "synthetic",
                    "reason": "Development learning fixture; not measured installation evidence.",
                    "recovery": {"complete": True},
                    "energy": {},
                }
            )
    return episodes
