# BusPriceTracker

Track bus fares for a configurable trip and get a push notification on your phone when a matching fare drops by at least the amount you choose.

Fare data comes straight from redBus's public JSON endpoints — **no API key, no paid service, no scraping proxy**. It runs entirely on GitHub Actions' free tier, so there is nothing to host and nothing to pay for.

Trip settings live in `config.json`, so you can change them from the GitHub mobile app without touching Python.

## How it works

```
GitHub Actions (every 15 min)
   → read config.json          which trip to track
   → fetch fares from redBus   free JSON API
   → compare with data/history.json   the previous snapshot
   → if a fare dropped ≥ your threshold → ntfy push to your phone
   → commit data/history.json    becomes the next baseline
```

- The first run for a trip saves a baseline in `data/history.json`; comparison starts on the run after that.
- A run with no qualifying drop sends **no notification** — you only get pushed when something actually drops.
- Changing the source, destination, travel date, AC preference or seat preference changes the trip *signature*, so the old baseline is not compared against the new trip. The next successful run establishes a fresh baseline.
- Changing only `min_drop_amount` keeps your baseline intact.

All fare comparison is deterministic Python. There is no AI model in the loop.

## Quick start

1. **Fork or clone** this repository.
2. **Edit `config.json`** for your trip:

   ```json
   {
     "active": true,
     "source": "Hyderabad",
     "destination": "Tuni",
     "date": "2026-11-22",
     "bus_ac_type": "AC",
     "seat_type": "Single Sleeper",
     "min_drop_amount": 50
   }
   ```

3. **Add the `NTFY_TOPIC` secret** — see below.
4. **Run the workflow once** from the Actions tab to record a baseline.
5. Install the ntfy app, subscribe to your topic, and wait for a drop.

## Configure a trip

| Field | Description |
| --- | --- |
| `active` | `false` skips tracking and makes no API calls, while leaving the schedule enabled. |
| `source` | Departure city name as redBus spells it. |
| `destination` | Arrival city name as redBus spells it. |
| `date` | Travel date, `YYYY-MM-DD`. A past date returns no buses, so the run no-ops. |
| `bus_ac_type` | `AC`, `Non-AC`, or `Any`. |
| `seat_type` | `Single Sleeper`, `Double Sleeper`, `Seater`, or `Any`. |
| `min_drop_amount` | Minimum fare decrease, in rupees, required before you are notified. |
| `fromCity` / `toCity` | Optional. Pin redBus city IDs to skip the name lookup. |

Commit the file when you change it; the next scheduled run picks it up. You can also trigger a run manually from the Actions tab.

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
| `tracker-direct.yml` | every 15 minutes, or manual | redBus JSON API | The scheduled path. Free. |
| `tracker-apify.yml` | manual only | Apify actor | Optional fallback. Paid, and needs `APIFY_TOKEN` + `APIFY_ACTOR_ID`. |

`tracker-apify.yml` is deliberately **not** scheduled: it bills per run, and its credentials
expire. Enable it only if redBus changes its API and you want a working fallback while you fix
`src/redbus_client.py`. Never run both on a schedule — they both write `data/history.json` and would
race each other.

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
├── src/
│   ├── redbus_client.py        free redBus JSON client + normaliser
│   ├── tracker_direct.py       scheduled runner: fetch → compare → notify
│   ├── tracker_apify.py        optional Apify-backed runner
│   └── verify_redbus.py        manual inspector for live fares
└── data/
    ├── history.json            the baseline snapshot, committed each run
    └── cities.json             cached redBus city IDs
```

| File | What it does |
| --- | --- |
| `config.json` | The trip you are tracking. The only file you normally edit. |
| `src/redbus_client.py` | Free redBus JSON client; normalises results to the stored schema. |
| `src/tracker_direct.py` | Scheduled runner: fetch → compare → notify. |
| `src/tracker_apify.py` | Optional Apify-backed runner; same comparison logic. |
| `src/verify_redbus.py` | Manual inspector for live fares and the endpoint contract. |
| `data/cities.json` | Cached redBus city IDs, so lookups happen once. |
| `data/history.json` | The baseline snapshot, committed by the workflow each run. |

`config.json` stays at the repository root on purpose: it is the one file you edit from the
GitHub mobile app, so it should be the easiest thing to find.

## Notes and caveats

- `data/history.json` is written by the workflow. Don't hand-edit it during normal operation; a scheduled run will overwrite you. Editing a price in it is a handy way to test alerting.
- Large diffs in `data/history.json` are normal — redBus reorders results between runs even when prices are unchanged.
- This relies on redBus's internal, undocumented endpoints. They can change without notice. If the tracker stops finding buses, run `src/verify_redbus.py` to see what broke.
- Scheduled Actions are not guaranteed to start exactly on time, and can be delayed under load.
- Intended for personal, low-frequency tracking. Be considerate: don't drop the interval to seconds.

## License

No license is specified yet. All rights reserved unless the repository owner adds one.
