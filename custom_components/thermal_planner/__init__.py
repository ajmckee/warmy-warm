"""Local reports, guarded measurements and evidence-based heating planning."""


def _config_schema(config):
    """Use config entries only; keep offline analytics importable without HA."""
    from homeassistant.helpers import config_validation as cv

    from .const import DOMAIN

    return cv.config_entry_only_config_schema(DOMAIN)(config)


CONFIG_SCHEMA = _config_schema


async def async_setup(hass, config):
    from .integration import async_setup as setup

    return await setup(hass, config)


async def async_setup_entry(hass, entry):
    from .integration import async_setup_entry as setup_entry

    return await setup_entry(hass, entry)


async def async_unload_entry(hass, entry):
    from .integration import async_unload_entry as unload_entry

    return await unload_entry(hass, entry)


async def async_remove_entry(hass, entry):
    from .integration import async_remove_entry as remove_entry

    await remove_entry(hass, entry)
