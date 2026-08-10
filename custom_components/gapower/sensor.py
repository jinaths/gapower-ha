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
        GaPowerSensor(coordinator, entry, description) for description in SENSORS
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
