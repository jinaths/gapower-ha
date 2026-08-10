# Georgia Power for Home Assistant

Pulls **hourly** electricity usage and cost from your Georgia Power (Southern Company) account
into the Home Assistant **Energy Dashboard**. Runs entirely inside HA — no external host, no
cron, no separate Python environment.

- Hourly kWh + cost, imported as long-term statistics
- ~365 days backfilled on first run, then incremental
- Native config flow, and a reauth prompt if the credentials stop working
- **Zero PyPI dependencies** — uses `aiohttp`, which HA already ships

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

### Staleness alarm

A broken scraper looks identical to "no new data." Watch the timestamp:

```yaml
alias: Georgia Power data stale
triggers:
  - trigger: template
    value_template: >
      {{ (now() - states('sensor.georgia_power_last_reading')|as_datetime).total_seconds() > 172800 }}
actions:
  - action: notify.persistent_notification
    data:
      message: Georgia Power usage data hasn't updated in over 48 hours.
```

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
  auth chain is updated — expect that once or twice a year.
- Bot protection (Imperva) can block requests from your IP for ~30 minutes. The integration backs
  off rather than hammering.

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
