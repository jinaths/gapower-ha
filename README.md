# Georgia Power for Home Assistant

Pulls **hourly** electricity usage and cost from your Georgia Power (Southern Company) account
into the Home Assistant **Energy Dashboard**. Runs entirely inside HA — no external host, no
cron, no separate Python environment.

- Hourly kWh + cost, imported as long-term statistics
- ~365 days backfilled on first run, then incremental
- Native config flow, and a reauth prompt if the credentials stop working
- **Zero PyPI dependencies** — uses `aiohttp`, which HA already ships

> ## Status: working
>
> Southern Company's Aug 31 – Sep 8 2026 CIS migration **replaced the entire login
> system** with ForgeRock/PingAM OIDC and moved the usage API to new hosts. The auth
> chain and data client were rewritten for it and have been running since
> 2026-09-08 — see [2026-09 migration](#2026-09-southern-company-migration) for the
> full mapping of what changed and why the old flow could not be patched.

## Why not `southern-company-hacs`?

That integration is the obvious alternative. Reasons this exists instead:

| | southern-company-hacs | this |
|---|---|---|
| Last tagged release | `1.0.0`, Aug 2024 (HACS installs this, not `main`) | — |
| In HACS default store | no — manual custom repo either way | no |
| Dependency | pins `southern-company-api==0.7.0` | none |
| Auth first hop | that library's older `GET` + scrape `data-aft` | the newer JSON `POST` flow |
| Hourly data | reported broken on residential accounts (issue #124) | verified working |
| Failure backoff | 600s, in-memory | escalating, survives restarts |

Full analysis: [`../georgia-power-integration.md`](../georgia-power-integration.md).

## Install

HACS → three-dot menu → **Custom repositories** → add this repo, category **Integration** →
install → restart HA → **Settings → Devices & Services → Add Integration → Georgia Power**.

Or copy `custom_components/gapower/` into your HA `config/custom_components/` and restart.

## Set up the Energy Dashboard

**Settings → Dashboards → Energy → Add consumption**, pick *Georgia Power Energy Consumption*,
and attach *Georgia Power Energy Cost* as its cost. Statistics appear under
`gapower:energy_consumption` and `gapower:energy_cost`.

The first run backfills a year, which takes a couple of minutes and ~13 requests. After that it
polls twice a day and imports only the last few hours.

## Entities

| Entity | Purpose |
|---|---|
| `sensor.georgia_power_last_reading` | Timestamp of the newest hour imported — **watch this for staleness** |
| `sensor.georgia_power_latest_hour_usage` | kWh in that hour |
| `sensor.georgia_power_latest_hour_cost` | Cost of that hour |
| `sensor.georgia_power_rows_imported` | Rows written on the last run (diagnostic) |
| `sensor.georgia_power_status` | `ok` / `invalid_auth` / `utility_outage` / `blocked` / `error`, with the failure message on a `last_error` attribute |

`status` is the only entity here that stays **available when a poll fails** — the
others report values from the last successful run, so they all go `unavailable` at
exactly the moment something needs to explain itself. Alert on the others; read
`status` to find out why.

### Staleness alarm

A broken scraper looks identical to "no new data." Watch the timestamp:

```yaml
alias: Georgia Power data stale
triggers:
  - trigger: template
    # `ts is none` catches unknown/unavailable - total integration failure - which a
    # bare subtraction misses, because now() - None raises and the trigger never fires.
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

Read `status` in the message rather than just reporting that the data sensor is
`unavailable`. "Unavailable" is true of a wrong password, a utility outage, a WAF
block and a network blip alike, and the difference decides whether there is
anything for you to do.

## Expectations

- Data lags the portal by **~24–48 hours**. The most recent hours will be missing; that's normal.
- **Recent cost is provisional and gets revised.** Verified against the live API 2026-08-09:
  unbilled hours carry an energy-only charge (~$0.056/kWh) which Georgia Power trues up to the
  full effective rate (~$0.17/kWh) once the billing cycle closes. So the last few weeks of cost
  will read low and then correct themselves. The integration re-imports the last
  `REFETCH_DAYS` (45) every run specifically so those corrections get picked up — don't lower
  that value below a billing cycle or the understated numbers become permanent.
  Usage (kWh) is not affected, only cost.
- Southern Company changes their login flow every so often. When they do, this breaks until the
  auth chain is updated — expect that once or twice a year. It happened in September 2026, and
  that one was a full replacement rather than a tweak: see
  [2026-09 migration](#2026-09-southern-company-migration).
- Bot protection (Imperva) can block requests from your IP for ~30 minutes. The integration backs
  off rather than hammering.
- **Sessions are reused between polls, and the portal drops them without warning.** When it does,
  the usage API answers with a `302` back to the login flow rather than a `401`. That is treated as
  a session rejection: the integration re-authenticates and retries the poll once, so a dropped
  session costs a few extra requests rather than the twelve hours until the next scheduled run.
  It retries exactly once — a genuinely rejected account must never become a login loop against
  the utility.

## 2026-09 Southern Company migration

Southern Company ran a planned CIS migration **Aug 31 – Sep 8 2026**. During the outage every
`customerservice2` route — including unauthenticated ones — `302`'d to
`friendly-error.southernco.com`; the integration now detects that specifically and reports it as
a utility-side outage rather than a credential problem.

What came back afterwards is a different system.

### Auth moved to ForgeRock/PingAM OIDC

| | before | after |
|---|---|---|
| Credential endpoint | `webauth…/webservices/api/WebUser/Login` (flat JSON) | `customerlogin.southernco.com/am/json/realms/root/realms/alpha/authenticate` (ForgeRock `callbacks[]`) |
| Service selector | — | `?authIndexType=service&authIndexValue=occSecureLogin` |
| Token exchange | `webauth…/SPA/Navigation` → scrape `ScWebToken` from a hidden input | `/am/oauth2/authorize`, OIDC auth-code + **PKCE S256**, `client_id=webauthclient`, `response_mode=form_post` |
| Landing | `POST customerservice2…/Account/LoginComplete` | same, but reached via `webauth…/SPA/signin-forgerock-oidc-authcode` → `/SPA/ExternalAuthentication/forgerock-oidc-authcode` |

`/Account/LoginValidated/JwtToken` still exists and still answers `200`, with
`{"StatusCode":200,"Message":"Successfully retrieved jwtToken.","Data":null}`. **The step-4
failure is a symptom, not the bug** — the request is fine, the session behind it isn't. Patching
step 4 is wasted effort.

### The usage API moved too

`customerservice2api…/MPUData` is gone. Hourly data now comes from:

```
GET https://occmypowerusageapi.southerncompany.com
    /api/v1/MyPowerUsage/UsageGraphData/{serviceAgreementId}/Hourly
    ?accountId=…&personId=…&operatingCompany=GPC
    &startDate=MM/DD/YYYY&endDate=MM/DD/YYYY
    &servicePointId=…&premiseId=…
    &billFactorCode=null&intervalBehavior=Automatic
```

`Daily` and `Monthly` are sibling routes on the same path. It needs four opaque ~134-character
identifiers — `serviceAgreementId`, `personId`, `servicePointId`, `premiseId` — sourced from
`occaccountapi…/api/v1/Accounts/{accountId}/Summary` and `occpersonapi…/api/v1/person/{personId}`.
Related hosts in the same family: `occbillingapi`, `occcustomerserviceapi`, `occoutageapi`,
`occpaymentapi`. There is also a bulk-export path hinted at by
`occcustomerserviceapi…/api/v1/Utilities/getRegistryValue?key=HOURLY_BULK_EXPORT_DAYS`, which may
be cleaner than reading the graph endpoint.

**All three endpoint quirks below survived the migration unchanged.**

### How this was diagnosed, and one security warning

The scripted diagnostic in `tools/` was actively misleading here — see the warning in its
docstring. What actually worked was capturing a **browser HAR** of a real manual login and diffing
it against what the integration sends.

> ⚠️ **Do not treat a HAR of a login as safe, even a "sanitized" one.** Chrome's
> *Save as HAR (sanitized)* strips cookies, auth headers, and the `"password"` JSON key — but it
> does **not** understand ForgeRock's `callbacks[]` array, so the account password was written to
> the "sanitized" file in plaintext anyway. Parse HARs with a redacting script rather than reading
> them raw, delete them afterwards, and rotate any password one has touched.

## Endpoint quirks (do not "fix" these)

Verified live 2026-08-06/07. Each of these silently produces `HasData=false`:

1. **`EndDate` is exclusive.** `StartDate == EndDate` returns nothing. Always pass last-wanted-day + 1.
2. **No time component on dates.** `MM/DD/YYYY` works; `"MM/DD/YYYY 11:59:59 PM"` fails even with the +1.
3. **`intervalBehavior` must be `Automatic`** (or `Interval`). `Hourly` returns null on the `/Hourly` route.

And on bot detection: a *successful* response contains `_Incapsula_Resource`, `/_Incapsula_`, and
sometimes `reese84` — those are the Imperva client SDK, present whether or not you're blocked.
Southern Company's `/SPA/Navigation` step also legitimately returns a `<title>Loading</title>`
shell that carries the token. Matching on any of those blocks working logins. Only
`"Request unsuccessful"` / `"Incapsula incident"` mean actually blocked.

## Security

Credentials are stored by Home Assistant in `.storage/core.config_entries` as **unencrypted
JSON**, like every other HA integration. Anyone with file access to your HA config can read them.
Consider a Georgia Power password not reused anywhere else.

Automated access to the customer portal is very likely contrary to Southern Company's terms of
service. This is personal, low-volume, read-only access to your own account — but that tradeoff
is yours to make knowingly.

## Not supported

Alabama Power / Mississippi Power (the service-point lookup is Georgia-specific), Nicor Gas,
multiple accounts on one login, and sub-hourly data (Southern Company doesn't expose it).
