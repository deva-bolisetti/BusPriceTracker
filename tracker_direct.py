"""RedBus-direct tracker: fetches fares for free directly from redBus.

Uses the same config.json, history.json and ntfy alerting as tracker.py, but
replaces the Apify fetch and the Gemini analysis with deterministic Python.
No paid API is involved anywhere in this runner.

Usage:
    python tracker_direct.py
"""

import json
import os

import redbus_client

NTFY_TOPIC = os.environ.get("NTFY_TOPIC")

CONFIG_FILE = "config.json"
HISTORY_FILE = "history.json"

# Gemini is no longer called; alert text is formatted locally by format_alert_message().
# Kept here only to document which models the old analysis used, in case AI phrasing is
# ever reintroduced (see the commented-out analyze_with_ai block further down).
# AI_MODELS = ("gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-3.1-flash-lite")


def load_json(filepath, default):
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def scraper_input(config):
    """Mirror tracker.py so the trip signature stays byte-compatible."""
    return {
        "source": config["source"],
        "destination": config["destination"],
        "dateOfJourney": config.get("dateOfJourney", config.get("date", "")),
        "pageSize": config.get("pageSize", 20),
        "maxItems": config.get("maxItems", 100),
        "country": config.get("country", "india"),
        "proxyConfiguration": config.get("proxyConfiguration", {"useApifyProxy": True}),
    }


def fetch_latest_buses(config):
    """Drop-in replacement for the Apify fetch step."""
    journey_date = config.get("dateOfJourney", config.get("date", ""))
    return redbus_client.fetch_buses(
        source=config["source"],
        destination=config["destination"],
        date_of_journey=journey_date,
        from_city_id=config.get("fromCity"),
        to_city_id=config.get("toCity"),
    )


SEAT_TIER_KEYS = {
    "single sleeper": "SINGLE_SLEEPER",
    "double sleeper": "SLEEPER",
    "sleeper": "SLEEPER",
    "seater": "SEATER",
}


def _tier_price(tiers, tier_key):
    """Read originalPrice out of the list-per-tier shape Apify also used."""
    entries = (tiers or {}).get(tier_key)
    if isinstance(entries, list) and entries:
        entry = entries[0]
    else:
        entry = entries
    if isinstance(entry, dict):
        return entry.get("originalPrice")
    if isinstance(entry, (int, float)):
        return entry
    return None


def _tier_count(tiers, tier_key):
    entries = (tiers or {}).get(tier_key)
    if isinstance(entries, list) and entries and isinstance(entries[0], dict):
        return entries[0].get("count")
    return None


def _bus_key(bus):
    """Identify one inventory across snapshots.

    travelsName/busType/departureTime alone collide, and the collisions are real:
    one operator can sell two separate inventories under the same name, bus type and
    departure (e.g. VARAHI TRAVELS 20:30 at Rs.950 and Rs.3444). serviceId is the
    stable discriminator and stayed identical for every bus across repeated fetches,
    where routeId is re-issued frequently and must not be used for matching.
    """
    return (
        bus.get("travelsName"),
        bus.get("busType"),
        bus.get("departureTime"),
        bus.get("serviceId") or bus.get("routeId"),
    )


def find_fare_drops(config, previous_buses, current_buses):
    """Deterministically narrow buses down to genuine tier-level drops.
    Returns a list of candidate drop dicts, sorted by drop amount descending.
    Each candidate dict contains:
    - operator: Bus operator name
    - busType: Bus type
    - departureTime: Departure time
    - seatTier: Seat tier
    - oldFare: Previous fare
    - newFare: New fare
    - drop: Fare drop amount
    - seatsBefore: Number of seats available before the drop
    - seatsAfter: Number of seats available after the drop
    """
    min_drop = int(config.get("min_drop_amount", 50))
    ac_pref = str(config.get("bus_ac_type", "Any")).strip().lower()
    seat_pref = str(config.get("seat_type", "Any")).strip().lower()

    if seat_pref in SEAT_TIER_KEYS:
        tier_keys = [SEAT_TIER_KEYS[seat_pref]]
    else:
        tier_keys = ["SEATER", "SLEEPER", "SINGLE_SLEEPER"]

    candidates = []
    previous_by_key = {}
    for bus in previous_buses:
        previous_by_key.setdefault(_bus_key(bus), bus)

    for bus in current_buses:
        if ac_pref == "ac" and not bus.get("isAc"):
            continue
        if ac_pref in ("non-ac", "nonac", "non ac") and bus.get("isAc"):
            continue

        old_bus = previous_by_key.get(_bus_key(bus))
        if old_bus is None:
            # A bus absent from history.json has no baseline fare, so it can
            # never produce a drop. Skip it rather than reporting it.
            continue

        for tier in tier_keys:
            old_price = _tier_price(old_bus.get("fareDetailsBySeatType"), tier)
            new_price = _tier_price(bus.get("fareDetailsBySeatType"), tier)
            if old_price is None or new_price is None:
                continue

            drop = old_price - new_price
            if drop < min_drop:
                continue

            candidates.append(
                {
                    "operator": bus.get("travelsName"),
                    "busType": bus.get("busType"),
                    "departureTime": bus.get("departureTime"),
                    "seatTier": tier,
                    "oldFare": old_price,
                    "newFare": new_price,
                    "drop": drop,
                    "seatsBefore": _tier_count(old_bus.get("fareDetailsBySeatType"), tier),
                    "seatsAfter": _tier_count(bus.get("fareDetailsBySeatType"), tier),
                }
            )

    candidates.sort(key=lambda c: -c["drop"])
    return candidates


def format_alert_message(config, candidates):
    """Build the ntfy message from deterministic drop data.

    This replaces what Gemini used to do. Gemini's only roles were: reject sold-out
    tiers, reply NO_DROP when nothing qualified, and phrase the alert. All three are
    deterministic:

      - Sold-out tiers never appear in fareDetailsBySeatType, so there is nothing to
        reject. (Verified: 0 of 164 buses in a live baseline had count == 0.)
      - "Nothing qualified" is simply an empty candidate list.
      - Phrasing is a template.

    Gemini could never find a drop the filter missed, since it only saw the shortlist,
    so removing it loses no detection ability.
    """
    if not candidates:
        return None

    min_drop = int(config.get("min_drop_amount", 50))
    source = config.get("source", "?")
    destination = config.get("destination", "?")
    journey_date = config.get("dateOfJourney", config.get("date", "")) or "unspecified"

    # Match the old AI output shape so alert history stays comparable.
    lines = [f"Bus fare drop: {source} to {destination} on {journey_date}"]
    if len(candidates) > 1:
        lines[0] += f" ({len(candidates)} drops, min Rs.{min_drop})"

    tier_labels = {
        "SINGLE_SLEEPER": "Single Sleeper",
        "SLEEPER": "Double Sleeper",
        "SEATER": "Seater",
    }

    for candidate in candidates:
        tier = tier_labels.get(candidate["seatTier"], candidate["seatTier"])
        lines.append("")
        lines.append(f"- {candidate['operator']} ({candidate['busType']})")
        lines.append(f"  Departure: {candidate['departureTime']}")
        lines.append(
            f"  {tier}: Rs.{candidate['oldFare']} -> Rs.{candidate['newFare']} "
            f"(down Rs.{candidate['drop']})"
        )
        if candidate.get("seatsAfter") is not None:
            lines.append(f"  Seats available: {candidate['seatsAfter']}")

    return "\n".join(lines)


# --- Gemini analysis, disabled -------------------------------------------------
# Replaced by find_fare_drops() + format_alert_message(). Left commented as a
# reference for the prompt shape and model fallback, and because it documents why
# sending full snapshots to a model was wrong (269k tokens, answered NO_DROP while
# a real Rs.1000 drop was present).
#
# GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
#
# def analyze_with_ai(config, previous_buses, current_buses):
#     from google import genai
#
#     client = genai.Client(api_key=GEMINI_API_KEY)
#     journey_date = config.get("dateOfJourney", config.get("date", "")) or "unspecified"
#     bus_ac_type = config.get("bus_ac_type", "Any")
#     seat_type = config.get("seat_type", "Any")
#     min_drop_amount = config.get("min_drop_amount", 50)
#     candidates = find_fare_drops(config, previous_buses, current_buses)
#
#     if not candidates:
#         print(f"Deterministic filter found no tier drop >= Rs.{min_drop_amount} "
#               f"for {bus_ac_type}/{seat_type}.")
#         return "NO_DROP"
#
#     print(f"Deterministic filter found {len(candidates)} candidate drop(s); "
#           f"sending {len(json.dumps(candidates))} chars to the model.")
#
#     prompt = f"""
#     You are a bus fare analyst tracking {config['source']} to {config['destination']} on {journey_date}.
#
#     A deterministic script already matched buses across both snapshots and filtered for
#     fare drops of at least Rs.{min_drop_amount} on the requested seat tier. Your only job is
#     to discard false positives and write the alert.
#
#     STRICT FILTERS already applied: Bus AC Preference = {bus_ac_type}; Seat Type = {seat_type}.
#
#     REJECT a candidate only when:
#     - seatsAfter is 0, meaning the tier is sold out and the price is not bookable.
#     - the drop is on a different seat tier than requested.
#
#     Otherwise ACCEPT it.
#
#     Reply with exact text NO_DROP only if every candidate is rejected.
#     Otherwise reply with a concise alert listing, for each accepted candidate:
#       - Operator & Bus Type
#       - Departure Time
#       - Seat Tier & Old Fare -> New Fare
#
#     CANDIDATE_DROPS:
#     {json.dumps(candidates, indent=2)}
#     """
#
#     for model in AI_MODELS:
#         try:
#             print("Trying with model: ", model)
#             response = client.models.generate_content(model=model, contents=prompt)
#             verdict = (response.text or "").strip()
#             print("Response from AI: ", verdict)
#             if not verdict:
#                 raise ValueError("The model returned an empty response.")
#             return verdict
#         except Exception as error:
#             print(f"AI model {model} failed: {error}")
#             continue
#
#     return None


def send_android_alert(config, message):
    import requests

    ntfy_url = NTFY_TOPIC if NTFY_TOPIC.startswith("http") else f"https://ntfy.sh/{NTFY_TOPIC}"
    seat_type = config.get("seat_type", "Any")
    requests.post(
        ntfy_url,
        data=message.encode("utf-8"),
        headers={
            "Title": f"Bus Drop ({seat_type}): {config['source']} to {config['destination']}",
            "Priority": "high",
            "Tags": "bus,money_with_wings",
        },
        timeout=15,
    )


def main():
    config = load_json(CONFIG_FILE, {})
    if not config.get("active", True):
        print("Tracker is paused in config.json. Exiting.")
        return

    missing = [
        name
        for name, value in {
            "NTFY_TOPIC": NTFY_TOPIC,
        }.items()
        if not value
    ]
    if missing:
        print(
            "Skipping tracker run because required secret(s) are missing: "
            + ", ".join(missing)
            + ". Add them in GitHub repository secrets to enable live tracking."
        )
        return

    if not config.get("source") or not config.get("destination"):
        raise ValueError("config.json must include non-empty source and destination values.")

    actor_input = scraper_input(config)
    search_signature = json.dumps(
        {
            **actor_input,
            "bus_ac_type": config.get("bus_ac_type", "Any"),
            "seat_type": config.get("seat_type", "Any"),
        },
        sort_keys=True,
    )

    history = load_json(HISTORY_FILE, {})
    previous_buses = history.get("buses", []) if history.get("signature") == search_signature else []

    try:
        current_buses = fetch_latest_buses(config)
    except redbus_client.RedbusError as error:
        print(f"redBus fetch failed, leaving history untouched: {error}")
        return

    if not current_buses:
        print("redBus returned no buses (past date, or no inventory). Leaving history untouched.")
        return

    if previous_buses:
        candidates = find_fare_drops(config, previous_buses, current_buses)
        message = format_alert_message(config, candidates)

        if message:
            print(f"Found {len(candidates)} drop(s):")
            for candidate in candidates:
                print(f"  {candidate['operator']} | {candidate['departureTime']} | "
                      f"{candidate['seatTier']} | {candidate['oldFare']} -> {candidate['newFare']}")
            send_android_alert(config, message)
            print("Alert sent:\n" + message)
        else:
            # No notification on a quiet run: the cron fires every 15 minutes and a
            # "no drop" push each time is noise.
            min_drop = config.get("min_drop_amount", 50)
            print(f"No fare drop of at least Rs.{min_drop} detected. No alert sent.")
    else:
        print("New trip baseline saved. Will compare on the next run.")

    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump({"signature": search_signature, "buses": current_buses}, f, indent=2)


if __name__ == "__main__":
    main()
