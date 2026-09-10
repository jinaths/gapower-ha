"""Fetch Georgia Power hourly usage and import it as HA external statistics.

Pattern follows homeassistant/components/opower/coordinator.py - the in-core
reference for importing a utility's hourly energy data as external statistics.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    GaPowerApi,
    GaPowerAuthError,
    GaPowerBotDetected,
    GaPowerError,
    GaPowerMaintenance,
    GaPowerSessionExpired,
    GaPowerTransient,
)
from .const import BACKFILL_DAYS, DOMAIN, REFETCH_DAYS, UPDATE_INTERVAL

_LOGGER = logging.getLogger(__name__)

# `mean_type` replaced `has_mean` in newer HA cores. Support both so the integration
# works across versions (southern-company-hacs PR #123 hit the same compat problem).
try:
    from homeassistant.components.recorder.models import StatisticMeanType

    _MEAN_NONE: Any = StatisticMeanType.NONE
    _USE_MEAN_TYPE = True
except ImportError:  # pragma: no cover - older cores
    _MEAN_NONE = None
    _USE_MEAN_TYPE = False


class GaPowerCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Pull hourly usage and push it into the recorder's statistics tables."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
            config_entry=entry,
        )
        # Dedicated session, for two reasons: this talks to an Imperva-fronted host
        # with its own browser-ish headers, and the login relay DEPENDS on cookie
        # persistence between hops. aiohttp enables a cookie jar by default and HA's
        # helper doesn't override it, but pass one explicitly so a future change to
        # either default can't silently break authentication.
        self.api = GaPowerApi(
            async_create_clientsession(hass, cookie_jar=aiohttp.CookieJar()),
            entry.data[CONF_USERNAME],
            entry.data[CONF_PASSWORD],
        )
        # Why the last poll failed, kept here rather than in the coordinator's data.
        # Every entity fed by `data` goes unavailable the instant a poll fails, taking
        # its attributes with it - which is precisely the moment something needs to be
        # able to say what broke. The status sensor reads these instead.
        self.last_error: str | None = None
        self.last_error_kind: str | None = None
        self.last_success: dt.datetime | None = None

    @property
    def _usage_id(self) -> str:
        return f"{DOMAIN}:energy_consumption"

    @property
    def _cost_id(self) -> str:
        return f"{DOMAIN}:energy_cost"

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            try:
                data = await self._async_poll()
            except GaPowerSessionExpired as err:
                # The session died between polls or part-way through this one.
                # Deferring to the next scheduled run would cost 12 hours of data for
                # something a single login fixes, so re-authenticate and go again -
                # once. A second failure falls through to the handlers below, so a
                # genuinely rejected account can never become a retry loop against
                # the utility.
                self.logger.debug("Session rejected (%s); re-authenticating once", err)
                await self.api.force_relogin()
                data = await self._async_poll()
        except GaPowerAuthError as err:
            # Triggers HA's reauth flow rather than retrying - repeated bad logins
            # risk locking the utility account.
            raise ConfigEntryAuthFailed(self._record("invalid_auth", err)) from err
        except GaPowerMaintenance as err:
            raise UpdateFailed(self._record("utility_outage", err)) from err
        except GaPowerBotDetected as err:
            raise UpdateFailed(
                self._record(
                    "blocked",
                    f"{err}. Southern Company's WAF is challenging us; this usually "
                    "clears within ~30 minutes.",
                )
            ) from err
        except (GaPowerTransient, GaPowerError) as err:
            raise UpdateFailed(self._record("error", err)) from err

        self.last_error = None
        self.last_error_kind = None
        self.last_success = dt.datetime.now(dt.timezone.utc)
        return data

    async def _async_poll(self) -> dict[str, Any]:
        """One full pass: authenticate, resolve the account, import the statistics."""
        await self.api.login()
        await self.api.resolve_account()
        return await self._async_import_statistics()

    def _record(self, kind: str, err: object) -> str:
        """Remember why a poll failed, and hand back the message to raise with."""
        self.last_error_kind = kind
        self.last_error = str(err)
        return self.last_error

    async def _async_get_last(self, statistic_id: str) -> dict | None:
        last = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, statistic_id, True, {"sum"}
        )
        rows = last.get(statistic_id) if last else None
        return rows[0] if rows else None

    async def _async_baseline(
        self, statistic_id: str, before: dt.datetime
    ) -> float:
        """Cumulative sum for the last hour strictly before `before`."""
        stats = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            before - dt.timedelta(days=REFETCH_DAYS + 7),
            before,
            {statistic_id},
            "hour",
            None,
            {"sum"},
        )
        rows = (stats or {}).get(statistic_id) or []
        prior = [r for r in rows if r.get("sum") is not None]
        return float(prior[-1]["sum"]) if prior else 0.0

    async def _async_import_statistics(self) -> dict[str, Any]:
        tz = ZoneInfo(self.hass.config.time_zone)
        today = dt.datetime.now(tz).date()

        last = await self._async_get_last(self._usage_id)
        if last is None:
            start_date = today - dt.timedelta(days=BACKFILL_DAYS)
            since: dt.datetime | None = None
            base_kwh = base_cost = 0.0
            _LOGGER.info("No existing statistics; backfilling %s days", BACKFILL_DAYS)
        else:
            last_start = dt.datetime.fromtimestamp(last["start"], tz)
            since = (last_start - dt.timedelta(days=REFETCH_DAYS)).replace(
                minute=0, second=0, microsecond=0
            )
            start_date = since.date()
            base_kwh = await self._async_baseline(self._usage_id, since)
            base_cost = await self._async_baseline(self._cost_id, since)
            _LOGGER.debug(
                "Resuming from %s (baseline %.1f kWh / $%.2f)", since, base_kwh, base_cost
            )

        hourly = await self.api.async_get_hourly(start_date, today)
        if not hourly:
            _LOGGER.debug("No hourly rows returned for %s .. %s", start_date, today)
            return self.data or {}

        usage_stats: list[StatisticData] = []
        cost_stats: list[StatisticData] = []
        running_kwh, running_cost = base_kwh, base_cost
        latest: dt.datetime | None = None
        latest_kwh = latest_cost = None

        for ts in sorted(hourly):
            aware = ts.replace(minute=0, second=0, microsecond=0, tzinfo=tz)
            if since is not None and aware < since:
                continue
            vals = hourly[ts]
            if "kwh" in vals:
                running_kwh += vals["kwh"]
                usage_stats.append(
                    StatisticData(start=aware, state=vals["kwh"], sum=running_kwh)
                )
                latest, latest_kwh = aware, vals["kwh"]
            if "cost" in vals:
                running_cost += vals["cost"]
                cost_stats.append(
                    StatisticData(start=aware, state=vals["cost"], sum=running_cost)
                )
                latest_cost = vals["cost"]

        if usage_stats:
            async_add_external_statistics(
                self.hass,
                self._metadata(
                    self._usage_id,
                    "Georgia Power Energy Consumption",
                    UnitOfEnergy.KILO_WATT_HOUR,
                    # EnergyConverter.UNIT_CLASS in homeassistant/util/unit_conversion.py
                    unit_class="energy",
                ),
                usage_stats,
            )
        if cost_stats:
            async_add_external_statistics(
                self.hass,
                self._metadata(
                    self._cost_id,
                    "Georgia Power Energy Cost",
                    self.hass.config.currency,
                    # Money has no unit converter, so no unit class - same as core's
                    # opower integration, which passes unit_class=None for cost.
                    unit_class=None,
                ),
                cost_stats,
            )
        _LOGGER.debug("Imported %s usage / %s cost rows", len(usage_stats), len(cost_stats))

        return {
            "last_reading": latest,
            "latest_kwh": latest_kwh,
            "latest_cost": latest_cost,
            "total_kwh": round(running_kwh, 3),
            "imported": len(usage_stats),
        }

    def _metadata(
        self, statistic_id: str, name: str, unit: str | None, unit_class: str | None
    ) -> StatisticMetaData:
        # `unit_class` must always be present as a key - omitting it is deprecated and
        # stops working in HA 2026.11. None is a valid value for unconvertible units.
        meta: dict[str, Any] = {
            "has_sum": True,
            "name": name,
            "source": DOMAIN,
            "statistic_id": statistic_id,
            "unit_of_measurement": unit,
            "unit_class": unit_class,
        }
        if _USE_MEAN_TYPE:
            meta["mean_type"] = _MEAN_NONE
        else:
            meta["has_mean"] = False
        return StatisticMetaData(**meta)  # type: ignore[typeddict-item]
