"""Constants for the Georgia Power integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "gapower"

# Data lags ~24-48h at the portal, so polling hard buys nothing. Twice a day keeps
# request volume far below whatever triggers Southern Company's bot detection.
UPDATE_INTERVAL = timedelta(hours=12)

# How far back to re-import on every run. async_add_external_statistics is keyed on
# `start`, so re-pushing overwrites in place rather than double-counting.
#
# This is 45, not 3, because Georgia Power REVISES cost retroactively. Verified
# 2026-08-09 against the live API: unbilled hours carry a provisional energy-only
# charge (~$0.056/kWh) that is trued up to the full effective rate (~$0.17/kWh) once
# the billing cycle closes - a 3x difference. A 3-day window would import the
# provisional numbers and never see the correction, permanently understating cost in
# the Energy Dashboard. 45 days covers a ~30-day billing cycle plus margin.
#
# Cost of the wider window: ~2,160 statistic rows re-imported per run instead of
# ~100. Still far below the ~17,500 a full-year re-push would cost, so the HA Pi's
# SD card is not meaningfully affected.
REFETCH_DAYS = 45

# How much history to pull on the very first run.
BACKFILL_DAYS = 365

# MPUData tolerated a 30-day window (744 points) cleanly in testing.
CHUNK_DAYS = 30

# Southern Company uses -1 as a placeholder for "no reading yet", not a value.
SENTINEL = -1

CONF_ACCOUNT_NUMBER = "account_number"
