"""Fresh deterministic inputs; no live HA, MQTT, tokens, sleeps or random seeds."""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from homeassistant import loader
from homeassistant.config_entries import ConfigEntries
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry, device_registry, entity_registry, floor_registry
from homeassistant.setup import async_setup_component

from dev.custom_components.fake_house.model import House


@pytest.fixture
def fixture():
    return json.loads((Path(__file__).parent / "fixtures/cooldown.json").read_text())


@pytest.fixture
def fake_house():
    return House()


@pytest.fixture
def simulated_cooldown(fake_house):
    """Four exact hours at five-minute cadence, timestamped independently of today."""
    start = datetime(2026, 1, 1, tzinfo=UTC)
    fake_house.mode = "off"
    history = {
        f"sensor.fake_house_{room}": [] for room in ("radiator", "ufh", "bathroom", "outdoor")
    }
    for minute in range(0, 241, 5):
        for entity, samples in history.items():
            samples.append(
                {
                    "timestamp": (start + timedelta(minutes=minute)).isoformat(),
                    "value": getattr(fake_house, entity.removeprefix("sensor.fake_house_")),
                    "unit": "°C",
                }
            )
        if minute < 240:
            fake_house.step(5)
    rooms = []
    for name, weight, emitter in (
        ("radiator", 2, "radiator"),
        ("ufh", 1, "underfloor"),
        ("bathroom", 0, "radiator"),
    ):
        rooms.append(
            {
                "name": name,
                "floor": "Ground",
                "circuit": emitter,
                "emitter": emitter,
                "sensors": [f"sensor.fake_house_{name}"],
                "weights": [1],
                "diagnostics": [],
                "floor_weight": weight,
                "comfort_min": 19,
                "constrains": True,
            }
        )
    return deepcopy(
        {
            "config": {
                "rooms": rooms,
                "outdoor": "sensor.fake_house_outdoor",
                "weather": None,
                "max_age_minutes": 15,
            },
            "history": history,
            "start": start.isoformat(),
            "end": (start + timedelta(hours=4)).isoformat(),
        }
    )


@pytest.fixture
async def simulator_factory(tmp_path, monkeypatch):
    import custom_components

    monkeypatch.setattr(
        custom_components,
        "__path__",
        [
            str(Path(__file__).resolve().parents[1] / "dev/custom_components"),
            *custom_components.__path__,
        ],
    )
    instances = []

    async def start():
        hass = HomeAssistant(str(tmp_path))
        instances.append(hass)
        hass.config.time_zone = "Europe/Warsaw"
        hass.config.skip_pip = True
        hass.config_entries = ConfigEntries(hass, {})
        loader.async_setup(hass)
        if hasattr(hass.config_entries, "async_wait_initialized"):
            await hass.config_entries.async_initialize()
        await floor_registry.async_load(hass)
        await area_registry.async_load(hass)
        if hasattr(device_registry, "async_setup"):
            device_registry.async_setup(hass)
        await device_registry.async_load(hass)
        await entity_registry.async_load(hass)
        assert await async_setup_component(hass, "fake_house", {"fake_house": {"auto_tick": False}})
        await hass.async_block_till_done()
        return hass

    yield start
    for hass in instances:
        await hass.async_stop(force=True)
