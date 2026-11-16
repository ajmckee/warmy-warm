"""Read-only adapter for the public Recorder history API."""

from datetime import timedelta

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import get_significant_states

from .configuration import all_entities


def _read_history(hass, config, start, end):
    entities = all_entities(config)
    # Fetch an actual lookback, not a synthetic start-state timestamp. This preserves age.
    history = get_significant_states(
        hass,
        start - timedelta(minutes=config["max_age_minutes"]),
        end + timedelta(microseconds=1),
        entity_ids=sorted(entities),
        include_start_time_state=False,
        significant_changes_only=False,
        minimal_response=False,
        no_attributes=False,
    )
    return {
        entity: [
            dict(
                timestamp=state.last_updated.isoformat(),
                value=state.attributes.get("temperature")
                if entity.startswith("weather.")
                else state.state,
                unit=state.attributes.get("temperature_unit")
                if entity.startswith("weather.")
                else state.attributes.get("unit_of_measurement"),
            )
            for state in states
        ]
        for entity, states in history.items()
    }


async def async_history(hass, config, start, end):
    """Query and materialize lazy Recorder states on its executor."""
    return await get_instance(hass).async_add_executor_job(_read_history, hass, config, start, end)
