"""Advisory Fanger PMV/PPD, with conservative ISO applicability limits.

Heat-balance equations from ISO 7730; reference examples and applicability:
https://pythermalcomfort.readthedocs.io/en/latest/documentation/models.html
This reports a vote/index, never a measured or invented 'feels-like °C'.
"""

from math import exp, sqrt

from .signals import dew_point, finite


def comfort(air_c, humidity, assumptions=None, radiant_c=None):
    assumptions = dict(assumptions or {})
    met, clo, speed = (
        assumptions.get("met", 1.1),
        assumptions.get("clo", 1.0),
        assumptions.get("air_speed", 0.1),
    )
    radiant = radiant_c if radiant_c is not None else air_c
    explanation = {
        "met": met,
        "clo": clo,
        "relative_air_speed_m_s": speed,
        "radiant_c": radiant,
        "radiant_source": "measured"
        if radiant_c is not None
        else "air temperature proxy; warm-floor radiation is not measured",
        "external_work_met": 0,
        "model": "Fanger PMV/PPD (ISO 7730 heat balance)",
    }
    humidity = finite(humidity)
    result = {
        "pmv": None,
        "ppd_percent": None,
        "dew_point_c": dew_point(air_c, humidity),
        "humidity_percent": humidity if humidity is not None and 0 <= humidity <= 100 else None,
        "assumptions": explanation,
        "quality": "unavailable",
    }
    if air_c is None or radiant is None or humidity is None or not 0 <= humidity <= 100:
        return result
    if not (
        10 < air_c < 30 and 10 < radiant < 40 and 0 < speed < 1 and 0.8 < met < 4 and 0 < clo < 2
    ):
        result["quality"] = "outside_model_limits"
        return result
    vapor = humidity * 10 * exp(16.6536 - 4030.183 / (air_c + 235))
    if not 0 < vapor < 2700:
        result["quality"] = "outside_model_limits"
        return result
    metabolic = met * 58.15
    insulation = 0.155 * clo
    area = 1 + 1.29 * insulation if insulation <= 0.078 else 1.05 + 0.645 * insulation

    def exchange(surface):
        convection = max(12.1 * sqrt(speed), 2.38 * abs(surface - air_c) ** 0.25)
        radiative = 3.96e-8 * area * ((surface + 273) ** 4 - (radiant + 273) ** 4)
        convective = area * convection * (surface - air_c)
        return radiative + convective

    low, high = -50.0, 80.0
    for _ in range(80):
        surface = (low + high) / 2
        residual = surface - (35.7 - 0.028 * metabolic) + insulation * exchange(surface)
        if residual > 0:
            high = surface
        else:
            low = surface
    loss = (
        0.00305 * (5733 - 6.99 * metabolic - vapor)
        + max(0, 0.42 * (metabolic - 58.15))
        + 1.7e-5 * metabolic * (5867 - vapor)
        + 0.0014 * metabolic * (34 - air_c)
        + exchange((low + high) / 2)
    )
    pmv = (0.303 * exp(-0.036 * metabolic) + 0.028) * (metabolic - loss)
    if not -2 < pmv < 2:
        result["quality"] = "outside_model_limits"
        return result
    result.update(
        pmv=pmv,
        ppd_percent=100 - 95 * exp(-0.03353 * pmv**4 - 0.2179 * pmv**2),
        quality="estimated_under_assumptions",
    )
    return result
