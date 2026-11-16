"""Recorder-visible temperatures from the development simulator."""

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import UnitOfTemperature
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from . import DOMAIN, SIGNAL


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    async_add_entities(
        [
            Temperature(hass.data[DOMAIN], room)
            for room in ("radiator", "ufh", "bathroom", "outdoor")
        ]
        + [
            Reading(hass.data[DOMAIN], key, label, unit, device_class, state_class)
            for key, label, unit, device_class, state_class in (
                (
                    "humidity",
                    "Humidity",
                    "%",
                    SensorDeviceClass.HUMIDITY,
                    SensorStateClass.MEASUREMENT,
                ),
                (
                    "leaving_water",
                    "Leaving Water",
                    "°C",
                    SensorDeviceClass.TEMPERATURE,
                    SensorStateClass.MEASUREMENT,
                ),
                (
                    "electrical_power",
                    "Electrical Power",
                    "kW",
                    SensorDeviceClass.POWER,
                    SensorStateClass.MEASUREMENT,
                ),
                (
                    "thermal_power",
                    "Thermal Power",
                    "kW",
                    SensorDeviceClass.POWER,
                    SensorStateClass.MEASUREMENT,
                ),
                (
                    "electrical_kwh",
                    "Electrical Energy",
                    "kWh",
                    SensorDeviceClass.ENERGY,
                    SensorStateClass.TOTAL_INCREASING,
                ),
                (
                    "thermal_kwh",
                    "Thermal Energy",
                    "kWh",
                    SensorDeviceClass.ENERGY,
                    SensorStateClass.TOTAL_INCREASING,
                ),
                ("operating_mode", "Operating Mode", None, None, None),
            )
        ]
    )


class Temperature(SensorEntity):
    _attr_should_poll = False
    _attr_force_update = True
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, simulator, room):
        self.simulator = simulator
        self.room = room
        self._attr_name = f"Fake House {room.title()}"
        self._attr_unique_id = f"fake_house_{room}"

    @property
    def native_value(self):
        return round(getattr(self.simulator.house, self.room), 6)

    async def async_added_to_hass(self):
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL, self.async_write_ha_state))


class Reading(Temperature):
    def __init__(self, simulator, key, label, unit, device_class, state_class):
        self.simulator, self.room = simulator, key
        self._attr_name = f"Fake House {label}"
        self._attr_unique_id = f"fake_house_{key}"
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        self._attr_state_class = state_class

    @property
    def native_value(self):
        value = getattr(self.simulator.house, self.room)
        return round(value, 6) if isinstance(value, (float, int)) else value
