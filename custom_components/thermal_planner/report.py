"""Compact human-readable report; traces live only in HA storage."""

from zoneinfo import ZoneInfo

from .analysis import timestamp


def markdown_report(report, zone):
    if report is None:
        return "No report yet. Select Replay historical window to begin."

    def number(value):
        return "unavailable" if value is None else f"{value:.3f}"

    def label(value):
        return (
            str(value)
            .replace("|", "/")
            .replace("\n", " ")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    def local(value):
        return timestamp(value).astimezone(ZoneInfo(zone)).isoformat()

    lines = [
        f"**{local(report['start'])} → {local(report['end'])}**",
        report["label"],
        f"Outdoor source: {label(report['outdoor_source'] or 'unavailable')}",
        "",
    ]
    for window in report["windows"]:
        lines.extend(
            [
                f"**{local(window['start'])} → {local(window['end'])}**",
                "",
                "| Type / name | Start → end °C | Δ °C | °C/h | Coverage | Outside coverage | Outside °C | Gap K | °C/h at 10 K | Quality |",
                "|---|---|---|---|---|---|---|---|---|---|",
            ]
        )
        for kind in ("rooms", "floors"):
            for name, row in window[kind].items():
                lines.append(
                    f"| {kind}: {label(name)} | {number(row['start_c'])} → {number(row['end_c'])} | {number(row['delta_c'])} | {number(row['slope_c_per_h'])} | {row['coverage']:.0%} | {row['outside_coverage']:.0%} | {number(row['mean_outside_c'])} | {number(row['mean_gap_k'])} | {number(row['normalized_c_per_h_10k'])} | {row['quality']} |"
                )
        lines.append("")
    for name, room in report["rooms"].items():
        drops = room["thresholds"]
        lines.append(
            f"{label(name)}: 0.5°C drop {number(drops['0.5'])} h; 1°C drop {number(drops['1.0'])} h. Observation {drops['observed_hours']:.2f} h. Comfort minimum breached: {room['comfort_breached']}; guard coverage {room['comfort_coverage']:.0%}."
        )
    lines.extend(
        [
            "",
            f"First included room to cool by 0.5°C: {label(report['limiting_room'] or 'unobserved')}",
            f"Report ID: `{report['id']}`. Export through the Thermal Planner: Export CSV action.",
        ]
    )
    return "\n\n".join(lines[:4]) + "\n" + "\n".join(lines[4:])
