# Georgia Power for Home Assistant

See your Georgia Power electricity use in Home Assistant's Energy Dashboard — **hour by hour**,
with cost, going back a full year.

No extra hardware. No separate server or script to run. No add-ons. You enter your
georgiapower.com login once, and Home Assistant does the rest.

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

## How this compares to the alternatives

**Snapshot taken 2026-09-10.** All of these are actively developed — check their repos before
taking this table as current.

If you searched for this, you probably found some combination of four links. Two of those are
the same repository, and two are developer libraries rather than something you can install:

| | What it actually is |
|---|---|
| [`apearson/southern-company-api`](https://github.com/apearson/southern-company-api) | A **Node.js library**. Not a Home Assistant integration — you'd have to build something with it. |
| [`Southern-Company-HA/southern_company_api`](https://github.com/Southern-Company-HA/southern_company_api) | A **Python library**, published to PyPI. Also not an integration by itself. |
| [`Southern-Company-HA/southern-company-hacs`](https://github.com/Southern-Company-HA/southern-company-hacs) | **The real alternative.** A Home Assistant integration that wraps the Python library above. |
| [`Lash-L/southern-company-hacs`](https://github.com/Lash-L/southern-company-hacs) | **The same repository as the one above**, under its old name — GitHub redirects it. Not a separate option. |

So for a Home Assistant user there is really one alternative. Here is an honest comparison —
limited to things I actually verified, because I have not read their code closely enough to tell
you what it lacks:

| | southern-company-hacs | this |
|---|---|---|
| Utilities covered | Georgia, Alabama, Mississippi Power, Nicor Gas | **Georgia Power only** |
| Python dependencies | a pinned PyPI library (`southern-company-api`) | **none** — `requirements: []`, uses what Home Assistant already ships |
| What HACS installs by default | newest tagged release: `1.0.0`, Aug 2024 | this repo's default branch |
| Users | 34 stars, years of real-world use | new, essentially untested by anyone but me |

**Pick theirs if** you're not in Georgia, you have Nicor Gas, or you want the option with more
people behind it and more eyes on it. For most readers that is the right answer, and I'd rather
say so than pretend otherwise.

**Pick this if** you're a Georgia Power customer specifically after hourly data, and would rather
maintain one self-contained thing than an integration plus a separately-versioned library.

### What this one does differently

Stated as what it does, not as what anything else doesn't:

- **No dependencies at all.** One folder, `aiohttp`, done. Nothing to version-pin, nothing that can
  break because a shared library shifted underneath it.
- **A status sensor that stays available when an update fails**, reporting `ok` / `invalid_auth` /
  `utility_outage` / `blocked` / `error` with the message attached. Every other sensor here reports
  the last good run, so they all go `unavailable` at exactly the moment you need an explanation.
- **A rejected session re-authenticates and retries immediately** rather than waiting out the
  twelve hours to the next scheduled update — once, never in a loop, because repeated failed
  logins risk locking a utility account.
- **Re-reads 45 days every run**, so Georgia Power's retroactive cost corrections actually land
  instead of freezing at the provisional rate.
- **Refuses to guess.** Where this can't tell a real value from a placeholder, it imports nothing
  rather than importing something wrong.

### Credit, and a caveat about this whole section

- **`apearson/southern-company-api` worked the login chain out first**, and this project's
  pre-migration flow was ported from it. That's where the hard part came from.
- **Both Python projects above shipped fixes for the September 2026 Georgia Power login change on
  the same day this was written.** They are actively maintained. The tagged-release row is a real
  difference for HACS users *today* and is one `git tag` away from being wrong.
- An earlier draft of this table claimed their hourly path was broken on residential accounts,
  citing [issue #124](https://github.com/Southern-Company-HA/southern-company-hacs/issues/124).
  **That issue is closed and appears to have been fixed in August 2026.** Left here as a note
  because a comparison table that quietly drops its wrong claims is worse than one that says so.

---

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

- Alabama Power, Mississippi Power, Nicor Gas — use `southern-company-hacs` for those
- More than one account on a single login
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
