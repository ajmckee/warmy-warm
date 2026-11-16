"""Deterministic toy house. No HA imports, randomness, wall clock, or device I/O."""

from dataclasses import dataclass
from math import isfinite


@dataclass
class House:
    mode: str = "heat"
    target: float = 23.0
    radiator: float = 22.0
    ufh: float = 22.0
    bathroom: float = 21.0
    outdoor: float = 8.0
    elapsed_minutes: float = 0.0
    flow_target: float = 40.0
    humidity: float = 50.0
    electrical_kwh: float = 0.0
    thermal_kwh: float = 0.0
    operating_mode: str = "space_heat"

    @property
    def electrical_power(self):
        return 2 * (self.flow_target / 40) ** 2 if self.mode == "heat" else 0.05

    @property
    def thermal_power(self):
        return (
            6 * self.flow_target / 40
            if self.mode == "heat" and self.operating_mode == "space_heat"
            else 0
        )

    @property
    def leaving_water(self):
        return self.flow_target if self.mode == "heat" else self.outdoor

    def step(self, minutes: float) -> None:
        """Linear cooldown/recovery, bounded by outside/target temperatures.

        Rates are test inputs, not an identified physical building model.
        UFH responds more slowly than the radiator in both directions.
        """
        if not isfinite(minutes) or not 0 < minutes <= 1440:
            raise ValueError("minutes must be finite and in (0, 1440]")
        self.electrical_kwh += self.electrical_power * minutes / 60
        self.thermal_kwh += self.thermal_power * minutes / 60
        for room, cooling, heating in (
            ("radiator", 1.0, 2.0),
            ("ufh", 0.25, 0.5),
            ("bathroom", 1.5, 2.0),
        ):
            value = getattr(self, room)
            if self.mode == "heat" and self.operating_mode == "space_heat" and value < self.target:
                value = min(self.target, value + heating * self.flow_target / 40 * minutes / 60)
            else:
                value = max(self.outdoor, value - cooling * minutes / 60)
            setattr(self, room, value)
        self.elapsed_minutes += minutes
