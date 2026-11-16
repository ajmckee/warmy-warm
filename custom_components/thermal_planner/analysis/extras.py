"""Optional replay channels; missing telemetry never removes temperature reports."""

from bisect import bisect_right
from collections import Counter

from . import timestamp
from .comfort import comfort
from .signals import classify, dew_point, energy_summary, finite


def enrich(report, history, times, traces, config, age):
    indexed = {}
    for entity, records in history.items():
        values = {timestamp(record["timestamp"]).timestamp(): record for record in records}
        indexed[entity] = (sorted(values), values)

    def reading(entity, when):
        stamps, records = indexed.get(entity, ([], {}))
        index = bisect_right(stamps, when) - 1
        return records[stamps[index]] if index >= 0 and when - stamps[index] <= age else None

    humidities = {}
    for room in config["rooms"]:
        name = room["name"]
        humidity = []
        for when in times:
            record = reading(room.get("humidity"), when)
            value = finite(record["value"]) if record and record.get("unit") == "%" else None
            humidity.append(value if value is not None and 0 <= value <= 100 else None)
        if room.get("humidity"):
            humidities[name] = humidity
            report["rooms"][name]["humidity"] = {
                "start_percent": humidity[0],
                "end_percent": humidity[-1],
                "coverage": sum(v is not None for v in humidity) / len(humidity),
                "dew_point_c": dew_point(traces[name][-1], humidity[-1]),
            }
        if room.get("comfort_estimate"):
            from .signals import temperature

            radiant = reading(room.get("radiant_temperature"), times[-1])
            report["rooms"][name]["comfort_estimate"] = comfort(
                traces[name][-1],
                humidity[-1],
                room.get("comfort_assumptions"),
                temperature(radiant["value"], radiant.get("unit")) if radiant else None,
            )
    if humidities:
        report["traces"]["humidity"] = humidities
    source = config.get("heat_source", {})
    points = []
    categories = Counter()
    for when in times:
        climate = reading(source.get("climate"), when)
        heating = reading(source.get("space_heating"), when)
        mode = reading(source.get("operating_mode"), when)
        category = classify(
            climate["value"] if climate else None,
            heating["value"] if heating else None,
            mode["value"] if mode else None,
            source,
        )
        categories[category] += 1
        point = {"time": when}
        for name, definition in config.get("energy", {}).items():
            record = reading(definition.get("entity"), when)
            point[name] = {"value": record["value"], "unit": record.get("unit")} if record else None
        points.append(point)
    if config.get("energy"):
        report["energy"] = energy_summary(points, config["energy"], min(age, 600))
    if source.get("climate") or source.get("space_heating"):
        report["delivery_classification"] = {
            key: count / len(times) for key, count in categories.items()
        }
        if categories.get("space_heat_stopped") == len(times):
            report["label"] = (
                "confirmed cessation of space heat by selected indication; residual heat/circulation may remain"
            )
        elif categories.get("climate_off") == len(times):
            report["label"] = (
                "climate off; actual heat delivery unverified; residual heat/circulation may remain"
            )
        if categories.get("dhw") or categories.get("defrost"):
            report["contamination"] = (
                "DHW/defrost overlaps this window; do not use it as a clean cooldown episode."
            )
