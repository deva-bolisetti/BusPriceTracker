"""Validate redbus_client against the live site and print a comparison summary.

Usage:
    python src/verify_redbus.py            # uses config.json
    python src/verify_redbus.py 2026-11-25 # override travel date
"""

import json
import sys
from pathlib import Path

import redbus_client

REPO_ROOT = Path(__file__).resolve().parents[1]


def load_config():
    """Load config.json, tolerating the // comments used to document allowed values."""
    return redbus_client.load_jsonc(str(REPO_ROOT / "config.json"), {})


def tier_price(tiers, tier):
    """Read the fare out of the tier -> list-of-dicts shape the client emits."""
    entries = (tiers or {}).get(tier)
    if isinstance(entries, list):
        entries = entries[0] if entries else None
    if isinstance(entries, dict):
        return entries.get("originalPrice")
    return entries


def main() -> int:
    config = load_config()
    date = sys.argv[1] if len(sys.argv) > 1 else config.get("dateOfJourney") or config.get("date")

    print(f"Route: {config['source']} -> {config['destination']}  date={date}\n")

    try:
        buses = redbus_client.fetch_buses(
            source=config["source"],
            destination=config["destination"],
            date_of_journey=date,
        )
    except Exception as error:
        print(f"\nFETCH FAILED: {type(error).__name__}: {error}")
        return 1

    if not buses:
        print("\nNo buses returned.")
        return 1

    print(f"\n{len(buses)} buses retrieved.\n")
    print(f"{'Operator':28} {'BusType':34} {'Dep':16} {'AC':4} {'Slp':4} {'Tiers'}")
    print("-" * 118)
    for bus in buses:
        tiers = ",".join(
            f"{tier}:{tier_price(bus.get('fareDetailsBySeatType'), tier)}"
            for tier in (bus.get("fareDetailsBySeatType") or {})
        )
        print(
            f"{(bus.get('travelsName') or '')[:27]:28} "
            f"{(bus.get('busType') or '')[:33]:34} "
            f"{(bus.get('departureTime') or '')[:15]:16} "
            f"{str(bus.get('isAc')):4} {str(bus.get('isSleeper')):4} {tiers}"
        )

    ac_sleeper_single = [
        b for b in buses
        if b.get("isAc") and "SINGLE_SLEEPER" in (b.get("fareDetailsBySeatType") or {})
    ]
    print(f"\nAC buses with SINGLE_SLEEPER tier: {len(ac_sleeper_single)}")
    for bus in ac_sleeper_single:
        price = tier_price(bus["fareDetailsBySeatType"], "SINGLE_SLEEPER")
        print(f"  {bus['travelsName']} @ {bus['departureTime']} -> Rs.{price}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
