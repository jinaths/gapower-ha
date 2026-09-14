"""Southern Company / Georgia Power web API client.

Rewritten 2026-09-08 for the Aug 31 - Sep 8 2026 CIS migration, which replaced the
authentication system outright rather than adjusting it. Mapped from a browser HAR of
a real manual login; see README "2026-09 Southern Company migration".

What the migration changed:

* Credentials now go to ForgeRock/PingAM at customerlogin.southernco.com, using its
  `callbacks[]` protocol, not the old flat-JSON `/webservices/api/WebUser/Login`.
* The portal session is then minted through an OIDC authorization-code exchange with
  PKCE, replacing the `ScWebToken` hidden-input scrape.
* The data API moved from customerservice2api `MPUData` to
  occmypowerusageapi `MyPowerUsage/UsageGraphData`, which needs four opaque
  identifiers instead of a bare service-point number.

What did NOT change, and is still load-bearing - do not "clean these up":

* `endDate` is EXCLUSIVE. startDate == endDate returns an empty range and reports
  hasData=false. Always pass (last wanted day + 1).
* Dates must be plain MM/DD/YYYY. Appending a time component ("11:59:59 PM") makes
  the Hourly route return null even with the correct +1 day.
* `intervalBehavior` must be "Automatic" (or "Interval"). Counterintuitively "Hourly"
  returns null on the /Hourly route.

The first three were verified live 2026-08-06/07 and re-confirmed against the new
endpoint in the 2026-09-08 capture.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
import logging
import random
import re
import uuid
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse

import aiohttp
from yarl import URL

from .const import BILL_PERIOD_LOOKBACK_DAYS, CHUNK_DAYS, SENTINEL

_LOGGER = logging.getLogger(__name__)

FORGEROCK = "https://customerlogin.southernco.com"
WEBAUTH = "https://webauth.southernco.com"
CS2 = "https://customerservice2.southerncompany.com"
ACCOUNT_API = "https://occaccountapi.southerncompany.com/api/v1"
USAGE_API = "https://occmypowerusageapi.southerncompany.com/api/v1"

# ForgeRock realm path and the authentication tree the portal uses.
AM_AUTH_PATH = "/am/json/realms/root/realms/alpha/authenticate"
AM_SERVICE = "occSecureLogin"

# Used only if AM's serverinfo lookup fails. Observed 2026-09-08; this tenant does not
# use the stock `iPlanetDirectoryPro`, and the value is specific to their deployment.
AM_COOKIE_FALLBACK = "c4928758b74fd64"

# Where webauth issues the OIDC challenge that starts a login.
WEBAUTH_OIDC_INIT = f"{WEBAUTH}/SPA/ExternalAuthentication/forgerock-oidc-authcode"

# Copied verbatim from a real browser navigation. ALL SEVEN are required: dropping
# WL_CancelUrl or originalPath makes webauth answer 500 instead of redirecting to
# ForgeRock (confirmed by probing both variants, 2026-09-08). WL_ReturnUrl keeps
# %2FBilling%2FHome encoded because it is a query value inside a nested URL.
OIDC_INIT_PARAMS = {
    "WL_ReturnUrl": f"{CS2}/Account/LoginComplete?returnUrl=%2FBilling%2FHome",
    "WL_AppId": "pr-occ-forgerock",
    "WL_Type": "E",
    "BrowserContextTarget": "Top",
    "WL_CancelUrl": f"{CS2}/Account/Login",
    # NOT load-bearing, despite the name. Probed unauthenticated 2026-09-10: GPC, APC,
    # MPC, SCS, a nonsense value and an empty string ALL return the same 302 to AM's
    # authorize endpoint with a fresh state + PKCE challenge. Southern Company runs one
    # ForgeRock realm for every operating company, so the login chain is not
    # Georgia-specific and this value is never validated. Left as "GPC" only because
    # that is what a real browser sends. The operating company that actually matters is
    # read per-account in `resolve_account` and passed to the usage API as
    # `operatingCompany`.
    "Company": "GPC",
    "originalPath": "/OCC/login",
}

# ForgeRock rejects the callback protocol without these two. The SDK marker also keeps
# us on the JSON API rather than AM's own hosted login UI.
FORGEROCK_HEADERS = {
    "Accept-API-Version": "protocol=1.0,resource=2.1",
    "X-Requested-With": "forgerock-sdk",
    "Accept": "application/json, text/plain, */*",
}

# Custom headers the portal's own XHRs carry to the occ* services. `devicetype` and
# `userid` are echoed verbatim from the capture; the services accept the request
# without the analytics-only `trace-data`, so that one is not reproduced.
DEVICE_TYPE = "Desktop"

MAX_RETRIES = 3
BACKOFF_BASE = 2.0
BACKOFF_CAP = 30.0
REQUEST_DELAY = 1.0
MAX_REDIRECTS = 10

# Re-login this far before the bearer actually expires, so a long backfill cannot
# have the token die underneath it mid-run.
TOKEN_MARGIN = dt.timedelta(minutes=10)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)
BROWSER_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Ch-Ua": '"Chromium";v="152", "Not?A_Brand";v="24", "Google Chrome";v="152"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Upgrade-Insecure-Requests": "1",
}

# Keep this list NARROW. Verified live 2026-08-07: a *successful* portal response
# contains "_Incapsula_Resource" and "/_Incapsula_" - those are the Imperva client SDK
# <script> tags, present on every page whether blocked or not. Same for "reese84".
# Matching on them blocks perfectly good logins. Only the real block page carries
# "Request unsuccessful. Incapsula incident ID: ...".
IMPERVA_MARKERS_STRONG = ("incapsula incident", "request unsuccessful")
IMPERVA_MARKERS_WEAK = ("incident id", "robot")

# Imperva's JS challenge renders a bare "Loading" shell - but so do several legitimate
# hops in this relay, which carry their payload in a self-submitting form. Only
# meaningful when the expected field is ALSO missing; checked at the call site.
LOADING_SHELL = "<title>loading</title>"

# During a planned outage Southern Company parks EVERY customerservice2 route here -
# including unauthenticated ones. Verified 2026-09-07 during the Aug 31 - Sep 8 2026
# CIS migration: /Billing/Home and the data API both 302 to this host with no session
# at all. So a redirect here is never a credential, token, or WAF problem, and must not
# be reported as one - the old code surfaced it as the very misleading
# "login step 3: SouthernJwtCookie missing".
MAINTENANCE_HOST = "friendly-error.southernco.com"


class GaPowerError(Exception):
    """Base error."""


class GaPowerAuthError(GaPowerError):
    """Credentials rejected. Must NOT be retried - repeats risk account lockout."""


class GaPowerBotDetected(GaPowerError):
    """Imperva challenge / IP ban. Retrying now deepens it."""


class GaPowerTransient(GaPowerError):
    """Network blip, 5xx, or 429. Safe to retry later."""


class GaPowerSessionExpired(GaPowerTransient):
    """The portal rejected the session we were reusing.

    A subclass of GaPowerTransient, so every existing `except GaPowerTransient`
    still catches it. The distinct type exists only so the coordinator can tell
    "come back later" apart from "log in again and retry right now".
    """


class GaPowerMaintenance(GaPowerError):
    """Utility-side planned outage. Nothing to fix here; resolves on its own."""


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
    return False


def _attr(tag: str, name: str) -> str | None:
    """Read one HTML attribute, tolerating either quote style and loose spacing."""
    m = re.search(rf"""{name}\s*=\s*["']([^"']*)["']""", tag, re.I)
    return m.group(1) if m else None


def _exp_utc(claims: dict[str, Any]) -> dt.datetime | None:
    """The token's expiry as an aware UTC datetime, or None if it has none."""
    exp = claims.get("exp")
    try:
        return dt.datetime.fromtimestamp(float(exp), dt.timezone.utc)
    except (TypeError, ValueError):
        return None


def _jwt_claims(token: str) -> dict[str, Any]:
    """Decode a JWT payload without verifying it. {} if it is not readable."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:  # noqa: BLE001 - a malformed segment just means "not a JWT"
        return {}


def _form_post_target(body: str, base: str) -> tuple[str, dict[str, str]] | None:
    """Pull the action and hidden fields out of a self-submitting HTML form.

    Every hop of the post-ForgeRock relay is one of these: an HTML page whose only job
    is to POST a token onward via inline JS. Attribute order is not guaranteed, so
    match name and value independently within each <input> rather than as one pattern.
    """
    form = re.search(r"<form([^>]*)>(.*?)</form>", body, re.I | re.S)
    if not form:
        return None
    action = _attr(form.group(1), "action")
    if action is None:
        return None

    # Scoped to this form's own content. Reading <input>s from the whole document
    # would fold in fields from any other form on the page - harmless on the relay's
    # bare auto-submit shells, wrong the moment one of them gains a second form.
    fields: dict[str, str] = {}
    for tag in re.findall(r"<input[^>]*>", form.group(2), re.I):
        name = _attr(tag, "name")
        if name:
            fields[name] = _attr(tag, "value") or ""
    if not fields:
        return None
    return urljoin(base, action), fields


class GaPowerApi:
    """Talks to the Southern Company customer portal."""

    def __init__(self, session: aiohttp.ClientSession, username: str, password: str) -> None:
        self._session = session
        self._username = username
        self._password = password
        self.jwt: str | None = None
        self._soft_auth = False
        self.account_auth: str | None = None
        self.account: str | None = None
        self.company: str | None = None
        # Opaque ASP.NET Core DataProtection blobs ("CfDJ8..."), ~134 chars each. They
        # are tied to the app's key ring, so they are resolved fresh on every login
        # rather than cached across restarts.
        self.person_id: str | None = None
        self.premise_id: str | None = None
        self.service_point: str | None = None
        self.service_agreement: str | None = None

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
                        "Location": resp.headers.get("Location", ""),
                    }
                    self._harvest_tokens(resp.headers)
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                last = err
                if attempt == MAX_RETRIES:
                    raise GaPowerTransient(f"{type(err).__name__}: {err}") from err
            else:
                # Checked before everything else: during an outage this fires on the
                # login relay AND on the data API, so one choke point covers both.
                if MAINTENANCE_HOST in raw_headers["Location"]:
                    raise GaPowerMaintenance(
                        f"{url.split('?')[0]} redirects to {MAINTENANCE_HOST} - "
                        "Southern Company's portal is in a planned outage. "
                        "Credentials are fine; this clears when they restore service "
                        "and the next poll backfills the gap."
                    )
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

    def _harvest_tokens(self, headers: Any) -> None:
        """Mirror the portal SPA's response interceptor.

        The bearer arrives as a response HEADER on any request - not a cookie and not
        a body field, which is why /Account/LoginValidated/JwtToken read as empty:
        200, "Data": null, no Set-Cookie, and the token sitting in ScJwtToken all
        along. Straight from their own client:

            headers.get("ScJwtToken") ? setSessionToken(...)
              : headers.get("ScSoftAuthJwtToken") && setSessionToken(...)
            headers.get("ScAccountsJwtToken") && setAccountsToken(...)
        """
        full = headers.get("ScJwtToken")
        if full:
            self.jwt = full
            self._soft_auth = False
        elif headers.get("ScSoftAuthJwtToken") and (self.jwt is None or self._soft_auth):
            # Soft auth is a partially-authenticated token. Their client prefers the
            # full one, but only within a single response - across responses a later
            # soft token would silently downgrade a good session, and the occ*
            # services would start refusing us for no visible reason. Only ever
            # accept it when we do not already hold a full token.
            self.jwt = headers["ScSoftAuthJwtToken"]
            self._soft_auth = True
        account_auth = headers.get("account-authorization")
        if account_auth:
            self.account_auth = account_auth

    @staticmethod
    def _cookie(headers: dict, name: str) -> str | None:
        for raw in headers.get("Set-Cookie", []):
            m = re.search(rf"{name}=(\S*?);", raw, re.I)
            if m:
                return m.group(1)
        return None

    # ------------------------------------------------------------------ login

    def _reset_session(self) -> None:
        """Drop every trace of the previous session.

        The coordinator builds one GaPowerApi per config entry and reuses it, so the
        cookie jar outlives a poll. Discovery below depends on getting the *logged
        out* redirect chain, which an inherited session would quietly suppress - so a
        full login always starts from a clean jar.
        """
        self._session.cookie_jar.clear()
        self.jwt = None
        self._soft_auth = False
        self.account_auth = None
        self.account = self.company = None
        self.person_id = self.premise_id = None
        self.service_point = self.service_agreement = None

    def _token_valid_for(self, margin: dt.timedelta = TOKEN_MARGIN) -> bool:
        """True if the held bearer is still good, judged from its own `exp`."""
        if not self.jwt:
            return False
        expires = _exp_utc(_jwt_claims(self.jwt))
        if expires is None:
            return False  # unreadable expiry - do not gamble, re-login
        return dt.datetime.now(dt.timezone.utc) + margin < expires

    async def _session_is_live(self) -> bool:
        """Is the session from the last poll still usable?

        Answered from the token's own `exp` first, which costs nothing. Only when
        that says the token should still be good do we spend requests confirming it -
        and an expired token skips the probe entirely and goes straight to login,
        rather than paying for two requests to be told what the claim already said.
        """
        if not self._token_valid_for():
            return False
        # Drop the held token before probing. /Billing/Home answers 200 whether or
        # not anyone is signed in (confirmed with an unauthenticated curl), and
        # _fetch_jwt's only other check is `if not self.jwt` - which a token left
        # over from the previous poll satisfies for free. Between them the probe
        # could not fail, so a session the server had already dropped still read as
        # live and every request after it was doomed. Clearing first makes the probe
        # demand a freshly harvested ScJwtToken, which only a live session sends.
        self.jwt = None
        self._soft_auth = False
        try:
            await self._fetch_jwt()
        except GaPowerTransient:
            return False
        # A soft-auth token means the portal recognises the browser but not the
        # session; the occ* services will not accept it. Log in properly.
        return not self._soft_auth

    async def login(self) -> None:
        """ForgeRock OIDC login. Raises GaPowerAuthError on bad credentials.

        Deliberately does NOT mint its own OIDC request. The portal is a .NET
        confidential client: it generates `state`, `nonce` and the PKCE challenge
        itself, and validates them on the way back. Inventing our own would be
        rejected, so the flow starts by letting the portal issue its own authorize URL
        and only then authenticates against ForgeRock.
        """
        # Reuse a still-valid session rather than re-authenticating. This turns a
        # routine poll into one request instead of roughly eight, and every login
        # avoided is one fewer credential POST against a portal whose terms this
        # already stretches. GaPowerBotDetected and GaPowerMaintenance deliberately
        # propagate out of the probe - those mean stop, not try harder.
        if await self._session_is_live():
            return
        await self.force_relogin()

    async def force_relogin(self) -> None:
        """Authenticate from scratch, without consulting the current session.

        `login` reuses a session it believes is still good. When the portal has just
        told us otherwise mid-poll, that belief is the thing at fault - so the
        recovery path has to bypass it rather than ask again.
        """
        self._reset_session()
        authorize_url = await self._discover_authorize_url()
        await self._forgerock_authenticate()
        await self._replay_authorize(authorize_url)
        await self._fetch_jwt()

    async def _discover_authorize_url(self) -> str:
        """Ask webauth to issue a fresh OIDC challenge and return its authorize URL.

        Crawling /Billing/Home for this does not work: that route answers 200 with the
        Angular shell (a spinner plus the Imperva and Dynatrace agents) and performs the
        login redirect in client-side JavaScript, so the authorize URL never appears in
        any HTML we can see. Calling webauth's initiation endpoint directly is what
        produces it - verified 2026-09-08, it 302s straight to
        customerlogin/am/oauth2/authorize.

        The challenge carries a `state` and PKCE `code_challenge` that webauth mints and
        then validates on the way back, alongside a correlation cookie set on this very
        response. Both are why the request has to originate here rather than be
        assembled by us.
        """
        # Pre-encoded on purpose: WL_ReturnUrl nests a URL that itself carries an
        # encoded query, and it must reach webauth double-encoded. Handing the string
        # to yarl for encoding is not reliable here - it can treat the existing %2F as
        # already-encoded and pass it through - so build the query and mark it final.
        url = URL(f"{WEBAUTH_OIDC_INIT}?{urlencode(OIDC_INIT_PARAMS)}", encoded=True)
        status, _, headers = await self._request("GET", url)

        if status not in (301, 302, 303, 307, 308):
            raise GaPowerTransient(
                f"login initiation returned HTTP {status}, expected a redirect - "
                "webauth answers 500 when a required parameter is missing, so the "
                "portal's login parameters have most likely changed; capture a browser "
                "HAR from a logged-out state and compare OIDC_INIT_PARAMS"
            )
        location = headers["Location"]
        if "/am/oauth2/authorize" not in location:
            raise GaPowerTransient(
                "login initiation redirected somewhere unexpected: "
                f"{urlparse(location).netloc}{urlparse(location).path}"
            )
        return urljoin(str(url), location)

    async def _forgerock_authenticate(self) -> None:
        """Two-step callback exchange against AM; leaves a session cookie in the jar."""
        url = f"{FORGEROCK}{AM_AUTH_PATH}"
        params = {"authIndexType": "service", "authIndexValue": AM_SERVICE}

        # First POST is empty - AM answers with authId plus a callbacks template.
        status, body, _ = await self._request(
            "POST", url, params=params, json={}, headers=FORGEROCK_HEADERS
        )
        if status != 200:
            raise GaPowerTransient(f"forgerock: callback fetch returned HTTP {status}")
        try:
            challenge = json.loads(body)
        except ValueError as err:
            raise GaPowerBotDetected("forgerock authenticate returned non-JSON") from err
        if not challenge.get("callbacks"):
            raise GaPowerTransient("forgerock: no callbacks in the challenge")

        # Fill the inputs by callback TYPE, not by index or IDToken name. AM renumbers
        # those whenever the authentication tree is edited; the types are stable.
        for cb in challenge["callbacks"]:
            kind = cb.get("type")
            inputs = cb.get("input") or []
            if not inputs:
                continue
            if kind == "NameCallback":
                inputs[0]["value"] = self._username
            elif kind == "PasswordCallback":
                inputs[0]["value"] = self._password
            elif kind == "ConfirmationCallback":
                inputs[0]["value"] = 0  # "Log In", the first option

        status, body, _ = await self._request(
            "POST", url, params=params, json=challenge, headers=FORGEROCK_HEADERS
        )
        if status in (401, 403):
            raise GaPowerAuthError("Login rejected - check username and password")
        if status != 200:
            raise GaPowerTransient(f"forgerock: authenticate returned HTTP {status}")
        try:
            result = json.loads(body)
        except ValueError as err:
            raise GaPowerBotDetected("forgerock authenticate returned non-JSON") from err

        token_id = result.get("tokenId")
        if not token_id:
            # A surviving `callbacks` array means AM wants another factor (e.g. an OTP
            # or a security question), which this integration cannot answer.
            if result.get("callbacks"):
                raise GaPowerAuthError(
                    "Georgia Power is asking for an additional login step "
                    "(MFA or a security question). Sign in through the website once to "
                    "clear it; if MFA is enabled on the account this integration "
                    "cannot log in."
                )
            raise GaPowerAuthError("Login rejected - check username and password")

        # AM returns the session as `tokenId` in the body and does NOT set the session
        # cookie itself, so the authorize call that follows would arrive
        # unauthenticated. webauth then quietly falls through to WL_ReturnUrl and the
        # relay "succeeds" with no session at all, surfacing much later as a missing
        # ScJwtToken. Set the cookie explicitly.
        await self._set_am_session(token_id)

    async def _set_am_session(self, token_id: str) -> None:
        """Install AM's session token as a cookie on the ForgeRock domain.

        The cookie's name is deployment-specific (this tenant uses a random-looking
        hex string, not the stock `iPlanetDirectoryPro`), so ask AM for it rather than
        hard-coding it and silently breaking if they redeploy.
        """
        name = AM_COOKIE_FALLBACK
        try:
            status, body, _ = await self._request(
                "GET",
                f"{FORGEROCK}/am/json/serverinfo/*",
                headers={"Accept": "application/json"},
            )
            if status == 200:
                name = (json.loads(body) or {}).get("cookieName") or name
        except (GaPowerError, ValueError):
            # Non-fatal: fall back to the known name and let the login attempt itself
            # report the real failure if that name is wrong.
            _LOGGER.debug("serverinfo lookup failed; using fallback AM cookie name")

        self._session.cookie_jar.update_cookies(
            {name: token_id}, response_url=URL(FORGEROCK)
        )

    async def _replay_authorize(self, authorize_url: str) -> None:
        """Re-request authorize with a live AM session, then ride the relay home.

        Each hop is either a redirect or a self-submitting form carrying the auth code
        onward: AM -> webauth/signin-forgerock-oidc-authcode ->
        webauth/ExternalAuthentication -> customerservice2/Account/LoginComplete.
        """
        url, method, data = authorize_url, "GET", None

        for hop in range(MAX_REDIRECTS):
            kw: dict[str, Any] = {"data": data} if data else {}
            status, body, headers = await self._request(method, url, **kw)
            # The relay is several opaque hops and only its final symptom is visible
            # in the config entry, so trace each one. Names only - no token values.
            _LOGGER.debug(
                "relay hop %s: %s %s%s -> %s%s | set-cookie=%s | jar=%s",
                hop,
                method,
                urlparse(url).netloc,
                urlparse(url).path,
                status,
                " -> " + urlparse(headers["Location"]).path
                if headers.get("Location")
                else "",
                [c.split("=")[0].strip() for c in headers.get("Set-Cookie", [])],
                sorted({c.key for c in self._session.cookie_jar}),
            )

            if status in (301, 302, 303, 307, 308):
                location = headers["Location"]
                if not location:
                    raise GaPowerTransient(f"redirect from {url} carried no Location")
                url, method, data = urljoin(url, location), "GET", None
                parts = urlparse(url)
                # Landing on an ordinary portal page means the relay finished and the
                # session cookie is set; following it would only fetch a page we do not
                # need. /Account/* is still the relay itself, so keep going there.
                if parts.netloc == urlparse(CS2).netloc and not parts.path.lower().startswith(
                    "/account/"
                ):
                    return
                continue

            if status != 200:
                raise GaPowerTransient(f"login relay: HTTP {status} from {url.split('?')[0]}")

            target = _form_post_target(body, url)
            if target is not None:
                _LOGGER.debug(
                    "relay hop %s: form -> %s%s fields=%s",
                    hop,
                    urlparse(target[0]).netloc,
                    urlparse(target[0]).path,
                    sorted(target[1]),
                )
            if target is None:
                if LOADING_SHELL in body[:4000].lower():
                    raise GaPowerBotDetected(
                        f"{urlparse(url).path} returned a JS challenge shell"
                    )
                raise GaPowerTransient(
                    f"login relay stalled at {urlparse(url).path}: expected a redirect "
                    "or a self-submitting form, got neither"
                )
            url, method, data = target[0], "POST", target[1]

        raise GaPowerTransient("too many hops completing the login relay")

    async def _fetch_jwt(self) -> None:
        """Collect the bearer token the occ* services want.

        Read out of the authenticated portal page. Session cookies from the relay are
        already attached by aiohttp.
        """
        # Both of these are ordinary requests; the tokens ride back on their
        # response headers and _harvest_tokens picks them up. The portal page is
        # fetched first because that is what the browser does, and it is the request
        # that establishes the account-authorization header for later calls.
        status, _, _ = await self._request(
            "GET", f"{CS2}/Billing/Home", headers={"Referer": f"{CS2}/"}
        )
        if status != 200:
            raise GaPowerTransient(
                f"portal page returned HTTP {status} after login - no valid session"
            )
        await self._request(
            "GET",
            f"{CS2}/Account/LoginValidated/JwtToken",
            headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": f"{CS2}/Billing/Home",
            },
        )
        if not self.jwt:
            raise GaPowerTransient(
                "logged in, but no ScJwtToken header came back from "
                "/Account/LoginValidated/JwtToken - the portal hands the bearer over "
                "in a response header, so check whether that header was renamed"
            )
        claims = _jwt_claims(self.jwt)
        _LOGGER.debug(
            "bearer acquired: len=%s iss=%s aud=%s soft_auth=%s expires=%s",
            len(self.jwt), claims.get("iss"), claims.get("aud"), self._soft_auth,
            _exp_utc(claims),
        )

    # ------------------------------------------------------------------ data

    @property
    def _auth(self) -> dict[str, str]:
        # Mirrors the SPA's request interceptor. Capital-B "Bearer" and the
        # x-transaction-id prefix are copied from it verbatim rather than guessed.
        headers = {
            "Accept": "application/json, text/plain, */*",
            "UserId": self._username,
            "DeviceType": DEVICE_TYPE,
            "x-transaction-id": f"OCC_{uuid.uuid4()}",
            "Origin": CS2,
            "Referer": f"{CS2}/",
        }
        if self.jwt:
            headers["Authorization"] = f"Bearer {self.jwt}"
        if self.account_auth:
            headers["account-authorization"] = self.account_auth
        return headers

    async def _get_json(self, url: str, label: str, **kw: Any) -> dict:
        status, body, headers = await self._request(
            "GET", url, headers=self._auth, **kw
        )
        if status in (401, 403):
            # These services live on sibling hosts (occ*api.southerncompany.com), so
            # what actually reaches them depends on cookie domain scope - log that
            # alongside whatever the service says about the rejection.
            _LOGGER.debug(
                "%s auth rejected (%s): bearer=%s cookies_sent=%s body=%.300s",
                label,
                status,
                "yes" if self.jwt else "no",
                sorted(self._session.cookie_jar.filter_cookies(URL(url)).keys()),
                body,
            )
            # Force the next poll through a full login rather than reusing a session
            # the server has already rejected. Without this a stale session keeps
            # probing as live and the integration never recovers on its own.
            self._reset_session()
            raise GaPowerSessionExpired(f"{label}: HTTP {status} - session expired")
        if status in (301, 302, 303, 307, 308):
            # A JSON API does not redirect a request it is willing to answer. These
            # hosts turn away a stale bearer by sending the browser back to the login
            # flow rather than by returning 401, so this is a session rejection
            # wearing a different number. It used to fall through to the generic
            # non-200 branch below, which left the dead session in place to be reused
            # on every subsequent poll - observed 2026-09-10 as a 302 on
            # UsageGraphData/Hourly that stuck for a full 12h cycle.
            _LOGGER.debug(
                "%s redirected (%s) to %s - treating as a session rejection",
                label,
                status,
                urlparse(headers.get("Location") or "").path or "?",
            )
            self._reset_session()
            raise GaPowerSessionExpired(f"{label}: HTTP {status} - session rejected")
        if status != 200:
            raise GaPowerTransient(f"{label}: HTTP {status}")
        try:
            return json.loads(body)
        except ValueError as err:
            raise GaPowerBotDetected(f"{label} returned non-JSON") from err

    async def resolve_account(self) -> None:
        """Resolve the account and the four identifiers the usage API requires."""
        # The identifiers are session-scoped DataProtection blobs, so they stay valid
        # exactly as long as the session does - and _reset_session clears them together
        # with it. Skipping the re-resolve saves two requests on every reused poll.
        if all(
            (
                self.account,
                self.person_id,
                self.service_agreement,
                self.premise_id,
                self.service_point,
            )
        ):
            return

        payload = await self._get_json(f"{ACCOUNT_API}/Cap/", "Cap")
        accounts = payload.get("data") or []
        if not accounts:
            raise GaPowerError("No accounts returned")
        acct = next((a for a in accounts if a.get("isPrimaryAccount")), accounts[0])
        self.account = str(acct.get("accountNumber") or "")
        self.company = acct.get("company") or "GPC"
        if not self.account:
            raise GaPowerError(f"Account number not found; keys: {sorted(acct)}")

        summary = await self._get_json(
            f"{ACCOUNT_API}/Accounts/{self.account}/Summary", "Accounts/Summary"
        )
        data = summary.get("data") or {}
        self.person_id = data.get("mainPersonId")

        # Accounts can carry gas and other agreements alongside electric; only the
        # active electric one has hourly interval data behind it.
        agreements = data.get("serviceAgreements") or []
        agreement = next(
            (
                a
                for a in agreements
                if a.get("serviceTypeCode") == "E" and a.get("isActive")
            ),
            None,
        )
        if agreement is None:
            kinds = sorted({str(a.get("serviceTypeCode")) for a in agreements})
            raise GaPowerError(f"No active electric service agreement; found {kinds}")
        self.service_agreement = agreement.get("serviceAgreementId")
        self.premise_id = agreement.get("premiseId")

        points = agreement.get("servicePoints") or data.get("servicePoints") or []
        if points:
            self.service_point = points[0].get("servicePointId")

        missing = [
            n
            for n, v in (
                ("personId", self.person_id),
                ("serviceAgreementId", self.service_agreement),
                ("premiseId", self.premise_id),
                ("servicePointId", self.service_point),
            )
            if not v
        ]
        if missing:
            raise GaPowerError(f"Account summary missing required ids: {missing}")

    async def async_get_bill_periods(self) -> list[tuple[dt.date, dt.date]]:
        """Every bill cycle the portal knows about, oldest first, `end` EXCLUSIVE.

        Cheap: same host and bearer as the usage call, and `resolve_account` already
        holds all three identifiers, so this is one extra GET of about 1.2 KB.

        The cycle has to be read rather than assumed. The user's own bill dates land
        on day 25-28 of the month with cycles of 29-32 days, because it is meter-read
        driven and not calendar driven - a hardcoded "reset on the 28th" would have
        been wrong on 3 of the last 7 cycles, and wrong precisely at the boundary,
        which is the one place it changes which cycle an hour is charged to.

        WARNING: the parameters are PascalCase. The sibling UsageGraphData route on
        this very host is camelCase. This API answers a wrong parameter shape with an
        empty result rather than an error, so a casing slip here would not raise - it
        would look exactly like "this account has no bill periods" and be believed.
        """
        today = dt.date.today()
        params = {
            "ServiceAgreementId": self.service_agreement,
            "ServicePointId": self.service_point,
            "PremiseId": self.premise_id,
            "StartDate": (
                today - dt.timedelta(days=BILL_PERIOD_LOOKBACK_DAYS)
            ).strftime("%m/%d/%Y"),
            # Deliberately today, not today+1. Unlike the usage routes this window is
            # a filter over whole periods rather than a day range, and a capture of
            # the portal's own SPA shows EndDate=<capture date> returning the
            # in-progress cycle. Mirror the shape that is known to work.
            "EndDate": today.strftime("%m/%d/%Y"),
            "OperatingCompany": self.company,
        }
        payload = await self._get_json(
            f"{USAGE_API}/MyPowerUsage/BillPeriods", "BillPeriods", params=params
        )
        periods = _parse_bill_periods(payload)
        if not periods:
            # Key names only - never values. This is the one diagnostic that matters
            # if the response shape is not what _parse_bill_periods expects, and
            # without it a schema mismatch is indistinguishable from a new account.
            _LOGGER.warning(
                "BillPeriods returned no usable periods (top-level keys: %s). Falling "
                "back to the last known cycle boundary.",
                sorted(payload) if isinstance(payload, dict) else type(payload).__name__,
            )
        else:
            _LOGGER.debug(
                "BillPeriods: %s cycles, most recent %s -> %s (end exclusive)",
                len(periods), periods[-1][0], periods[-1][1],
            )
        return periods

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
        # CHUNK_DAYS=30 was carried over from the retired MPUData endpoint, and is now
        # confirmed on this route too: a 45-day refetch on 2026-09-09 returned 1081
        # points across two chunks (45 x 24 = 1080) and the short-window check below
        # stayed silent. No server-side cap. If that ever changes, the portal states
        # its own limit at
        # occcustomerserviceapi /api/v1/Utilities/getRegistryValue?key=HOURLY_BULK_EXPORT_DAYS,
        # which is worth reading before guessing at a new value.
        params = {
            "accountId": self.account,
            "personId": self.person_id,
            "operatingCompany": self.company,
            "startDate": start.strftime("%m/%d/%Y"),
            # endDate is EXCLUSIVE -> last wanted day + 1
            "endDate": (last_day + dt.timedelta(days=1)).strftime("%m/%d/%Y"),
            "servicePointId": self.service_point,
            "premiseId": self.premise_id,
            # Literal string "null" - that is what the portal sends, and an omitted
            # parameter is not equivalent.
            "billFactorCode": "null",
            "intervalBehavior": "Automatic",
        }
        payload = await self._get_json(
            f"{USAGE_API}/MyPowerUsage/UsageGraphData/{self.service_agreement}/Hourly",
            "UsageGraphData/Hourly",
            params=params,
        )
        data = payload.get("data") or {}
        if not data.get("hasData"):
            _LOGGER.debug("No hourly data for %s .. %s", start, last_day)
            return None
        inner = data.get("data")
        # The old MPUData route nested a JSON *string* here; the replacement returns a
        # real object. Accept both so a rollback in either direction still parses.
        if isinstance(inner, str):
            try:
                inner = json.loads(inner)
            except ValueError:
                return None
        if not isinstance(inner, dict):
            return None

        # Verify what came back rather than trusting CHUNK_DAYS to still be safe. A
        # server-side cap introduced later would return a short window and silently
        # leave holes in the history - the worst failure mode this has, because the
        # import succeeds and nothing looks wrong.
        labels = (inner.get("xAxis") or {}).get("labels") or []
        wanted = (last_day - start).days + 1
        if labels and len(labels) < wanted * 24 * 0.9:
            _LOGGER.warning(
                "Requested %s days of hourly data (%s..%s) but got %s points, about "
                "%.1f days. The endpoint is probably capping the window - lower "
                "CHUNK_DAYS (currently %s) to avoid gaps.",
                wanted, start, last_day, len(labels), len(labels) / 24, CHUNK_DAYS,
            )
        else:
            _LOGGER.debug(
                "chunk %s..%s: %s points for %s day(s) requested", start, last_day,
                len(labels), wanted,
            )
        return inner


def _parse_ts(pt: dict[str, Any]) -> dt.datetime | None:
    """Points carry a naive local ISO timestamp in `name`.

    `x` used to be a constant 0 and is now the hour's index within the response;
    either way it is not a timestamp, so `name` remains the only usable field.
    """
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


# Keys that could name the two ends of a bill period. The response BODY was never
# captured - the browser trace recorded this request and its 1223-byte length, not
# its content - so match on meaning instead of on an exact schema. Every other route
# on this host answers in camelCase even where it takes PascalCase parameters, and
# the comparison below is case-folded so either works.
_PERIOD_START_KEYS = (
    "startdate", "billstartdate", "periodstartdate", "fromdate", "start",
)
_PERIOD_END_KEYS = (
    "enddate", "billenddate", "periodenddate", "todate", "end",
)


def _as_date(value: Any) -> dt.date | None:
    """Best-effort date out of whatever this portal put in a date field."""
    if not isinstance(value, str):
        return None
    s = value.strip()
    m = re.search(r"/Date\((-?\d+)", s)  # legacy ASP.NET epoch-millis form
    if m:
        return dt.datetime.fromtimestamp(int(m.group(1)) / 1000).date()
    head = re.split(r"[T ]", s, 1)[0]  # drop any time component
    # Ordering is not arbitrary: "2026/09/29" cannot parse as %m/%d/%Y (month 2026),
    # and "09/29/2026" cannot parse as %Y-%m-%d, so each format rejects the other's
    # input rather than silently transposing month and day.
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%Y/%m/%d"):
        try:
            return dt.datetime.strptime(head, fmt).date()
        except ValueError:
            continue
    return None


def _normalise_ends(
    pairs: list[tuple[dt.date, dt.date]]
) -> list[tuple[dt.date, dt.date]]:
    """Rewrite `end` as an EXCLUSIVE bound, whichever convention the API meant.

    Consecutive bill cycles tile - one ends exactly where the next begins - so the
    convention can be read straight off the data instead of guessed. If a period's
    end equals the next period's start it is already exclusive; if it falls one day
    earlier the API meant it inclusively and a day is added.

    This changes one thing only: whether the last day of a cycle belongs to it. That
    is also the hour that decides which bill a spike is charged to, so it is worth
    taking from evidence rather than assumption.
    """
    if len(pairs) < 2:
        return pairs
    day = dt.timedelta(days=1)
    touching = sum(1 for (_, e), (s, _) in zip(pairs, pairs[1:]) if e == s)
    gapped = sum(1 for (_, e), (s, _) in zip(pairs, pairs[1:]) if e + day == s)
    if gapped > touching:
        _LOGGER.debug("BillPeriods end dates look inclusive; shifting to exclusive")
        return [(s, e + day) for s, e in pairs]
    return pairs


def _parse_bill_periods(payload: dict[str, Any]) -> list[tuple[dt.date, dt.date]]:
    """(start, end_exclusive) pairs from a BillPeriods response, oldest first."""
    records: Any = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(records, dict):
        # Tolerate one extra level of wrapping, as UsageGraphData does with its
        # data.data. Take the first list-valued member rather than naming a key we
        # have not actually seen.
        records = next(
            (v for _, v in sorted(records.items()) if isinstance(v, list)), None
        )
    if not isinstance(records, list):
        return []

    pairs: list[tuple[dt.date, dt.date]] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        lowered = {str(k).lower(): v for k, v in rec.items()}
        start = end = None
        for key in _PERIOD_START_KEYS:
            start = _as_date(lowered.get(key))
            if start:
                break
        for key in _PERIOD_END_KEYS:
            end = _as_date(lowered.get(key))
            if end:
                break
        # A zero- or negative-length period is a parse failure, not a short cycle.
        if start and end and end > start:
            pairs.append((start, end))

    pairs.sort()
    return _normalise_ends(pairs)
