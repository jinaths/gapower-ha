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

# --- Smart Usage tariff -------------------------------------------------------
# Recorded 2026-09-07 from the user's own rate card. Update both numbers together
# if Georgia Power reprices; nothing else in the code encodes the tariff.

# Dollars per billed kW, charged on the single highest-usage hour of the whole bill
# cycle - whenever it falls. It is NOT limited to the on-peak window, which is why a
# 9pm Sunday spike costs exactly as much here as a 3pm Tuesday one.
DEMAND_RATE = 12.44

# Georgia Power rounds the demand kW HALF-UP: 4.49 kWh bills as 4 kW, 4.50 as 5 kW
# (user-confirmed against a real bill, 2026-09-09). The charge is therefore a STEP
# function, not a slope - the only thing that matters is which side of the next X.50
# the cycle's worst hour lands on. Shaving 0.4 off a 4.9 kWh hour saves nothing;
# shaving 0.13 off a 4.62 kWh hour saves the entire $12.44.
DEMAND_ROUNDING = 0.5

# How far back to ask for bill periods. Mirrors the two-year span the portal's own
# SPA requests - the one window shape known to return the current cycle.
BILL_PERIOD_LOOKBACK_DAYS = 730
