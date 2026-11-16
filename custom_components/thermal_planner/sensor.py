"""Native status and summary entities, without bulky historical attributes."""

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo

from .analysis import timestamp
from .const import DOMAIN
from .control import CONTROL_KEY


async def async_setup_entry(hass, entry, async_add_entities):
    manager = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            ThermalSensor(manager, key)
            for key in (
                "status",
                "result",
                "next_action",
                "last_report",
                "first_cooling_room",
                "report_quality",
                "lab_run",
                "planner_status",
                "control_mode",
                "campaign_completed",
            )
        ]
    )


class ThermalSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, manager, key):
        self.manager = manager
        self.key = key
        self._attr_name = key.replace("_", " ").capitalize()
        self._attr_unique_id = f"{manager.entry.entry_id}_{key}"
        if key == "last_report":
            self._attr_device_class = SensorDeviceClass.TIMESTAMP
        if key == "report_quality":
            self._attr_name = "Indoor data quality"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, manager.entry.entry_id)},
            name="Thermal Planner",
            manufacturer="Thermal Planner",
            model="Read-only replay",
        )

    async def async_added_to_hass(self):
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, f"{DOMAIN}_{self.manager.entry.entry_id}", self.async_write_ha_state
            )
        )

    @property
    def native_value(self):
        engine = getattr(self.manager, "engine", None)
        if self.key == "planner_status":
            return engine.plan["status"] if engine and engine.plan else "not_planned"
        if self.key == "control_mode":
            return engine.mode if engine else "advisory"
        if self.key == "campaign_completed":
            return engine.campaign["completed"] if engine and engine.campaign else 0
        if self.key == "status":
            return self.manager.status
        if self.key == "next_action":
            return "Open Thermal Planner in the sidebar"
        report = self.manager.latest
        if self.key == "last_report":
            return timestamp(report["end"]) if report else None
        if self.key == "first_cooling_room":
            return report["limiting_room"] if report else None
        if self.key == "report_quality":
            return (
                (
                    "complete"
                    if all(
                        row["coverage"] == 1
                        for window in report["windows"]
                        for row in window["rooms"].values()
                    )
                    else "missing_or_stale"
                )
                if report
                else None
            )
        if self.key == "lab_run":
            controller = self.manager.hass.data.get(CONTROL_KEY)
            return controller.journal["phase"] if controller and controller.journal else "idle"
        return report["end"] if report else "No report"

    @property
    def extra_state_attributes(self):
        report = self.manager.latest
        return {
            "entry_id": self.manager.entry.entry_id,
            "report_id": report["id"] if report else None,
            "limiting_room": report["limiting_room"] if report else None,
            "report_url": "/thermal-planner",
            "plan_id": engine.plan["id"]
            if (engine := getattr(self.manager, "engine", None)) and engine.plan
            else None,
        }
