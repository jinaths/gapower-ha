"""Offline tests for the demand-charge work. No network, no Home Assistant.

Run:  python tests/test_demand.py      (exits non-zero on any failure)

Covers the parts where being wrong is expensive and silent: the half-up rounding
that decides a $12.44 step, the cycle boundary that decides which bill an hour is
charged to, the response shapes this API has actually been seen to return, and
that the failure diagnostic cannot print an account number.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys
from zoneinfo import ZoneInfo

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import hastub  # noqa: E402

# Stand in for Home Assistant BEFORE importing the integration, so this runs on a
# bare Python with nothing but aiohttp installed - no HA, no recorder, no network.
hastub.install()
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "custom_components"))

from gapower import api as gapi  # noqa: E402
from gapower import coordinator as gcoord  # noqa: E402
from gapower import sensor as gsensor  # noqa: E402

GaPowerCoordinator = gcoord.GaPowerCoordinator
TZ = ZoneInfo("America/New_York")
D = dt.date
OK = FAIL = 0


def check(label, got, want):
    global OK, FAIL
    if got == want:
        OK += 1
    else:
        FAIL += 1
        print("  FAIL " + label)
        print("        got  " + repr(got))
        print("        want " + repr(want))


def section(t):
    print("\n--- " + t)


# =========================================================== _as_date
section("_as_date: every shape this portal has been seen to use")
check("US slash", gapi._as_date("09/29/2026"), D(2026, 9, 29))
check("ISO", gapi._as_date("2026-09-29"), D(2026, 9, 29))
check("ISO + time", gapi._as_date("2026-09-29T00:00:00"), D(2026, 9, 29))
check("US + time", gapi._as_date("09/29/2026 12:00:00 AM"), D(2026, 9, 29))
check("ISO slash", gapi._as_date("2026/09/29"), D(2026, 9, 29))
check("US dash", gapi._as_date("09-29-2026"), D(2026, 9, 29))
check("ASP.NET epoch", gapi._as_date("/Date(1790654400000)/") is not None, True)
check("padded", gapi._as_date("  2026-09-29  "), D(2026, 9, 29))
# Month and day must never silently transpose. 29 cannot be a month, and the format
# ordering guarantees each candidate rejects the other's input rather than accepting
# it wrongly.
check("no transpose", gapi._as_date("09/29/2026").month, 9)
check("junk", gapi._as_date("not a date"), None)
check("None", gapi._as_date(None), None)
check("number", gapi._as_date(1790654400), None)
check("empty", gapi._as_date(""), None)

# =========================================================== _normalise_ends
section("_normalise_ends: read the end-date convention off the data")
day = dt.timedelta(days=1)
tiling = [(D(2026, 7, 28), D(2026, 8, 28)), (D(2026, 8, 28), D(2026, 9, 29))]
check("already exclusive is left alone", gapi._normalise_ends(list(tiling)), tiling)

inclusive = [(D(2026, 7, 28), D(2026, 8, 27)), (D(2026, 8, 28), D(2026, 9, 28))]
check(
    "inclusive gets a day added",
    gapi._normalise_ends(list(inclusive)),
    [(D(2026, 7, 28), D(2026, 8, 28)), (D(2026, 8, 28), D(2026, 9, 29))],
)
check("a single period is untouched", gapi._normalise_ends([tiling[0]]), [tiling[0]])
check("empty", gapi._normalise_ends([]), [])

# =========================================================== _parse_bill_periods
section("_parse_bill_periods: shape-tolerant, the body was never captured")
camel = {"data": [
    {"startDate": "07/28/2026", "endDate": "08/28/2026"},
    {"startDate": "08/28/2026", "endDate": "09/29/2026"},
]}
check("camelCase", gapi._parse_bill_periods(camel), tiling)

pascal = {"data": [
    {"StartDate": "07/28/2026", "EndDate": "08/28/2026"},
    {"StartDate": "08/28/2026", "EndDate": "09/29/2026"},
]}
check("PascalCase", gapi._parse_bill_periods(pascal), tiling)

alt = {"data": [
    {"billStartDate": "07/28/2026", "billEndDate": "08/28/2026"},
    {"billStartDate": "08/28/2026", "billEndDate": "09/29/2026"},
]}
check("billStart/billEnd naming", gapi._parse_bill_periods(alt), tiling)

nested = {"data": {"billPeriods": [
    {"startDate": "07/28/2026", "endDate": "08/28/2026"},
    {"startDate": "08/28/2026", "endDate": "09/29/2026"},
]}}
check("one extra level of wrapping", gapi._parse_bill_periods(nested), tiling)

check("unsorted input comes back oldest first",
      gapi._parse_bill_periods({"data": list(reversed(camel["data"]))}), tiling)
check("extra fields alongside the dates are harmless",
      gapi._parse_bill_periods({"data": [
          {"startDate": "08/28/2026", "endDate": "09/29/2026",
           "billAmount": 210.4, "isEstimated": False}]}),
      [(D(2026, 8, 28), D(2026, 9, 29))])
check("empty data", gapi._parse_bill_periods({"data": []}), [])
check("no data key", gapi._parse_bill_periods({"status": True}), [])
check("data is a scalar", gapi._parse_bill_periods({"data": 7}), [])
check("not a dict at all", gapi._parse_bill_periods([]), [])
check("records that are not dicts are skipped",
      gapi._parse_bill_periods({"data": ["x", 3, None]}), [])
check("a record missing one end is dropped",
      gapi._parse_bill_periods({"data": [{"startDate": "08/28/2026"}]}), [])
check("end <= start is a parse failure, not a short cycle",
      gapi._parse_bill_periods(
          {"data": [{"startDate": "09/29/2026", "endDate": "08/28/2026"}]}), [])

# =========================================================== async_get_bill_periods
section("the shape the portal actually returns: a period dropdown")

# Captured live 2026-09-13. BillPeriods answers with the SPA's period <select>:
# 14 entries, newest first, both ends of the cycle packed into ONE semicolon-
# joined string. There is no startDate/endDate pair to find, which is exactly why
# the first version parsed nothing.
LIVE = {"status": True, "statusCode": 200, "message": None, "modelErrors": None,
        "data": [
            {"key": "a" * 21, "value": "08/28/2026 00:00:00;09/29/2026 00:00:00"},
            {"key": "b" * 21, "value": "07/28/2026 00:00:00;08/28/2026 00:00:00"},
            {"key": "c" * 21, "value": "06/26/2026 00:00:00;07/28/2026 00:00:00"},
        ]}
check("the live dropdown shape parses",
      gapi._parse_bill_periods(LIVE),
      [(D(2026, 6, 26), D(2026, 7, 28)), (D(2026, 7, 28), D(2026, 8, 28)),
       (D(2026, 8, 28), D(2026, 9, 29))])
check("newest-first input comes back oldest-last",
      gapi._parse_bill_periods(LIVE)[-1], (D(2026, 8, 28), D(2026, 9, 29)))
check("the 21-char key is not mistaken for a date range",
      gapi._combined_dates({"key": "a" * 21}), None)

section("_combined_dates: separators, and the one that must NOT split")
check("semicolon", gapi._combined_dates({"v": "08/28/2026;09/29/2026"}),
      (D(2026, 8, 28), D(2026, 9, 29)))
check("semicolon with times",
      gapi._combined_dates({"v": "08/28/2026 00:00:00;09/29/2026 00:00:00"}),
      (D(2026, 8, 28), D(2026, 9, 29)))
check("spaced dash", gapi._combined_dates({"v": "08/28/2026 - 09/29/2026"}),
      (D(2026, 8, 28), D(2026, 9, 29)))
check("pipe", gapi._combined_dates({"v": "08/28/2026|09/29/2026"}),
      (D(2026, 8, 28), D(2026, 9, 29)))
check("the word to", gapi._combined_dates({"v": "08/28/2026 to 09/29/2026"}),
      (D(2026, 8, 28), D(2026, 9, 29)))
# A bare dash must never be a separator: it would cut 09-29-2026 in half and
# produce a nonsense range out of a single valid date.
check("a bare dash does NOT split a single US-dashed date",
      gapi._combined_dates({"v": "09-29-2026"}), None)
check("one date alone is not a range",
      gapi._combined_dates({"v": "08/28/2026"}), None)
check("three dates are not a range",
      gapi._combined_dates({"v": "08/28/2026;09/29/2026;10/28/2026"}), None)
check("reversed order is rejected",
      gapi._combined_dates({"v": "09/29/2026;08/28/2026"}), None)
check("junk", gapi._combined_dates({"v": "hello;world"}), None)
check("a long string is skipped before splitting",
      gapi._combined_dates({"v": "08/28/2026;09/29/2026" + "x" * 120}), None)
check("non-string values are skipped", gapi._combined_dates({"v": 12345}), None)
check("explicit start/end still wins over a combined field",
      gapi._parse_bill_periods({"data": [
          {"startDate": "08/28/2026", "endDate": "09/29/2026",
           "label": "01/01/2020;02/02/2020"}]}),
      [(D(2026, 8, 28), D(2026, 9, 29))])

section("async_get_bill_periods: one request, PascalCase, no leaked values")


def _api(responder):
    """A GaPowerApi whose only live part is _get_json, which `responder` fakes."""
    api = object.__new__(gapi.GaPowerApi)
    api.service_agreement = "SA-134"
    api.service_point = "SP-134"
    api.premise_id = "PR-134"
    api.company = "GPC"
    calls = []

    async def fake_get_json(url, label, params=None, **kw):
        calls.append({"url": url, "label": label, "params": params})
        return responder(params)

    api._get_json = fake_get_json
    return api, calls


EMPTY = {"status": True, "statusCode": 200, "message": None, "modelErrors": None,
         "data": None}

api, calls = _api(lambda p: LIVE)
check("the live response yields the current cycle last",
      asyncio.run(api.async_get_bill_periods())[-1], (D(2026, 8, 28), D(2026, 9, 29)))
check("exactly one request - the casing fallback is gone", len(calls), 1)
check("hits the usage host",
      calls[0]["url"].endswith("/MyPowerUsage/BillPeriods"), True)
# Casing was probed live 2026-09-13 and does NOT matter on this route; PascalCase
# is kept only because it is what the portal itself sends.
check("params are PascalCase", sorted(calls[0]["params"]),
      ["EndDate", "OperatingCompany", "PremiseId", "ServiceAgreementId",
       "ServicePointId", "StartDate"])
check("dates are MM/DD/YYYY with no time component",
      len(calls[0]["params"]["StartDate"]) == 10
      and "/" in calls[0]["params"]["StartDate"], True)
check("lookback is two years",
      (dt.datetime.strptime(calls[0]["params"]["EndDate"], "%m/%d/%Y").date()
       - dt.datetime.strptime(calls[0]["params"]["StartDate"], "%m/%d/%Y").date()).days,
      730)
check("ids are passed through", calls[0]["params"]["ServiceAgreementId"], "SA-134")

api, calls = _api(lambda p: EMPTY)
check("an unusable response yields [] rather than raising",
      asyncio.run(api.async_get_bill_periods()), [])
check("...and never retries", len(calls), 1)

section("_describe: structure only, never values")
check("a date shows through - it is what we are hunting",
      gapi._describe("08/28/2026"), "'08/28/2026'")
blob = "x" * 134
check("a 134-char id shows its length only", gapi._describe(blob), "<str 134>")
check("...and never its content", blob in gapi._describe(blob), False)
check("numbers and booleans are safe", gapi._describe(True), "True")
check("None", gapi._describe(None), "None")
check("empty list", gapi._describe([]), "[]")
check("a list reports length and its first record's shape",
      gapi._describe([{"startDate": "08/28/2026"}, {}]),
      "<list 2> first={startDate='08/28/2026'}")
check("nested dict keys are named", gapi._describe({"a": {"b": 1}}), "{a={b=1}}")
# Below the depth limit only key NAMES survive - so the fourth level must be
# absent, not present. An unbounded renderer on a deep payload is how a log line
# ends up carrying the thing it was written to withhold.
deep = gapi._describe({"a": {"b": {"c": {"d": 1}}}})
check("recursion is bounded", deep, "{a={b={c}}}")
check("...so nothing below the limit is rendered", "d" in deep, False)
acct = gapi._describe({"accountNumber": "1234567890", "startDate": "08/28/2026"})
check("a real-looking payload leaks no account number", "1234567890" in acct, False)
check("...while still showing the date field", "'08/28/2026'" in acct, True)
# This is the exact line that solved it, and it must stay safe: the combined value
# is date-shaped only after splitting, so it prints in full - which is fine, it is
# two dates - while the 21-char key beside it is withheld.
live_desc = gapi._describe(LIVE["data"])
check("the live shape renders readably", "09/29/2026" in live_desc, True)
check("...with the opaque key withheld", "a" * 21 in live_desc, False)

# =========================================================== the rounding rule
section("billed demand: half-up rounding is the whole business rule")
CY = (D(2026, 8, 28), D(2026, 9, 29))


def demand(hourly, cycle=CY, today=D(2026, 9, 2)):
    return GaPowerCoordinator._demand(None, hourly, cycle, TZ, today)


def peak_only(kwh, when=dt.datetime(2026, 9, 2, 13)):
    return demand({when: {"kwh": kwh}})


for kwh, want in ((4.49, 4), (4.50, 5), (4.499, 4), (5.50, 6), (5.49, 5),
                  (2.0, 2), (0.2, 0), (0.5, 1), (3.49, 3), (3.5, 4)):
    check(str(kwh) + " kWh bills as " + str(want) + " kW",
          peak_only(kwh)["billed_demand"], want)

# =========================================================== the hand-checked cycle
section("the 08/28-09/29 cycle, hand-worked on 2026-09-09")
hourly = {
    dt.datetime(2026, 9, 2, 13): {"kwh": 4.62, "cost": 0.26},   # the peak
    dt.datetime(2026, 9, 3, 19): {"kwh": 4.39},
    dt.datetime(2026, 9, 4, 19): {"kwh": 4.15},
    dt.datetime(2026, 8, 29, 13): {"kwh": 4.14},
    dt.datetime(2026, 8, 30, 18): {"kwh": 4.12},
    dt.datetime(2026, 8, 28, 0): {"kwh": 1.10},
}
r = demand(hourly)
check("peak kWh", r["cycle_peak"], 4.62)
check("peak hour", r["cycle_peak_time"], dt.datetime(2026, 9, 2, 13, tzinfo=TZ))
check("billed demand", r["billed_demand"], 5)
check("demand charge", r["demand_charge"], 62.20)
check("cycle length", r["cycle_length"], 32)
check("day of cycle", r["cycle_day"], 6)
check("cycle start", r["cycle_start"], dt.datetime(2026, 8, 28, tzinfo=TZ))
check("cycle end (exclusive)", r["cycle_end"], dt.datetime(2026, 9, 29, tzinfo=TZ))
check("target ceiling to drop a bracket", r["target_kwh"], 4.49)
check("kWh to shed", r["shed_kwh"], 0.13)
check("charge one bracket down", r["lower_charge"], 49.76)
check("saving from shedding", round(r["demand_charge"] - r["lower_charge"], 2), 12.44)
check("next step up", r["next_bracket_kwh"], 5.50)

# =========================================================== boundaries
section("cycle boundaries: which bill an hour is charged to")
big = 9.99
check("first day of the cycle counts",
      demand({dt.datetime(2026, 8, 28, 20): {"kwh": big}})["cycle_peak"], big)
check("last day of the cycle counts",
      demand({dt.datetime(2026, 9, 28, 23): {"kwh": big}})["cycle_peak"], big)
check("the exclusive end date does NOT count",
      demand({dt.datetime(2026, 9, 29, 0): {"kwh": big}})["cycle_peak"], None)
check("the day before the cycle does NOT count",
      demand({dt.datetime(2026, 8, 27, 23): {"kwh": big}})["cycle_peak"], None)
check("an out-of-cycle spike cannot beat an in-cycle peak",
      demand({dt.datetime(2026, 9, 29, 18): {"kwh": big},
              dt.datetime(2026, 9, 2, 13): {"kwh": 4.62}})["cycle_peak"], 4.62)
check("hours with cost but no kwh are ignored",
      demand({dt.datetime(2026, 9, 2, 13): {"cost": 1.0}})["cycle_peak"], None)
check("cycle_day is clamped to the cycle, not to today",
      demand(hourly, today=D(2026, 12, 25))["cycle_day"], 32)

section("degenerate inputs blank every field rather than leaving stale ones")
none_cycle = demand({dt.datetime(2026, 9, 2, 13): {"kwh": 4.62}}, cycle=None)
check("no cycle -> every key still present",
      set(none_cycle), set(gcoord._DEMAND_KEYS))
check("no cycle -> every value None", set(none_cycle.values()), {None})
empty = demand({})
check("no data -> cycle window still reported", empty["cycle_length"], 32)
check("no data -> peak is None", empty["cycle_peak"], None)
check("no data -> billed demand is None", empty["billed_demand"], None)
check("no data -> no stale target", empty["target_kwh"], None)
zero = peak_only(0.2)
check("a sub-half-kWh peak bills as 0 kW", zero["billed_demand"], 0)
check("...and offers no negative target", zero["target_kwh"], None)

# =========================================================== cycle caching
section("_async_current_cycle: cache, carry, and do not swallow a dead session")


class FakeApi:
    def __init__(self, result=None, exc=None):
        self.calls = 0
        self.result = result or []
        self.exc = exc

    async def async_get_bill_periods(self):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.result


class FakeCoord:
    def __init__(self, api, periods=(), cycle=None):
        self.api = api
        self._periods = list(periods)
        self._cycle = cycle

    _cycle_for = GaPowerCoordinator._cycle_for
    _async_current_cycle = GaPowerCoordinator._async_current_cycle


a = FakeApi()
c = FakeCoord(a, periods=tiling)
check("a cached cycle covering today is used",
      asyncio.run(c._async_current_cycle(D(2026, 9, 2))), CY)
check("...and costs no request", a.calls, 0)

a = FakeApi(result=tiling)
c = FakeCoord(a, periods=[])
check("a cache miss refetches", asyncio.run(c._async_current_cycle(D(2026, 9, 2))), CY)
check("...exactly once", a.calls, 1)

a = FakeApi(result=tiling)
c = FakeCoord(a, periods=tiling, cycle=tiling[0])
check("rollover picks the new cycle out of the cache",
      asyncio.run(c._async_current_cycle(D(2026, 9, 2))), CY)
check("...still without a request", a.calls, 0)

a = FakeApi(exc=gapi.GaPowerTransient("boom"))
c = FakeCoord(a, periods=[], cycle=CY)
check("a failed refresh carries the last known boundary",
      asyncio.run(c._async_current_cycle(D(2026, 10, 1))), CY)

a = FakeApi(exc=gapi.GaPowerMaintenance("portal down"))
c = FakeCoord(a, periods=[], cycle=None)
check("a failed refresh with nothing cached yields None, not a guessed date",
      asyncio.run(c._async_current_cycle(D(2026, 10, 1))), None)

a = FakeApi(exc=gapi.GaPowerSessionExpired("rejected"))
c = FakeCoord(a, periods=[], cycle=CY)
try:
    asyncio.run(c._async_current_cycle(D(2026, 10, 1)))
    check("GaPowerSessionExpired must propagate to the retry", "swallowed", "raised")
except gapi.GaPowerSessionExpired:
    check("GaPowerSessionExpired must propagate to the retry", "raised", "raised")

a = FakeApi(result=tiling)
c = FakeCoord(a, periods=[], cycle=CY)
check("a refresh that still does not cover today keeps the old cycle",
      asyncio.run(c._async_current_cycle(D(2027, 5, 1))), CY)

# =========================================================== sensor wiring
section("sensor descriptions")
keys = [d.key for d in gsensor.SENSORS]
for k in ("cycle_peak", "billed_demand", "demand_charge", "cycle_start"):
    check(k + " is declared", k in keys, True)
check("no duplicate keys", len(keys), len(set(keys)))
check("every description has a translation",
      all(d.translation_key for d in gsensor.SENSORS), True)

by_key = {d.key: d for d in gsensor.SENSORS}
check("billed_demand reads the right field", by_key["billed_demand"].value_fn(r), 5)
check("cycle_peak reads the right field", by_key["cycle_peak"].value_fn(r), 4.62)
check("demand_charge reads the right field",
      by_key["demand_charge"].value_fn(r), 62.20)
check("cycle_start reads the right field",
      by_key["cycle_start"].value_fn(r), dt.datetime(2026, 8, 28, tzinfo=TZ))
check("billed_demand is in kW",
      by_key["billed_demand"].native_unit_of_measurement, "kW")
check("cycle_peak is in kWh", by_key["cycle_peak"].native_unit_of_measurement, "kWh")

attrs = by_key["billed_demand"].attrs_fn(r)
check("the alert can read the peak off one entity", attrs["peak_kwh"], 4.62)
check("...and the ceiling", attrs["target_kwh"], 4.49)
check("...and what shedding saves",
      round(attrs["charge"] - attrs["lower_charge"], 2), 12.44)
check("...and where it is in the cycle",
      (attrs["cycle_day"], attrs["cycle_length"]), (6, 32))
check("no attribute is missing", [k for k, v in attrs.items() if v is None], [])

print("\n" + "=" * 58)
print(str(OK) + " passed, " + str(FAIL) + " failed")
sys.exit(1 if FAIL else 0)
