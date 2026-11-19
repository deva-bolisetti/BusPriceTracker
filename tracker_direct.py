"""RedBus-direct tracker: identical behaviour to tracker.py, data from redbus_client.

tracker.py (Apify) on main is untouched. This runner uses the same config.json,
history.json, Gemini analysis and ntfy alerting, but fetches fares for free
directly from redBus.

Usage:
    python tracker_direct.py
"""

import json
import os

import redbus_client

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")

CONFIG_FILE = "config.json"
HISTORY_FILE = "history.json"

# Try the lowest-cost model first, then fall back to the existing general-purpose model.
AI_MODELS = ("gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-3.1-flash-lite")


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
    return (bus.get("travelsName"), bus.get("busType"), bus.get("departureTime"))


def find_fare_drops(config, previous_buses, current_buses):
    """Deterministically narrow 160+ buses down to genuine tier-level drops.

    The prompt previously carried both full snapshots (~269k tokens). That is far
    beyond what a small model reliably reads, and it answered NO_DROP while a real
    Rs.1000 drop sat in the payload. Filtering in Python first means the model only
    ever judges a handful of candidates.
    """
    min_drop = int(config.get("min_drop_amount", 50))
    ac_pref = str(config.get("bus_ac_type", "Any")).strip().lower()
    seat_pref = str(config.get("seat_type", "Any")).strip().lower()

    if seat_pref in SEAT_TIER_KEYS:
        tier_keys = [SEAT_TIER_KEYS[seat_pref]]
    else:
        tier_keys = ["SEATER", "SLEEPER", "SINGLE_SLEEPER"]

    previous_by_key = {}
    for bus in previous_buses:
        previous_by_key.setdefault(_bus_key(bus), bus)

    candidates = []
    for bus in current_buses:
        key = _bus_key(bus)
        old_bus = previous_by_key.get(key)
        if old_bus is None:
            continue

        if ac_pref == "ac" and not bus.get("isAc"):
            continue
        if ac_pref in ("non-ac", "nonac", "non ac") and bus.get("isAc"):
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


def analyze_with_ai(config, previous_buses, current_buses):
    from google import genai

    client = genai.Client(api_key=GEMINI_API_KEY)
    journey_date = config.get("dateOfJourney", config.get("date", "")) or "unspecified"
    bus_ac_type = config.get("bus_ac_type", "Any")
    seat_type = config.get("seat_type", "Any")
    min_drop_amount = config.get("min_drop_amount", 50)
    candidates = find_fare_drops(config, previous_buses, current_buses)

    if not candidates:
        print(f"Deterministic filter found no tier drop >= Rs.{min_drop_amount} "
              f"for {bus_ac_type}/{seat_type}.")
        return "NO_DROP"

    print(f"Deterministic filter found {len(candidates)} candidate drop(s); "
          f"sending {len(json.dumps(candidates))} chars to the model "
          f"(versus {len(json.dumps(previous_buses)) + len(json.dumps(current_buses))} unfiltered).")

    prompt = f"""
    You are a bus fare analyst tracking {config['source']} to {config['destination']} on {journey_date}.

    A deterministic script already matched buses across both snapshots and filtered for
    fare drops of at least ₹{min_drop_amount} on the requested seat tier. Your only job is
    to discard false positives and write the alert.

    STRICT FILTERS already applied: Bus AC Preference = {bus_ac_type}; Seat Type = {seat_type}.

    REJECT a candidate only when:
    - seatsAfter is 0, meaning the tier is sold out and the price is not bookable.
    - the drop is on a different seat tier than requested.

    Otherwise ACCEPT it. The candidates below are already tier-matched, so a listed drop
    is a genuine drop for that tier.

    Reply with exact text NO_DROP only if every candidate is rejected.
    Otherwise reply with a concise alert listing, for each accepted candidate:
      - Operator & Bus Type
      - Departure Time
      - Seat Tier & Old Fare -> New Fare

    CANDIDATE_DROPS:
    {json.dumps(candidates, indent=2)}
    """

    for model in AI_MODELS:
        try:
            print("Trying with model: ", model)
            response = client.models.generate_content(
                model=model,
                contents=prompt,
            )
            verdict = (response.text or "").strip()
            usage = getattr(response, "usage_metadata", None)
            if usage is not None:
                print(f"  tokens: prompt={getattr(usage, 'prompt_token_count', '?')} "
                      f"total={getattr(usage, 'total_token_count', '?')}")
            print("Response from AI: ", verdict)
            if not verdict:
                raise ValueError("The model returned an empty response.")
            print(f"AI analysis completed with {model}.")
            return verdict
        except Exception as error:
            print(f"AI model {model} failed: {error}")
            continue

    print("AI analysis failed for all configured models; keeping the previous history for a retry.")
    return None


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
            "GEMINI_API_KEY": GEMINI_API_KEY,
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
        verdict = analyze_with_ai(config, previous_buses, current_buses)
        if verdict is None:
            print("No Verdict")
            send_android_alert(config, "verdict is NONE")
            return
        if verdict != "NO_DROP":
            send_android_alert(config, verdict)
            print("Alert sent:\n", verdict)
        else:
            print("No genuine price drop detected.")
            send_android_alert(config, verdict)
    else:
        print("New trip baseline saved. Will compare on the next run.")

    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump({"signature": search_signature, "buses": current_buses}, f, indent=2)


if __name__ == "__main__":
    main()
