"""Diagnostic sensors for Georgia Power.

The Energy Dashboard is fed by external statistics, not by these entities. These exist
so the integration is observable - in particular `last_reading`, which is what a
staleness automation should watch: if it stops advancing, the scraper has broken.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import datetime as dt
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import GaPowerConfigEntry
from .const import DOMAIN
from .coordinator import GaPowerCoordinator


@dataclass(frozen=True, kw_only=True)
class GaPowerSensorDescription(SensorEntityDescription):
    """Describes a Georgia Power sensor."""

    value_fn: Callable[[dict[str, Any]], Any]


SENSORS: tuple[GaPowerSensorDescription, ...] = (
    GaPowerSensorDescription(
        key="last_reading",
        translation_key="last_reading",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.get("last_reading"),
    ),
    # No state_class on these two. They report a single completed hour's value, which
    # jumps backwards in time as data arrives - not a live measurement and not a
    # running total. HA rejects MEASUREMENT for both the energy and monetary device
    # classes; omitting state_class keeps the device class (and its formatting/icon)
    # while leaving these out of long-term statistics, which is correct - the real
    # statistics come from the external import in the coordinator, not from here.
    GaPowerSensorDescription(
        key="latest_hour_usage",
        translation_key="latest_hour_usage",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        value_fn=lambda d: d.get("latest_kwh"),
    ),
    GaPowerSensorDescription(
        key="latest_hour_cost",
        translation_key="latest_hour_cost",
        device_class=SensorDeviceClass.MONETARY,
        value_fn=lambda d: d.get("latest_cost"),
    ),
    GaPowerSensorDescription(
        key="imported_rows",
        translation_key="imported_rows",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.get("imported"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GaPowerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the diagnostic sensors."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            *(GaPowerSensor(coordinator, entry, d) for d in SENSORS),
            GaPowerStatusSensor(coordinator, entry),
        ]
    )


class GaPowerSensor(CoordinatorEntity[GaPowerCoordinator], SensorEntity):
    """A single diagnostic value from the last coordinator run."""

    _attr_has_entity_name = True
    entity_description: GaPowerSensorDescription

    def __init__(
        self,
        coordinator: GaPowerCoordinator,
        entry: GaPowerConfigEntry,
        description: GaPowerSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Georgia Power",
            manufacturer="Southern Company",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> Any:
        if not self.coordinator.data:
            return None
        value = self.entity_description.value_fn(self.coordinator.data)
        if isinstance(value, dt.datetime):
            return value
        return value

    @property
    def native_unit_of_measurement(self) -> str | None:
        if self.entity_description.device_class is SensorDeviceClass.MONETARY:
            return self.hass.config.currency
        return self.entity_description.native_unit_of_measurement


class GaPowerStatusSensor(CoordinatorEntity[GaPowerCoordinator], SensorEntity):
    """Whether the integration is working, and if not, what stopped it.

    Deliberately always available. Every other entity here reports a value from the
    last poll, so all of them go unavailable the moment a poll fails - which is
    exactly the moment something needs to be able to explain itself. An automation
    watching only those can say "unavailable" and nothing more, which is what the
    first version of the staleness alert did. This one stays up and names the cause.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["ok", "invalid_auth", "utility_outage", "blocked", "error"]

    def __init__(
        self, coordinator: GaPowerCoordinator, entry: GaPowerConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_status"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Georgia Power",
            manufacturer="Southern Company",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def available(self) -> bool:
        """Always. The whole point of this entity is to survive a failed poll."""
        return True

    @property
    def native_value(self) -> str:
        if self.coordinator.last_update_success:
            return "ok"
        # A failure classified before the first successful poll may not have been
        # recorded yet (setup can fail outside _async_update_data), so fall back
        # rather than reporting an option that isn't in the list.
        return self.coordinator.last_error_kind or "error"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        failing = not self.coordinator.last_update_success
        return {
            "last_error": self.coordinator.last_error if failing else None,
            "last_success": self.coordinator.last_success,
        }
