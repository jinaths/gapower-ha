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

# --- TOU-RD-12 tariff ---------------------------------------------------------
# Schedule "TOU-RD-12" (Time of Use - Residential Demand), Original revision, sheet
# 2.40, effective with bills rendered for the billing month of June 2026. Confirmed
# 2026-09-24 against the tariff sheet itself (page 1 of 2), superseding the
# 2026-09-07 note that read it off a summary screenshot. Update these numbers
# together if Georgia Power reprices; nothing else in the code encodes the tariff.
#
# The whole bill, as reproduced to the cent on five printed bills (Mar-Aug 2026; see
# georgia-power-bill-calculation.md in the project notes). Used only for the bill
# projection - the Energy Dashboard still imports the portal's own cost figures.
BASIC_SERVICE_PER_DAY = 0.4603
ON_PEAK_RATE = 0.145620  # $/kWh, Jun-Sep Mon-Fri 2-7 pm
OFF_PEAK_RATE = 0.015569  # $/kWh, every other hour
# Dollars per kW, charged on the single highest-usage hour of the whole bill cycle -
# whenever it falls. It is NOT limited to the on-peak window, which is why a 9pm
# Sunday spike costs exactly as much here as a 3pm Tuesday one.
# The sheet carries ONE unqualified "Maximum kW" line with no summer/winter split,
# so this rate is YEAR-ROUND. That matters: this house bills its highest demand in
# January, not in summer (see demand-load-profile.md), and a cheaper winter rate
# would have changed which months are worth working on. It does not exist.
#
# The kW is NOT rounded. The printed bills show "Pk kW 1 Hour" to three decimals
# (3.636, 4.324, 4.538, 4.624, 4.774) and every line reproduces only with that
# figure unrounded. An earlier version of this integration rounded half-up to whole
# kW on a customer's say-so; the bills disproved it (2026-09-26). The charge is a
# slope: every 0.1 kWh off the worst hour saves the same amount.
DEMAND_RATE = 12.44
# Riders on the base bill (ECCR-15, DSM-R-16), fuel (FCR-27, one flat rate for all
# kWh - this account is not on time-of-use fuel), then the Municipal Franchise Fee
# (inside city limits) and sales tax on everything before them.
ECCR = 0.130205
DSM_R = 0.011969
FUEL_JUN_SEP = 0.038069  # $/kWh on June-September bills
FUEL_OCT_MAY = 0.038561  # $/kWh on October-May bills
FRANCHISE_FEE = 0.030843
SALES_TAX = 0.0775
# What a dollar of base charge costs once riders, fee and tax are added (1.2687).
# Demand is all base, so this turns $12.44/kW into the $15.78/kW actually paid.
ALL_IN_BASE = (1 + ECCR + DSM_R) * (1 + FRANCHISE_FEE) * (1 + SALES_TAX)

# How far back to ask for bill periods. Mirrors the two-year span the portal's own
# SPA requests - the one window shape known to return the current cycle.
BILL_PERIOD_LOOKBACK_DAYS = 730
