"""Native hourly weather service with deterministic local test forecasts."""

from datetime import datetime, timedelta, timezone
from math import pi, sin

from homeassistant.components.weather import WeatherEntity, WeatherEntityFeature
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from . import DOMAIN, SIGNAL


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    async_add_entities([LocalWeather(hass.data[DOMAIN])])


class LocalWeather(WeatherEntity):
    _attr_name = "Fake House Weather"
    _attr_unique_id = "fake_house_weather"
    _attr_native_temperature_unit = "°C"
    _attr_supported_features = WeatherEntityFeature.FORECAST_HOURLY
    _attr_should_poll = False
    _attr_condition = "cloudy"

    def __init__(self, simulator):
        self.simulator = simulator

    @property
    def native_temperature(self):
        return self.simulator.house.outdoor

    @property
    def humidity(self):
        return self.simulator.house.humidity

    async def async_forecast_hourly(self):
        start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        return [
            {
                "datetime": (start + timedelta(hours=hour)).isoformat(),
                "temperature": self.simulator.house.outdoor + 2 * sin(hour * pi / 12),
                "condition": "cloudy",
            }
            for hour in range(49)
        ]

    async def async_added_to_hass(self):
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL, self.async_write_ha_state))
