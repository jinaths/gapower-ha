"""Southern Company / Georgia Power web API client.

Async (aiohttp) port of the auth chain from apearson/southern-company-api
(src/main.ts:50-196) - the 2024 "SPA endpoints" flow. Deliberately NOT built on the
`southern-company-api` PyPI package, whose first hop still uses the older
GET-and-scrape-`data-aft` pattern that upstream JS replaced.

Endpoint quirks that are load-bearing - do not "clean these up":

* `EndDate` on MPUData is EXCLUSIVE. StartDate == EndDate returns an empty range and
  reports HasData=false. Always pass (last wanted day + 1).
* Dates must be plain MM/DD/YYYY. Appending a time component ("11:59:59 PM") makes
  the Hourly route return null even with the correct +1 day.
* `intervalBehavior` must be "Automatic" (or "Interval"). Counterintuitively "Hourly"
  returns null on the /Hourly route.

All three were verified live 2026-08-06/07.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import random
import re
from typing import Any

import aiohttp

from .const import CHUNK_DAYS, SENTINEL

_LOGGER = logging.getLogger(__name__)

API = "https://customerservice2api.southerncompany.com/api"
WEBAUTH = "https://webauth.southernco.com"
CS2 = "https://customerservice2.southerncompany.com"

COMPANY_MAP = {0: "SCS", 1: "APC", 2: "GPC", 4: "MPC", 7: "NICOR_GAS"}

MAX_RETRIES = 3
BACKOFF_BASE = 2.0
BACKOFF_CAP = 30.0
REQUEST_DELAY = 1.0

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
BROWSER_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Ch-Ua": '"Chromium";v="126", "Not)A;Brand";v="99", "Google Chrome";v="126"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Upgrade-Insecure-Requests": "1",
}

# Keep this list NARROW. Verified live 2026-08-07: a *successful* /SPA/Navigation
# response contains "_Incapsula_Resource" and "/_Incapsula_" - those are the Imperva
# client SDK <script> tags, present on every page whether blocked or not. Same for
# "reese84". Matching on them blocks perfectly good logins. Only the real block page
# carries "Request unsuccessful. Incapsula incident ID: ...".
IMPERVA_MARKERS_STRONG = ("incapsula incident", "request unsuccessful")
IMPERVA_MARKERS_WEAK = ("incident id", "robot")

# Imperva's JS challenge renders a bare "Loading" shell - but so does Southern
# Company's own /SPA/Navigation step, which legitimately carries the ScWebToken.
# Only meaningful when the expected field is ALSO missing; checked at the call site.
LOADING_SHELL = "<title>loading</title>"


class GaPowerError(Exception):
    """Base error."""


class GaPowerAuthError(GaPowerError):
    """Credentials rejected. Must NOT be retried - repeats risk account lockout."""


class GaPowerBotDetected(GaPowerError):
    """Imperva challenge / IP ban. Retrying now deepens it."""


class GaPowerTransient(GaPowerError):
    """Network blip, 5xx, or 429. Safe to retry later."""


def _looks_like_bot_challenge(status: int, body: str, content_type: str) -> bool:
    """True if this is a WAF challenge rather than a real response."""
    if "json" in (content_type or "").lower():
        return False  # a real API response containing these words is not a challenge
    low = (body or "")[:4000].lower()
    if any(m in low for m in IMPERVA_MARKERS_STRONG):
        return True
    if status in (401, 403, 405, 406, 503):
        if any(m in low for m in IMPERVA_MARKERS_WEAK):
            return True
        if status == 403 and len(body or "") < 256:
            return True
    return False


class GaPowerApi:
    """Talks to the Southern Company customer portal."""

    def __init__(self, session: aiohttp.ClientSession, username: str, password: str) -> None:
        self._session = session
        self._username = username
        self._password = password
        self.jwt: str | None = None
        self.account: str | None = None
        self.company: str | None = None
        self.service_point: str | None = None

    async def _request(self, method: str, url: str, **kw: Any) -> tuple[int, str, dict]:
        """One HTTP call with bounded retry. Returns (status, body_text, headers)."""
        kw.setdefault("allow_redirects", False)
        headers = {**BROWSER_HEADERS, **(kw.pop("headers", None) or {})}
        timeout = aiohttp.ClientTimeout(total=45)
        last: Exception | str | None = None

        for attempt in range(MAX_RETRIES + 1):
            try:
                async with self._session.request(
                    method, url, headers=headers, timeout=timeout, **kw
                ) as resp:
                    body = await resp.text()
                    status = resp.status
                    ctype = resp.headers.get("Content-Type", "")
                    raw_headers = {
                        "Set-Cookie": resp.headers.getall("Set-Cookie", []),
                    }
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                last = err
                if attempt == MAX_RETRIES:
                    raise GaPowerTransient(f"{type(err).__name__}: {err}") from err
            else:
                if _looks_like_bot_challenge(status, body, ctype):
                    raise GaPowerBotDetected(
                        f"WAF challenge from {url.split('?')[0]} (HTTP {status})"
                    )
                if status == 429 or 500 <= status < 600:
                    last = f"HTTP {status}"
                    if attempt == MAX_RETRIES:
                        raise GaPowerTransient(f"{last} from {url.split('?')[0]}")
                else:
                    return status, body, raw_headers

            delay = min(BACKOFF_BASE * (2**attempt), BACKOFF_CAP)
            delay += random.uniform(0, delay * 0.25)
            _LOGGER.debug("Attempt %s failed (%s); retrying in %.1fs", attempt + 1, last, delay)
            await asyncio.sleep(delay)

        raise GaPowerTransient(str(last))

    @staticmethod
    def _cookie(headers: dict, name: str) -> str | None:
        for raw in headers.get("Set-Cookie", []):
            m = re.search(rf"{name}=(\S*?);", raw, re.I)
            if m:
                return m.group(1)
        return None

    async def login(self) -> None:
        """Four-hop token relay. Raises GaPowerAuthError on bad credentials."""
        # 1. verification token - real JSON
        status, body, _ = await self._request(
            "POST",
            f"{WEBAUTH}/webservices/api/WebUser/Login",
            json={"username": self._username, "password": self._password},
            headers={
                "Referer": f"{WEBAUTH}/SPA/OCC/login",
                "Accept": "application/json, text/plain, */*",
            },
        )
        if status != 200:
            raise GaPowerTransient(f"login step 1: HTTP {status}")
        try:
            payload = json.loads(body)
        except ValueError as err:
            # a JSON endpoint answering non-JSON is the classic Imperva tell
            raise GaPowerBotDetected("login endpoint returned non-JSON") from err
        token = (payload.get("data") or {}).get("token")
        if not token:
            raise GaPowerAuthError("Login rejected - check username and password")

        # 2. ScWebToken - scraped from an HTML hidden input
        status, body, _ = await self._request(
            "POST", f"{WEBAUTH}/SPA/Navigation", data={"Token": token, "ReturnUrl": "none"}
        )
        if status != 200:
            raise GaPowerTransient(f"login step 2: HTTP {status}")
        m = re.search(r'name="ScWebToken"\s+value="(\S+\.\S+\.\S+)"', body, re.I)
        if not m:
            # loading shell WITH the token is normal; WITHOUT it means JS never ran
            # for us, i.e. a challenge rather than a changed login flow
            if LOADING_SHELL in body[:4000].lower():
                raise GaPowerBotDetected("SPA/Navigation returned a JS challenge shell")
            raise GaPowerTransient("ScWebToken not found - login flow may have changed")

        # 3. SouthernJwtCookie - 302 + Set-Cookie
        status, _, headers = await self._request(
            "POST",
            f"{CS2}/Account/LoginComplete",
            params={"ReturnUrl": "/Billing/Home"},
            data={"ScWebToken": m.group(1)},
        )
        if status != 302:
            raise GaPowerTransient(f"login step 3: expected 302, got {status}")
        sjc = self._cookie(headers, "SouthernJwtCookie")
        if not sjc:
            raise GaPowerTransient("login step 3: SouthernJwtCookie missing")

        # 4. final bearer JWT
        status, _, headers = await self._request(
            "GET",
            f"{CS2}/Account/LoginValidated/JwtToken",
            headers={"Cookie": f"SouthernJwtCookie={sjc}"},
        )
        self.jwt = self._cookie(headers, "ScJwtToken")
        if not self.jwt:
            raise GaPowerTransient(f"login step 4: ScJwtToken missing (HTTP {status})")

    @property
    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"bearer {self.jwt}", "Accept": "application/json"}

    async def resolve_account(self) -> None:
        """Find the account number, company code, and service point."""
        status, body, _ = await self._request(
            "GET", f"{API}/account/getAllAccounts", headers=self._auth
        )
        if status != 200:
            raise GaPowerTransient(f"getAllAccounts: HTTP {status}")
        accounts = (json.loads(body).get("Data")) or []
        if not accounts:
            raise GaPowerError("No accounts returned")
        acct = accounts[0]

        raw = acct.get("Company")
        self.company = COMPANY_MAP.get(raw, str(raw)) if isinstance(raw, int) else raw

        for key in ("Number", "number", "AccountNumber", "accountNumber", "Account", "account"):
            if acct.get(key):
                self.account = str(acct[key])
                break
        if not self.account:
            raise GaPowerError(f"Account number not found; keys: {sorted(acct)}")

        status, body, _ = await self._request(
            "GET",
            f"{API}/MyPowerUsage/getMPUBasicAccountInformation/{self.account}/{self.company}",
            headers=self._auth,
        )
        if status != 200:
            raise GaPowerTransient(f"getMPUBasicAccountInformation: HTTP {status}")
        points = (json.loads(body).get("Data") or {}).get("meterAndServicePoints") or []
        if not points:
            raise GaPowerError(f"No service points for company={self.company}")
        self.service_point = points[0]["servicePointNumber"]

    async def async_get_hourly(
        self, start: dt.date, last_day: dt.date
    ) -> dict[dt.datetime, dict[str, float]]:
        """Hourly usage/cost/temp between two dates, inclusive of `last_day`.

        Returns {naive local datetime: {"kwh":, "cost":, "temp":}}.
        """
        out: dict[dt.datetime, dict[str, float]] = {}
        cursor = start
        while cursor <= last_day:
            chunk_last = min(cursor + dt.timedelta(days=CHUNK_DAYS - 1), last_day)
            parsed = await self._fetch_chunk(cursor, chunk_last)
            if parsed:
                _merge(parsed, out)
            cursor = chunk_last + dt.timedelta(days=1)
            if cursor <= last_day:
                await asyncio.sleep(REQUEST_DELAY)
        return out

    async def _fetch_chunk(self, start: dt.date, last_day: dt.date) -> dict | None:
        params = {
            "OPCO": self.company,
            "ServicePointNumber": self.service_point,
            "intervalBehavior": "Automatic",
            "StartDate": start.strftime("%m/%d/%Y"),
            # EndDate is EXCLUSIVE -> last wanted day + 1
            "EndDate": (last_day + dt.timedelta(days=1)).strftime("%m/%d/%Y"),
        }
        status, body, _ = await self._request(
            "GET",
            f"{API}/MyPowerUsage/MPUData/{self.account}/Hourly",
            headers=self._auth,
            params=params,
        )
        if status != 200:
            raise GaPowerTransient(f"MPUData/Hourly: HTTP {status}")
        data = (json.loads(body).get("Data")) or {}
        raw = data.get("Data")
        if not data.get("HasData") or raw is None:
            _LOGGER.debug("No hourly data for %s .. %s", start, last_day)
            return None
        return json.loads(raw)


def _parse_ts(pt: dict[str, Any]) -> dt.datetime | None:
    """Hourly points carry a naive local ISO timestamp in `name`; `x` is always 0."""
    name = pt.get("name")
    if isinstance(name, str):
        s = name.strip()
        m = re.search(r"/Date\((\d+)", s)
        if m:
            return dt.datetime.fromtimestamp(int(m.group(1)) / 1000)
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y %I:%M:%S %p"):
            try:
                return dt.datetime.strptime(s, fmt)
            except ValueError:
                continue
    return None


def _merge(parsed: dict[str, Any], acc: dict[dt.datetime, dict[str, float]]) -> None:
    series = parsed.get("series") or {}
    for name, field in (("usage", "kwh"), ("cost", "cost"), ("temp", "temp")):
        for pt in (series.get(name) or {}).get("data") or []:
            y = pt.get("y")
            if y is None or y == SENTINEL:
                continue
            ts = _parse_ts(pt)
            if ts is not None:
                acc.setdefault(ts, {})[field] = float(y)
