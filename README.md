# Georgia Power for Home Assistant

**Hourly electricity usage and cost in Home Assistant's Energy Dashboard — for Georgia Power, and
very likely Alabama Power and Mississippi Power too.**

Hour by hour, with cost, going back a full year. No extra hardware, no separate server or script,
no add-ons. You sign in once with your utility account and Home Assistant does the rest.

> ## 🔌 Alabama Power and Mississippi Power customers — this should work for you
>
> Despite the name, **nothing in the login or the data path is Georgia-specific.** Southern Company
> runs **one sign-in system for all of its operating companies**, and this talks to the shared
> services behind it.
>
> Verified by probing the live endpoint on 2026-09-10: the operating-company parameter isn't
> validated *at all* — Georgia, Alabama, Mississippi, a nonsense value and an empty string every
> one return the same valid sign-in challenge. The company that actually matters is read from
> **your own account** after you sign in, and passed through automatically.
>
> **It is untested on Alabama and Mississippi**, because I only have a Georgia Power account — but
> there is no code path that turns them away. If you try it, please
> [open an issue](https://github.com/jinaths/gapower-ha/issues) either way. **A confirmed "it
> works" is worth as much to me as a bug report**, and I'll update this the day someone tells me.
>
> Gas service (Nicor) won't work — it isn't electric. See
> [Other Southern Company utilities](#other-southern-company-utilities).

---

## What you get

Your Energy Dashboard fills in with real metered data from Georgia Power:

- **Hourly** kilowatt-hours and dollars — not just a daily total
- **About a year of history** backfilled the first time it runs
- Automatic updates twice a day after that
- A handful of sensors so you can tell at a glance whether it's still working

This reads your account. It never changes anything, never pays a bill, and never touches
your thermostat or anything else.

## Before you start

You need:

- **Home Assistant** 2024.6 or newer (any install type)
- A **Georgia Power online account** — the same username and password you use at
  georgiapower.com. If you've never signed in online, create the account there first.
- A **residential account with an AMI ("smart") meter.** Nearly all Georgia Power residential
  customers have one. If your meter is still read by hand, hourly data won't exist.

That's it.

### Other Southern Company utilities

The technical detail behind the callout at the top, for anyone who wants to check the reasoning
rather than take my word for it:

- **The login is operating-company agnostic**, verified 2026-09-10 by probing
  `webauth.southernco.com/SPA/ExternalAuthentication/forgerock-oidc-authcode` unauthenticated.
  The `Company` parameter a real browser sends — `GPC` — is **not validated**: `APC`, `MPC`,
  `SCS`, a nonsense value and an empty string all return the same `302` to ForgeRock's authorize
  endpoint with a fresh `state` + PKCE challenge. There is one realm for all operating companies.
- **The operating company is read per-account**, not hardcoded. After sign-in, `resolve_account`
  takes `company` off your own account record and passes it to the usage API as
  `operatingCompany`.
- **Service selection is generic** — it picks your first active **electric** service agreement by
  `serviceTypeCode`, not by state.
- Everything talks to the shared `southernco.com` / `southerncompany.com` hosts, which serve every
  operating company.

**Nicor Gas will not work.** It's gas, not electric, so the electric-agreement filter finds
nothing, and the portal handles gas differently anyway. Use
[`southern-company-hacs`](https://github.com/Southern-Company-HA/southern-company-hacs) — it
supports Nicor properly.

What stays Georgia-flavoured is purely cosmetic: the integration is titled "Georgia Power" and its
sensors are named to match. That's a label, not a restriction.

## Install

### With HACS (recommended)

1. Open **HACS** in Home Assistant
2. Click the **⋮** menu (top right) → **Custom repositories**
3. Paste `https://github.com/jinaths/gapower-ha`, choose category **Integration**, click **Add**
4. Find **Georgia Power** in the list and click **Download**
5. **Restart Home Assistant**
6. Go to **Settings → Devices & Services → + Add Integration**, search for **Georgia Power**,
   and enter your georgiapower.com username and password

### Without HACS

Copy the `custom_components/gapower/` folder into your Home Assistant `config/custom_components/`
folder, restart, then follow step 6 above.

## Add it to the Energy Dashboard

1. **Settings → Dashboards → Energy**
2. Under *Grid consumption*, click **Add consumption**
3. Pick **Georgia Power Energy Consumption**
4. For cost, choose **Use an entity tracking the total costs** and pick
   **Georgia Power Energy Cost**

The first run pulls roughly a year of history and takes a couple of minutes. Energy Dashboard
graphs may take up to an hour to catch up after that — that's Home Assistant, not this
integration.

## What to expect (please read — these are normal)

**Your data is one to two days behind.** Georgia Power publishes to their own website on a delay,
and this reads the same thing you'd see there. Today's usage will not appear today. This is the
single most common surprise, and there is no setting that fixes it — the data does not exist yet.

**Recent costs start low and correct themselves.** Until a billing cycle closes, Georgia Power
shows unbilled hours at an energy-only rate of about $0.056/kWh. Once the bill is issued, those
same hours are restated at the full rate — roughly three times higher. The integration re-reads
the last 45 days on every update specifically so those corrections get picked up, so recent dollar
figures will rise as they firm up. **Kilowatt-hours are never affected**, only cost.

**It updates twice a day, not continuously.** Polling harder would gain nothing (see the lag
above) and would draw attention from Southern Company's bot protection.

**Southern Company changes their login flow now and then.** When they do, this breaks until it's
updated — expect that maybe once or twice a year. It happened in September 2026 and was fixed.
Your existing history is never lost, and the gap fills itself in once it's working again.

## The sensors

| Sensor | What it tells you |
|---|---|
| **Status** | `ok`, or *why* it isn't — `invalid_auth`, `utility_outage`, `blocked`, `error` |
| **Last reading** | Timestamp of the newest hour imported. If this stops moving, something's wrong. |
| **Latest hour usage** | kWh in that hour |
| **Latest hour cost** | Cost of that hour |
| **Rows imported** | How many hours the last update wrote (diagnostic) |

**Status is the one to watch.** It's deliberately the only sensor that stays available when an
update fails — all the others report values from the last good run, so they all go `unavailable`
at exactly the moment you need something to explain itself. Status stays up and names the cause,
and carries the full error message in its `last_error` attribute.

### Optional: get told when it stops working

A broken scraper and a quiet night look identical on a graph. This automation catches both, and
tells you which:

```yaml
alias: Georgia Power data stale
triggers:
  - trigger: template
    # `ts is none` catches unknown/unavailable — total failure — which a bare
    # subtraction misses, because now() - None raises and the trigger never fires.
    value_template: >
      {% set ts = states('sensor.georgia_power_last_reading') | as_datetime %}
      {{ ts is none or (now() - ts).total_seconds() > 172800 }}
    for: "01:00:00"
actions:
  - action: notify.persistent_notification
    data:
      message: >
        Georgia Power: {{ states('sensor.georgia_power_status') }}.
        {{ state_attr('sensor.georgia_power_status', 'last_error') }}
```

Swap `notify.persistent_notification` for your phone's notify service to actually get told —
a persistent notification only appears inside Home Assistant, which is no use if you aren't
looking at it.

---

## How this compares to the alternative

**Snapshot: 2026-09-10.** Everything below was checked against their live repository and manifest
that day, not from memory. They ship often — verify before relying on it.

Searching for this turns up three other projects. Two of them are **developer libraries**, not
something you can install into Home Assistant:

| | What it actually is |
|---|---|
| [`apearson/southern-company-api`](https://github.com/apearson/southern-company-api) | A **Node.js library**. You'd have to build something with it. It worked the original login chain out first, and this project's pre-migration flow was ported from it. |
| [`Southern-Company-HA/southern_company_api`](https://github.com/Southern-Company-HA/southern_company_api) | A **Python library** on PyPI. Not an integration by itself — it's the engine the one below runs on. |
| [`Southern-Company-HA/southern-company-hacs`](https://github.com/Southern-Company-HA/southern-company-hacs) | **The real alternative.** A Home Assistant integration wrapping the library above. |

So there is one genuine alternative, and it is good. Here is the honest head-to-head.

### Side by side

| | southern-company-hacs | this |
|---|---|---|
| Utilities | Georgia, Alabama, Mississippi Power, **+ Nicor Gas** | Southern Company electric (Georgia tested) |
| Multiple accounts on one login | **yes** | no — one per config entry |
| Sensors | **~20** — projected bill high/low, average daily cost & usage, days used, outdoor temp, meter-read type, gas therms | 5, one of which reports *why* it broke |
| Daily fallback if hourly is unavailable | **yes** | no — hourly or nothing |
| Python dependencies | `southern-company-api==0.7.1` (pinned) | **none** — `requirements: []` |
| Poll interval | every **60 minutes** | every **12 hours** |
| History re-read each run | **31 days** | **45 days** |
| Maturity | 34 stars, `quality_scale: bronze`, years of use | new, essentially untested by anyone but me |

### Why you might pick this one

Four concrete reasons, not vibes:

**1. The 45-day re-read is a correctness fix, not a bigger number.**

This is the one that actually matters, and it is subtle. Georgia Power shows unbilled hours at an
energy-only rate near **$0.056/kWh**, then restates those same hours at the full rate — roughly
**three times higher** — once the billing cycle closes. An integration only captures that
correction if it re-reads far enough back to cross the cycle boundary.

Georgia Power bill cycles are **meter-read driven, not calendar driven**. Seven consecutive real
cycles measured **29, 30, 30, 31, 32 and 32 days**. A **31-day** window is *shorter than some
cycles*. When a 32-day cycle closes, the oldest hours in it fall outside a 31-day re-read — so
their cost never gets corrected, and stays understated in your Energy Dashboard permanently.

45 days clears the longest observed cycle with room to spare. Usage in kWh is unaffected either
way; this is purely about the dollars being right.

**2. Nothing to keep in sync.**

`requirements: []`. One folder, using the `aiohttp` that Home Assistant already ships.

That isn't a style preference — Southern Company breaks the login flow once or twice a year, and
this determines how a fix reaches you. When the September 2026 migration landed, the alternative
needed a fix in the **library**, a **PyPI release**, then a dependency bump and release in the
**integration** — commits in two repositories on the same day, plus a version bump in a third
place. Here the same fix is one file, and you get it by pulling the repo.

**3. Twelve hours between polls, not one.**

Their hourly cadence earns its keep — they surface live-ish billing projections that genuinely
move during the day. This integration does one thing, hourly statistics, and **that data lags
24–48 hours no matter what**, so polling 24 times a day would fetch the same answer 24 times.

That matters because there is no public API here. This signs in the way a browser does, against a
portal with Imperva bot protection and terms that don't contemplate automation. **Two requests a
day instead of twenty-four is a twelvefold smaller footprint** on someone else's infrastructure,
using your real credentials. That's a deliberate choice, and it's why you won't find a
"poll faster" option.

**4. When it breaks, it tells you what broke.**

Every sensor in both projects reports a value from the last successful update — so when an update
fails, they all go `unavailable` together, at exactly the moment you need an explanation. Their
sensor list is entirely data values.

This one adds a **Status** sensor that is deliberately *always available*: `ok`, `invalid_auth`,
`utility_outage`, `blocked` or `error`, with the underlying message on a `last_error` attribute.
The difference in practice is between an alert that says "the sensor is unavailable" and one that
says "Georgia Power rejected your password — and this will not retry on its own, because repeated
failures risk locking your utility account."

A rejected session also re-authenticates and retries **once**, immediately, rather than waiting out
the twelve hours to the next scheduled update.

### Why you might not

Genuinely — for a lot of readers theirs is the better answer:

- **You're not in Georgia**, or you have **Nicor Gas**. Theirs covers gas properly; this doesn't.
- **You have several accounts** on one login. Theirs handles that; this doesn't.
- **You want the billing projections** — projected bill, average daily cost, days into the cycle.
  Theirs has roughly four times as many sensors and they're useful ones.
- **Your meter isn't AMI**, so hourly data doesn't exist. Theirs falls back to daily; this
  imports nothing.
- **You'd rather run what other people run.** 34 stars, a HACS quality scale, and years of
  real-world use against a project with one user is a real argument, and I'd make it too.

### Two honest notes

- **Both Python projects shipped fixes for the September 2026 Georgia Power login change on the
  same day this was written.** They are actively maintained. Any "they're stale" framing you read
  elsewhere — including in an earlier draft of this file — is wrong.
- An earlier draft claimed their hourly path was broken on residential accounts, citing
  [issue #124](https://github.com/Southern-Company-HA/southern-company-hacs/issues/124).
  **That issue is closed and was fixed in August 2026.** The claim is gone. It's recorded here
  rather than quietly deleted, because a comparison table that drops its wrong claims without
  saying so shouldn't be trusted on the ones it keeps.

## Is this allowed?

Worth knowing before you install it:

- **This is very likely against Southern Company's terms of service.** There is no public API;
  this signs in the way a browser does and reads your own data. It's personal, read-only access to
  your own account at about two requests every twelve hours. That tradeoff is yours to make
  knowingly, and it is why this doesn't poll more often.
- **Your password is stored the way every Home Assistant integration stores credentials** — as
  plain JSON in `config/.storage/core.config_entries`. Anyone with file access to your Home
  Assistant can read it. **Use a Georgia Power password you don't reuse anywhere else.**
- Your credentials go to Southern Company and nowhere else. This project has no server, collects
  nothing, and phones nothing home.

## Not supported

- **Nicor Gas** — gas service, handled differently by the portal. Use `southern-company-hacs`.
- **Alabama / Mississippi Power** — probably fine (see above), but genuinely untested.
- **More than one account on a single login** — one account per config entry, and the first
  active electric agreement wins
- Sub-hourly data — Southern Company doesn't expose it (15-minute intervals return empty)
- Prepaid accounts, and meters that aren't AMI

## Something's wrong

1. **Check the Status sensor first** — it names the cause and carries the message.
2. `utility_outage` or `blocked` → wait. Both clear on their own; reloading makes a bot-detection
   block worse, not better.
3. `invalid_auth` → your password changed, or Georgia Power wants you to reset it. Sign in at
   georgiapower.com in a browser to confirm, then re-enter it in Home Assistant. **This
   deliberately does not retry on its own** — repeated failed logins risk locking your utility
   account.
4. Still stuck → open an issue with your Home Assistant version and the Status sensor's
   `last_error`. **Don't paste raw logs without reading them first.**

---

## For developers

<details>
<summary>How the login works, and the endpoint traps (click to expand)</summary>

### The September 2026 migration

Southern Company ran a CIS migration Aug 31 – Sep 8 2026 that replaced the entire authentication
system and moved the usage API to new hosts.

| | before | after |
|---|---|---|
| Credentials | `webauth…/webservices/api/WebUser/Login` (flat JSON) | `customerlogin.southernco.com/am/json/realms/root/realms/alpha/authenticate` (ForgeRock `callbacks[]`) |
| Token exchange | scrape `ScWebToken` from a hidden input | `/am/oauth2/authorize`, OIDC auth-code + **PKCE S256**, `response_mode=form_post` |
| Usage data | `customerservice2api…/MPUData` | `occmypowerusageapi…/api/v1/MyPowerUsage/UsageGraphData/{serviceAgreementId}/Hourly` |

Things that cost real time to work out:

- **The API bearer token arrives as a *response header*, `ScJwtToken`** — not a cookie, not a body
  field, and it is sent on *every* response. `/Account/LoginValidated/JwtToken` answers `200` with
  `"Data":null` and no `Set-Cookie`; the token was in the headers the whole time. Their own Angular
  bundle harvests it with a response interceptor, and so does this.
- **ForgeRock does not set the AM session cookie for you.** You must read `tokenId` out of the
  auth response and install the cookie yourself. The cookie's *name* is tenant-specific — read it
  from `/am/json/serverinfo/*` rather than assuming `iPlanetDirectoryPro`.
- **Fill ForgeRock callbacks by `type`, never by array index or `IDTokenN` name.** The live tree
  renumbers them.
- **A `3xx` from the JSON API means your session was rejected**, not that something moved. These
  hosts redirect a stale bearer back to the login flow instead of returning `401`.
- **`/Billing/Home` returns `200` to anyone**, signed in or not — so it cannot be used as a test of
  whether a session is still alive.

### Endpoint quirks — do not "fix" these

Each of these silently returns empty rather than erroring, which is this API's whole personality:

1. **`endDate` is exclusive.** `startDate == endDate` returns nothing. Always pass last-wanted-day + 1.
2. **No time component on dates.** `MM/DD/YYYY` works; `"MM/DD/YYYY 11:59:59 PM"` fails even with the +1.
3. **`intervalBehavior` must be `Automatic`** (or `Interval`). `Hourly` returns null on the `/Hourly` route.
4. **Parameter casing is inconsistent between sibling routes on the same host.** `UsageGraphData`
   takes camelCase (`servicePointId`); `BillPeriods` takes PascalCase (`ServicePointId`).

### On bot detection

A *successful* response contains `_Incapsula_Resource`, `/_Incapsula_` and sometimes `reese84` —
that's the Imperva client SDK, present whether or not you're blocked. Matching on any of those
blocks working logins. Only `"Request unsuccessful"` / `"Incapsula incident"` mean actually blocked.

### If you capture a HAR to debug this

⚠️ **Chrome's "Save as HAR (sanitized)" is not enough.** It strips cookies, auth headers and the
`"password"` JSON key — but it does **not** understand ForgeRock's `callbacks[]` array, so the
account password lands in the "sanitized" file in plaintext anyway. Parse HARs with a redacting
script rather than reading them raw, delete them afterwards, and rotate any password one has
touched.

</details>
