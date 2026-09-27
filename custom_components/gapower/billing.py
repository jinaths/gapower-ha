"""The TOU-RD-12 bill, computed from hourly usage. Pure functions, no Home Assistant.

Kept apart from the coordinator so the arithmetic can be tested on its own and so
the one place that encodes the bill is easy to find when Georgia Power reprices.
"""

from __future__ import annotations

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal

from .const import (
    ALL_IN_BASE,
    BASIC_SERVICE_PER_DAY,
    DEMAND_RATE,
    DSM_R,
    ECCR,
    FRANCHISE_FEE,
    FUEL_JUN_SEP,
    FUEL_OCT_MAY,
    OFF_PEAK_RATE,
    ON_PEAK_RATE,
    SALES_TAX,
)

_CENT = Decimal("0.01")


def _observed(day: dt.date) -> dt.date:
    """A fixed-date holiday that lands on a weekend is observed on the nearest weekday."""
    if day.weekday() == 5:
        return day - dt.timedelta(days=1)
    if day.weekday() == 6:
        return day + dt.timedelta(days=1)
    return day


def peak_holidays(year: int) -> set[dt.date]:
    """The two days the tariff moves out of on-peak: Independence Day and Labor Day."""
    sept1 = dt.date(year, 9, 1)
    labor = sept1 + dt.timedelta(days=(7 - sept1.weekday()) % 7)
    return {_observed(dt.date(year, 7, 4)), labor}


def is_on_peak(ts: dt.datetime) -> bool:
    """Hours beginning 2-6 pm, Monday-Friday, calendar June-September.

    `ts` is naive LOCAL time, the hour's start - the same shape the portal returns.
    """
    return (
        6 <= ts.month <= 9
        and ts.weekday() < 5
        and 14 <= ts.hour < 19
        and ts.date() not in peak_holidays(ts.year)
    )


def fuel_rate(cycle_end: dt.date) -> float:
    """FCR-27 by billing month. The bill is rendered as the cycle closes, so the
    month of the cycle's last day decides the season (inferred, not yet seen on an
    October bill)."""
    last = cycle_end - dt.timedelta(days=1)
    return FUEL_JUN_SEP if 6 <= last.month <= 9 else FUEL_OCT_MAY


def demand_all_in(kw: float) -> float:
    """What a demand of `kw` actually costs once riders, fee and tax are added."""
    return round(kw * DEMAND_RATE * ALL_IN_BASE, 2)


def estimate_bill(
    days: int, kwh: float, on_peak_kwh: float, peak_kw: float, fuel: float
) -> float:
    """Total bill in dollars, line by line as it is printed.

    Every line is rounded half-up to the cent and each percentage is taken of the
    already-rounded lines, in Decimal - float rounding lands a cent off on real bills.
    Reproduces the Jun-Jul and Jul-Aug 2026 bills exactly ($221.45, $217.33).
    """
    def c(x: Decimal) -> Decimal:
        return x.quantize(_CENT, rounding=ROUND_HALF_UP)

    def d(x: float) -> Decimal:
        return Decimal(str(x))

    kwh_d, on_d = d(round(kwh, 3)), d(round(on_peak_kwh, 3))
    base = (
        c(d(BASIC_SERVICE_PER_DAY) * days)
        + c(d(ON_PEAK_RATE) * on_d)
        + c(d(OFF_PEAK_RATE) * (kwh_d - on_d))
        + c(d(DEMAND_RATE) * d(round(peak_kw, 3)))
    )
    current = base + c(base * d(DSM_R)) + c(d(fuel) * kwh_d)
    subtotal = current + c(base * d(ECCR))
    fee = c(subtotal * d(FRANCHISE_FEE))
    # Printed as five lines - state 4 %, three local 1 % levies, 0.75 % - each rounded
    # on its own. SALES_TAX is their sum; change both together.
    taxable = subtotal + fee
    tax = sum(c(taxable * Decimal(r)) for r in ("0.04", "0.01", "0.01", "0.01", "0.0075"))
    return float(subtotal + fee + tax)
