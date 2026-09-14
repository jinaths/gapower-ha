"""Minimal stand-ins for the homeassistant modules gapower imports.

Only enough surface to let the pure logic be imported and exercised offline - no
network, no recorder, no event loop machinery.
"""
from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


def _mod(name: str, **attrs: Any) -> types.ModuleType:
    m = types.ModuleType(name)
    m.__path__ = []  # make every stub a package so submodules can be registered
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


class _Generic:
    def __class_getitem__(cls, item):  # ConfigEntry[X] / DataUpdateCoordinator[X]
        return cls


class ConfigEntry(_Generic):
    def __init__(self, entry_id="test_entry", data=None):
        self.entry_id = entry_id
        self.data = data or {}
        self.runtime_data = None


class Platform(StrEnum):
    SENSOR = "sensor"


class EntityCategory(StrEnum):
    DIAGNOSTIC = "diagnostic"


class UnitOfEnergy(StrEnum):
    KILO_WATT_HOUR = "kWh"


class UnitOfPower(StrEnum):
    KILO_WATT = "kW"


class SensorDeviceClass(StrEnum):
    TIMESTAMP = "timestamp"
    ENERGY = "energy"
    MONETARY = "monetary"
    POWER = "power"
    ENUM = "enum"


class SensorStateClass(StrEnum):
    MEASUREMENT = "measurement"


@dataclass(frozen=True, kw_only=True)
class SensorEntityDescription:
    key: str
    translation_key: str | None = None
    device_class: Any = None
    native_unit_of_measurement: str | None = None
    state_class: Any = None
    entity_category: Any = None


class _Entity:
    _attr_has_entity_name = False
    entity_description: Any = None

    def __init__(self, *a, **kw):
        pass


class SensorEntity(_Entity):
    pass


class CoordinatorEntity(_Entity, _Generic):
    def __init__(self, coordinator, *a, **kw):
        self.coordinator = coordinator


class DataUpdateCoordinator(_Generic):
    def __init__(self, hass=None, logger=None, name=None, update_interval=None,
                 config_entry=None):
        self.hass = hass
        self.logger = logger
        self.name = name
        self.data = None
        self.last_update_success = True


class UpdateFailed(Exception):
    pass


class ConfigEntryAuthFailed(Exception):
    pass


class StatisticData(dict):
    def __init__(self, **kw):
        super().__init__(**kw)


class StatisticMetaData(dict):
    def __init__(self, **kw):
        super().__init__(**kw)


class StatisticMeanType(StrEnum):
    NONE = "none"


class DeviceEntryType(StrEnum):
    SERVICE = "service"


class DeviceInfo(dict):
    def __init__(self, **kw):
        super().__init__(**kw)


def install() -> None:
    _mod("homeassistant")
    _mod("homeassistant.components")
    _mod("homeassistant.components.recorder", get_instance=lambda hass: None)
    _mod("homeassistant.components.recorder.models",
         StatisticData=StatisticData, StatisticMetaData=StatisticMetaData,
         StatisticMeanType=StatisticMeanType)
    _mod("homeassistant.components.recorder.statistics",
         async_add_external_statistics=lambda *a, **k: None,
         get_last_statistics=lambda *a, **k: {},
         statistics_during_period=lambda *a, **k: {})
    _mod("homeassistant.components.sensor",
         SensorDeviceClass=SensorDeviceClass, SensorEntity=SensorEntity,
         SensorEntityDescription=SensorEntityDescription,
         SensorStateClass=SensorStateClass)
    _mod("homeassistant.config_entries", ConfigEntry=ConfigEntry)
    _mod("homeassistant.const", CONF_PASSWORD="password", CONF_USERNAME="username",
         UnitOfEnergy=UnitOfEnergy, UnitOfPower=UnitOfPower,
         EntityCategory=EntityCategory, Platform=Platform)
    _mod("homeassistant.core", HomeAssistant=object)
    _mod("homeassistant.exceptions", ConfigEntryAuthFailed=ConfigEntryAuthFailed)
    _mod("homeassistant.helpers")
    _mod("homeassistant.helpers.aiohttp_client",
         async_create_clientsession=lambda *a, **k: None)
    _mod("homeassistant.helpers.update_coordinator",
         DataUpdateCoordinator=DataUpdateCoordinator, UpdateFailed=UpdateFailed,
         CoordinatorEntity=CoordinatorEntity)
    _mod("homeassistant.helpers.device_registry",
         DeviceEntryType=DeviceEntryType, DeviceInfo=DeviceInfo)
    _mod("homeassistant.helpers.entity_platform", AddEntitiesCallback=object)
