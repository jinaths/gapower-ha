"""The Georgia Power integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import GaPowerCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]

# Plain alias rather than PEP 695 `type` syntax, which needs Python 3.12+.
GaPowerConfigEntry = ConfigEntry[GaPowerCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: GaPowerConfigEntry) -> bool:
    """Set up Georgia Power from a config entry."""
    coordinator = GaPowerCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: GaPowerConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
