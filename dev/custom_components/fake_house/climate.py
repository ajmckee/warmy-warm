"""A real HA climate API backed only by the toy house."""

from homeassistant.components.climate import ClimateEntity, ClimateEntityFeature, HVACMode
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from . import DOMAIN, SIGNAL


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    async_add_entities([FakeHeatPump(hass.data[DOMAIN])])


class FakeHeatPump(ClimateEntity):
    _attr_name = "Fake House Heat Pump"
    _attr_unique_id = "fake_house_heat_pump"
    _attr_should_poll = False
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF]
    _attr_min_temp = 10
    _attr_max_temp = 30
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(self, simulator):
        self.simulator = simulator

    async def async_added_to_hass(self):
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL, self.async_write_ha_state))

    @property
    def hvac_mode(self):
        return HVACMode(self.simulator.house.mode)

    @property
    def target_temperature(self):
        return self.simulator.house.target

    @property
    def current_temperature(self):
        return self.simulator.house.radiator

    async def async_set_hvac_mode(self, hvac_mode):
        self.simulator.house.mode = HVACMode(hvac_mode).value
        await self.simulator.save()

    async def async_set_temperature(self, **kwargs):
        self.simulator.house.target = kwargs[ATTR_TEMPERATURE]
        await self.simulator.save()
