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
from homeassistant.const import EntityCategory, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import GaPowerConfigEntry
from .billing import demand_all_in
from .const import DOMAIN
from .coordinator import GaPowerCoordinator


@dataclass(frozen=True, kw_only=True)
class GaPowerSensorDescription(SensorEntityDescription):
    """Describes a Georgia Power sensor."""

    value_fn: Callable[[dict[str, Any]], Any]
    # Optional extra attributes. Used by billed_demand to carry the whole demand
    # picture on one entity, so an alert can be rendered from a single state object
    # rather than joining several sensors that update at slightly different moments.
    attrs_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None


def _demand_attrs(d: dict[str, Any]) -> dict[str, Any]:
    """Everything an alert needs about the cycle's demand, on one entity."""
    return {
        "peak_kwh": d.get("cycle_peak"),
        "peak_time": d.get("cycle_peak_time"),
        # The bill's "Demand" line, and what it costs once riders, franchise fee and
        # sales tax are added. Billing is linear, so there is no bracket to aim for:
        # every 0.1 kWh off the worst hour is worth the same (per_tenth_all_in).
        "charge": d.get("demand_charge"),
        "charge_all_in": d.get("demand_charge_all_in"),
        "per_tenth_all_in": demand_all_in(0.1),
        "bill_projection": d.get("bill_projection"),
        "missing_hours": d.get("missing_hours"),
        "data_through": d.get("data_through"),
        "cycle_start": d.get("cycle_start"),
        "cycle_end": d.get("cycle_end"),
        "cycle_day": d.get("cycle_day"),
        "cycle_length": d.get("cycle_length"),
    }


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
    # --- demand charge -------------------------------------------------------
    # Georgia Power bills the single highest-usage hour of the whole cycle at
    # $12.44/kW, in decimal kW - no rounding. On a recent cycle that one hour was
    # worth $57.47 ($72.91 all-in) against $6.43 for all the on-peak energy put
    # together, so these are the expensive numbers on the bill, not the kWh totals.
    GaPowerSensorDescription(
        key="cycle_peak",
        translation_key="cycle_peak",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        value_fn=lambda d: d.get("cycle_peak"),
        attrs_fn=lambda d: {"peak_time": d.get("cycle_peak_time")},
    ),
    GaPowerSensorDescription(
        key="billed_demand",
        translation_key="billed_demand",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda d: d.get("billed_demand"),
        attrs_fn=_demand_attrs,
    ),
    GaPowerSensorDescription(
        key="demand_charge",
        translation_key="demand_charge",
        device_class=SensorDeviceClass.MONETARY,
        value_fn=lambda d: d.get("demand_charge"),
        attrs_fn=lambda d: {"all_in": d.get("demand_charge_all_in")},
    ),
    # The whole bill for the current cycle, projected from the hours seen so far
    # with the formula verified on printed bills. See coordinator._projection for
    # why it fills the portal's missing hours before scaling.
    GaPowerSensorDescription(
        key="bill_projection",
        translation_key="bill_projection",
        device_class=SensorDeviceClass.MONETARY,
        value_fn=lambda d: d.get("bill_projection"),
        attrs_fn=lambda d: {
            "cycle_kwh_recorded": d.get("cycle_kwh"),
            "data_through": d.get("data_through"),
        },
    ),
    # Hours this cycle the portal reported as 0 kWh or not at all. A house never
    # draws zero for an hour; these are gaps in the utility's data, and they make
    # every derived number here less certain.
    GaPowerSensorDescription(
        key="missing_hours",
        translation_key="missing_hours",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="h",
        value_fn=lambda d: d.get("missing_hours"),
    ),
    # Exposed so the window these are measured over is visible rather than implied -
    # it is meter-read driven, lands on day 25-28, and runs 29-32 days.
    GaPowerSensorDescription(
        key="cycle_start",
        translation_key="cycle_start",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.get("cycle_start"),
        attrs_fn=lambda d: {"cycle_end": d.get("cycle_end")},
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
    def extra_state_attributes(self) -> dict[str, Any] | None:
        fn = self.entity_description.attrs_fn
        if fn is None or not self.coordinator.data:
            return None
        return fn(self.coordinator.data)

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
