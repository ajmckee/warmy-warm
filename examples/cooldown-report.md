# Synthetic cooldown report

**2026-01-01T01:00:00+01:00 → 2026-01-01T05:00:00+01:00**

observed indoor change; heat delivery unverified

Outdoor source: sensor.ambient_73


**2026-01-01T01:00:00+01:00 → 2026-01-01T02:00:00+01:00**

| Type / name | Start → end °C | Δ °C | °C/h | Coverage | Outside coverage | Outside °C | Gap K | °C/h at 10 K | Quality |
|---|---|---|---|---|---|---|---|---|---|
| rooms: Study | 23.500 → 22.500 | -1.000 | -1.000 | 100% | 100% | 10.000 | 13.000 | -0.769 | complete |
| rooms: Kitchen | 22.000 → 21.750 | -0.250 | -0.250 | 100% | 100% | 10.000 | 11.875 | -0.211 | complete |
| rooms: Bathroom | 21.000 → 19.000 | -2.000 | -2.000 | 100% | 100% | 10.000 | 10.000 | -2.000 | complete |
| rooms: Bedroom | 22.000 → 21.500 | -0.500 | -0.500 | 100% | 100% | 10.000 | 11.750 | -0.426 | complete |
| floors: Ground | 23.000 → 22.250 | -0.750 | -0.750 | 100% | 100% | 10.000 | 12.625 | -0.594 | complete |
| floors: Upper | 22.000 → 21.500 | -0.500 | -0.500 | 100% | 100% | 10.000 | 11.750 | -0.426 | complete |

**2026-01-01T02:00:00+01:00 → 2026-01-01T03:00:00+01:00**

| Type / name | Start → end °C | Δ °C | °C/h | Coverage | Outside coverage | Outside °C | Gap K | °C/h at 10 K | Quality |
|---|---|---|---|---|---|---|---|---|---|
| rooms: Study | 22.500 → 21.500 | -1.000 | -1.000 | 100% | 100% | 10.000 | 12.000 | -0.833 | complete |
| rooms: Kitchen | 21.750 → 21.500 | -0.250 | -0.250 | 100% | 100% | 10.000 | 11.625 | -0.215 | complete |
| rooms: Bathroom | 19.000 → 17.000 | -2.000 | -2.000 | 100% | 100% | 10.000 | 8.000 | -2.500 | complete |
| rooms: Bedroom | 21.500 → 21.000 | -0.500 | -0.500 | 100% | 100% | 10.000 | 11.250 | -0.444 | complete |
| floors: Ground | 22.250 → 21.500 | -0.750 | -0.750 | 100% | 100% | 10.000 | 11.875 | -0.632 | complete |
| floors: Upper | 21.500 → 21.000 | -0.500 | -0.500 | 100% | 100% | 10.000 | 11.250 | -0.444 | complete |

**2026-01-01T03:00:00+01:00 → 2026-01-01T05:00:00+01:00**

| Type / name | Start → end °C | Δ °C | °C/h | Coverage | Outside coverage | Outside °C | Gap K | °C/h at 10 K | Quality |
|---|---|---|---|---|---|---|---|---|---|
| rooms: Study | 21.500 → 19.500 | -2.000 | -1.000 | 100% | 100% | 10.000 | 10.500 | -0.952 | complete |
| rooms: Kitchen | 21.500 → 21.000 | -0.500 | -0.250 | 100% | 100% | 10.000 | 11.250 | -0.222 | complete |
| rooms: Bathroom | 17.000 → 13.000 | -4.000 | -2.000 | 100% | 100% | 10.000 | 5.000 | -4.000 | complete |
| rooms: Bedroom | 21.000 → 20.000 | -1.000 | -0.500 | 100% | 100% | 10.000 | 10.500 | -0.476 | complete |
| floors: Ground | 21.500 → 20.000 | -1.500 | -0.750 | 100% | 100% | 10.000 | 10.750 | -0.698 | complete |
| floors: Upper | 21.000 → 20.000 | -1.000 | -0.500 | 100% | 100% | 10.000 | 10.500 | -0.476 | complete |

Study: 0.5°C drop 0.500 h; 1°C drop 1.000 h. Observation 4.00 h. Comfort minimum breached: False; guard coverage 100%.
Kitchen: 0.5°C drop 2.000 h; 1°C drop 4.000 h. Observation 4.00 h. Comfort minimum breached: False; guard coverage 100%.
Bathroom: 0.5°C drop 0.250 h; 1°C drop 0.500 h. Observation 4.00 h. Comfort minimum breached: True; guard coverage 100%.
Bedroom: 0.5°C drop 1.000 h; 1°C drop 2.000 h. Observation 4.00 h. Comfort minimum breached: False; guard coverage 100%.

First constraining room to drop 0.5°C: Bathroom
Report ID: `synthetic-fixture`. Export through the Thermal Planner: Export CSV action.
