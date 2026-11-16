"""Explicit simulated space-heating delivery; DHW/defrost remain distinct."""

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from . import DOMAIN, SIGNAL


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    async_add_entities([SpaceHeating(hass.data[DOMAIN])])


class SpaceHeating(BinarySensorEntity):
    _attr_name = "Fake House Space Heating"
    _attr_unique_id = "fake_house_space_heating"
    _attr_should_poll = False
    _attr_force_update = True

    def __init__(self, simulator):
        self.simulator = simulator

    @property
    def is_on(self):
        return (
            self.simulator.house.mode == "heat"
            and self.simulator.house.operating_mode == "space_heat"
        )

    async def async_added_to_hass(self):
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL, self.async_write_ha_state))
