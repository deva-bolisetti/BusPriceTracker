# BusPriceTracker

Track bus fares for a configurable trip and get a push notification on your phone when a matching fare drops by at least the amount you choose.

Fare data comes straight from redBus's public JSON endpoints — **no API key, no paid service, no scraping proxy**. It runs entirely on GitHub Actions' free tier, so there is nothing to host and nothing to pay for.

Trip settings live in `config.json`, so you can change them from the GitHub mobile app without touching Python. That file accepts `//` comments, so the allowed values are documented inline.

## How it works

```
External scheduler (every 15 min)
   → workflow_dispatch on tracker-direct.yml
   → read config.json                which trip to track
   → fetch fares from redBus         free JSON API
   → compare with data/history.json  the previous snapshot
   → if a fare dropped ≥ your threshold → ntfy push to your phone
   → commit data/history.json        becomes the next baseline
```

GitHub's own `schedule` event is best-effort and drops most runs on public repositories, so the
cron is driven externally instead — see [Scheduling](#scheduling).

- The first run for a trip saves a baseline in `data/history.json`; comparison starts on the run after that.
- A run with no qualifying drop sends **no notification** — you only get pushed when something actually drops.
- Changing the source, destination, travel date or pinned city IDs changes the trip, so the old baseline is not compared against it. A fresh baseline is saved and a low-priority push tells you tracking restarted.
- Changing `seat_type`, `bus_ac_type` or `min_drop_amount` keeps your baseline intact — those only filter the comparison.

All fare comparison is deterministic Python. There is no AI model in the loop.

## Quick start

1. **Fork or clone** this repository.
2. **Edit `config.json`** for your trip. The file documents every allowed value inline:

   ```jsonc
   {
     "active": true,
     "source": "Hyderabad",
     "destination": "Tuni",
     "date": "2026-11-22",
     "bus_ac_type": "AC",       // "AC" | "Non-AC" | "Any"
     "seat_type": "Sleeper",    // "Sleeper" | "Single Sleeper" | "Double Sleeper" | "Seater" | "Any"
     "min_drop_amount": 50
   }
   ```

3. **Add the `NTFY_TOPIC` secret** — see below.
4. **Run the workflow once** from the Actions tab to record a baseline.
5. **Set up the external scheduler** — see [Scheduling](#scheduling).
6. Install the ntfy app, subscribe to your topic, and wait for a drop.

## Configure a trip

| Field | Description |
| --- | --- |
| `active` | `false` skips tracking and makes no API calls, while leaving the schedule enabled. |
| `source` | Departure city name as redBus spells it. |
| `destination` | Arrival city name as redBus spells it. |
| `date` | Travel date, `YYYY-MM-DD`. A past date returns no buses, so the run no-ops. |
| `bus_ac_type` | `AC`, `Non-AC`, or `Any`. |
| `seat_type` | `Sleeper`, `Single Sleeper`, `Double Sleeper`, `Seater`, or `Any`. Combine with commas. |
| `min_drop_amount` | Minimum fare decrease, in rupees, required before you are notified. |
| `fromCity` / `toCity` | Optional. Pin redBus city IDs to skip the name lookup. |

`config.json` is parsed leniently: `//` and `/* */` comments are stripped and trailing
commas are tolerated, so you can annotate the file freely. It is therefore not strict JSON —
if another tool reads it directly, strip the comments first.

A `seat_type` typo does not fail loudly: anything unrecognised is treated as `Any`.

### Seat types

redBus prices single and double berths as **separate tiers**, so they are separate
values here too:

| `seat_type` | Tiers compared |
| --- | --- |
| `Sleeper` | `SLEEPER` + `SINGLE_SLEEPER` — every sleeper berth, single or double |
| `Single Sleeper` | `SINGLE_SLEEPER` only |
| `Double Sleeper` | `SLEEPER` only |
| `Seater` | `SEATER` only |
| `Any` | all three |

`Sleeper` is the usual choice: a bus that sells single berths also sells doubles, so
one setting covers both and reports each tier's drop separately. Combine values with
commas if you want an explicit set, e.g. `"Single Sleeper, Seater"`.

### What resets the baseline

`data/history.json` stores the full inventory with every fare tier, so the seat and AC
settings are only *filters* applied at comparison time — changing them reuses the
existing baseline and compares immediately:

| Change | Baseline |
| --- | --- |
| `seat_type`, `bus_ac_type`, `min_drop_amount` | **kept** — comparison continues on the next run |
| `source`, `destination`, `date`, `fromCity`, `toCity` | **reset** — a new baseline is saved and a low-priority "Tracker re-baselined" push explains what changed |

A reset never alerts on a drop for the run that resets, because there is nothing to
compare against yet. Comparison resumes 15 minutes later.

Commit the file when you change it; the next run picks it up. You can also trigger a run manually from the Actions tab.

## Notifications

1. Install the [ntfy app](https://ntfy.sh/) on your phone.
2. Subscribe to the same topic you put in the `NTFY_TOPIC` secret.
3. Keep the topic private — anyone who knows a public topic name can publish to it.

A sample alert:

```
Bus fare drop: Hyderabad to Tuni on 2026-11-22

- KALOSONA DREAMLINE (VE A/C Seater / Sleeper (2+1))
  Departure: 2026-11-22 12:45:00
  Single Sleeper: Rs.3450 -> Rs.2750 (down Rs.700)
  Seats available: 3
```

## Required secret

Only one is needed for the free path.

In **Settings → Secrets and variables → Actions**, add:

| Secret | Purpose |
| --- | --- |
| `NTFY_TOPIC` | ntfy topic used for phone notifications |

Never put credentials in `config.json` or commit them.

## Workflows

| Workflow | Trigger | Data source | Notes |
| --- | --- | --- | --- |
| `tracker-direct.yml` | `workflow_dispatch` only | redBus JSON API | The live path. Free. |
| `tracker-loop.yml` | `workflow_dispatch` only | redBus JSON API | Manual backup: repeats `tracker-direct` inside one job. |
| `tracker-apify.yml` | `workflow_dispatch` only | Apify actor | Optional fallback. Paid, and needs `APIFY_TOKEN` + `APIFY_ACTOR_ID`. |

None of them carry a `schedule:` trigger. `tracker-direct.yml` is driven by the external
scheduler below; the other two are run by hand.

`tracker-loop.yml` takes two inputs: `hours` (default `5.75`, the most GitHub allows in one job)
and `interval_minutes` (default `15`). It is for covering a gap when the external cron is
unavailable, not for routine use — a loop burns roughly 44,400 Actions minutes a month if left
running, against about 2,880 for a 15-minute dispatch.

`tracker-apify.yml` is deliberately **not** scheduled: it bills per run, and its credentials
expire. Enable it only if redBus changes its API and you want a working fallback while you fix
`src/redbus_client.py`.

Never run two of these at the same time. They all write `data/history.json`, so overlapping runs
race each other and can push the same drop twice.

## Scheduling

GitHub's `schedule:` trigger is best-effort and, on public repositories, drops most runs — which
is why the workflow has no cron of its own. A free external scheduler calls the dispatch API
instead, which GitHub always honours.

1. Create a **fine-grained personal access token** scoped to this repository with
   *Actions: Read and write* and *Metadata: Read-only*.
2. At [cron-job.org](https://cron-job.org), create a job:
   - **URL**: `https://api.github.com/repos/<owner>/<repo>/actions/workflows/tracker-direct.yml/dispatches`
   - **Method**: `POST`
   - **Schedule**: custom `*/15 * * * *` — the form defaults to hourly, so change it.
   - **Headers** (one row each):
     | Header | Value |
     | --- | --- |
     | `Authorization` | `Bearer <your token>` |
     | `Accept` | `application/vnd.github+json` |
     | `X-GitHub-Api-Version` | `2022-11-28` |
     | `Content-Type` | `application/json` |
   - **Body**: `{"ref":"main"}` — use your working branch while you are still testing, then
     switch to `main` after merging.
3. Expect **HTTP 204** with an empty body. A `404` means the ref or workflow filename is wrong;
   a `401`/`403` means the token is missing a permission.
4. Watch the Actions tab for the first few runs and confirm each one commits `data/history.json`.

Treat the token like a password: it can start workflows in your repository. Rotate it if it is
ever pasted anywhere it could be read.

## Run it locally

```bash
pip install -r requirements.txt

# See live fares for the configured trip (no key needed)
python src/verify_redbus.py

# Do a real tracking run (needs an ntfy topic to actually notify)
NTFY_TOPIC=your-topic python src/tracker_direct.py
```

On Windows PowerShell, set the variable with `$env:NTFY_TOPIC="your-topic"`.

Both scripts locate `config.json` and `data/` relative to the repository root, so they work
from any working directory.

## Project layout

```
.
├── config.json                 the trip you are tracking (the file you edit)
├── requirements.txt
├── README.md
├── .github/workflows/
│   ├── tracker-direct.yml      the live path (dispatch only)
│   ├── tracker-loop.yml        manual loop backup
│   └── tracker-apify.yml       optional Apify fallback
├── src/
│   ├── redbus_client.py        free redBus JSON client + normaliser
│   ├── tracker_direct.py       runner: fetch → compare → notify
│   ├── tracker_apify.py        optional Apify-backed runner
│   └── verify_redbus.py        manual inspector for live fares
└── data/
    ├── history.json            the baseline snapshot, committed each run
    └── cities.json             cached redBus city IDs
```

| File | What it does |
| --- | --- |
| `config.json` | The trip you are tracking. The only file you normally edit. |
| `src/redbus_client.py` | Free redBus JSON client; normalises results to the stored schema; loads the JSONC config. |
| `src/tracker_direct.py` | Runner: fetch → compare → notify. |
| `src/tracker_apify.py` | Optional Apify-backed runner. Not kept in sync with the direct runner. |
| `src/verify_redbus.py` | Manual inspector for live fares and the endpoint contract. |
| `data/cities.json` | Cached redBus city IDs, so lookups happen once. |
| `data/history.json` | The baseline snapshot, committed by the workflow each run. |

`config.json` stays at the repository root on purpose: it is the one file you edit from the
GitHub mobile app, so it should be the easiest thing to find.

## Notes and caveats

- `data/history.json` is written by the workflow. Don't hand-edit it during normal operation; a scheduled run will overwrite you. Editing a price in it is a handy way to test alerting.
- Large diffs in `data/history.json` are normal — redBus reorders results between runs even when prices are unchanged.
- This relies on redBus's internal, undocumented endpoints. They can change without notice. If the tracker stops finding buses, run `src/verify_redbus.py` to see what broke.
- Dispatched runs still queue behind GitHub's own capacity, so they can start late under load.
- Intended for personal, low-frequency tracking. Be considerate: don't drop the interval to seconds.

## License

No license is specified yet. All rights reserved unless the repository owner adds one.
