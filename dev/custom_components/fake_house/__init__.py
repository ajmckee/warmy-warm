"""Opt-in laptop simulator; never packaged with Thermal Planner."""

from dataclasses import asdict
from datetime import timedelta

import voluptuous as vol
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import discovery
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from .model import House

DOMAIN = "fake_house"
SIGNAL = "fake_house_update"
CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Optional("tick_seconds", default=5): vol.All(vol.Coerce(int), vol.Range(min=1)),
                vol.Optional("step_minutes", default=5): vol.All(
                    vol.Coerce(float), vol.Range(min=0.01, max=1440)
                ),
                vol.Optional("auto_tick", default=True): cv.boolean,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


class Simulator:
    def __init__(self, hass):
        self.hass = hass
        self.house = House()
        self.store = Store(hass, 1, DOMAIN)

    @callback
    def publish(self):
        async_dispatcher_send(self.hass, SIGNAL)

    async def save(self):
        # Persist each command, including OFF: restarting the simulator must not
        # hide a missing restart restore in the future planner controller.
        await self.store.async_save(asdict(self.house))
        self.publish()


async def async_setup(hass, config):
    options = config[DOMAIN]
    simulator = Simulator(hass)
    if saved := await simulator.store.async_load():
        simulator.house = House(**saved)
    hass.data[DOMAIN] = simulator

    async def step(call):
        simulator.house.step(call.data["minutes"])
        await simulator.save()

    async def reset(call):
        simulator.house = House()
        await simulator.save()

    async def tick(now):
        simulator.house.step(options["step_minutes"])
        await simulator.save()

    hass.services.async_register(
        DOMAIN,
        "step",
        step,
        schema=vol.Schema(
            {vol.Required("minutes"): vol.All(vol.Coerce(float), vol.Range(min=0.01, max=1440))}
        ),
    )
    hass.services.async_register(DOMAIN, "reset", reset, schema=vol.Schema({}))
    if options["auto_tick"]:
        cancel = async_track_time_interval(hass, tick, timedelta(seconds=options["tick_seconds"]))

        @callback
        def stopped(event):
            cancel()

        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stopped)

    async def conditions(call):
        for key, value in call.data.items():
            setattr(simulator.house, key, value)
        await simulator.save()

    hass.services.async_register(
        DOMAIN,
        "set_conditions",
        conditions,
        schema=vol.Schema(
            {
                vol.Optional("outdoor"): vol.All(vol.Coerce(float), vol.Range(min=-30, max=45)),
                vol.Optional("humidity"): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
                vol.Optional("operating_mode"): vol.In(["space_heat", "dhw", "defrost", "idle"]),
            }
        ),
    )
    for platform in ("climate", "sensor", "number", "binary_sensor", "weather"):
        await discovery.async_load_platform(hass, platform, DOMAIN, {}, config)
    return True
