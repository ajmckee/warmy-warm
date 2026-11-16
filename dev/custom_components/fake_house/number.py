"""A readback-capable direct flow target for offline control trials."""

from homeassistant.components.number import NumberEntity
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from . import DOMAIN, SIGNAL


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    async_add_entities([FlowTarget(hass.data[DOMAIN])])


class FlowTarget(NumberEntity):
    _attr_name = "Fake House Flow Target"
    _attr_unique_id = "fake_house_flow_target"
    _attr_native_min_value = 20
    _attr_native_max_value = 60
    _attr_native_step = 0.5
    _attr_native_unit_of_measurement = "°C"
    _attr_should_poll = False
    _attr_force_update = True

    def __init__(self, simulator):
        self.simulator = simulator

    @property
    def native_value(self):
        return self.simulator.house.flow_target

    async def async_set_native_value(self, value):
        self.simulator.house.flow_target = value
        await self.simulator.save()

    async def async_added_to_hass(self):
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL, self.async_write_ha_state))
