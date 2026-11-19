import json
import os
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]

APIFY_TOKEN = os.environ.get("APIFY_TOKEN")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")
APIFY_API_URL = (os.environ.get("APIFY_API_URL") or "https://api.apify.com").rstrip("/")
APIFY_ACTOR_ID = os.environ.get("APIFY_ACTOR_ID")

CONFIG_FILE = REPO_ROOT / "config.json"
HISTORY_FILE = REPO_ROOT / "data" / "history.json"

# Gemini is no longer called; alert text is formatted locally by format_alert_message().
# The old model list and prompt are preserved in the commented-out block below.
# AI_MODELS = ("gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-3.1-flash-lite")


def load_json(filepath, default):
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def scraper_input(config):
    """Build the actor input, accepting both the scraper schema and old config keys."""
    return {
        "source": config["source"],
        "destination": config["destination"],
        "dateOfJourney": config.get("dateOfJourney", config.get("date", "")),
        "pageSize": config.get("pageSize", 20),
        "maxItems": config.get("maxItems", 100),
        "country": config.get("country", "india"),
        "proxyConfiguration": config.get("proxyConfiguration", {"useApifyProxy": True}),
    }


def fetch_latest_buses(actor_input):
    url = f"{APIFY_API_URL}/v2/acts/{APIFY_ACTOR_ID}/run-sync-get-dataset-items"

    response = requests.post(
        url,
        headers={"Authorization": f"Bearer {APIFY_TOKEN}"},
        json=actor_input,
        timeout=120,
    )
    response.raise_for_status()
    data = response.json()

    if isinstance(data, dict) and "items" in data:
        return data["items"]
    if isinstance(data, list):
        return data
    return data


SEAT_TIER_KEYS = {
    "single sleeper": "SINGLE_SLEEPER",
    "double sleeper": "SLEEPER",
    "sleeper": "SLEEPER",
    "seater": "SEATER",
}


def _tier_price(tiers, tier_key):
    """Read originalPrice out of the list-per-tier shape Apify returns."""
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

    Matching, tier selection and the drop threshold all happen here, so the result is
    reproducible and does not depend on a model reading two large JSON snapshots.
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
        old_bus = previous_by_key.get(_bus_key(bus))
        if old_bus is None:
            # A bus absent from history.json has no baseline fare, so it can never
            # produce a drop. Skip it rather than reporting it.
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


def format_alert_message(config, candidates):
    """Build the ntfy message from deterministic drop data.

    Replaces the Gemini call. The model only ever filtered sold-out tiers, decided
    NO_DROP, and phrased the alert; all three are deterministic. A tier only appears
    in fareDetailsBySeatType when it is bookable, so there is nothing to reject.
    """
    if not candidates:
        return None

    min_drop = int(config.get("min_drop_amount", 50))
    source = config.get("source", "?")
    destination = config.get("destination", "?")
    journey_date = config.get("dateOfJourney", config.get("date", "")) or "unspecified"

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
# Replaced by find_fare_drops() + format_alert_message(). Kept as a reference for
# the old prompt shape, and because it documents the failure that prompted the
# change: sending both full snapshots (~269k tokens) made the model answer NO_DROP
# while a real Rs.1000 drop was present in the payload.
#
# GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
#
# def analyze_with_ai(config, previous_buses, current_buses):
#     client = genai.Client(api_key=GEMINI_API_KEY)
#     journey_date = config.get("dateOfJourney", config.get("date", "")) or "unspecified"
#     bus_ac_type = config.get("bus_ac_type", "Any")
#     seat_type = config.get("seat_type", "Any")
#     min_drop_amount = config.get("min_drop_amount", 50)
#     prompt = f"""
#     You are a bus fare analyst tracking {config['source']} to {config['destination']} on {journey_date}.
#
#     STRICT FILTERS (Apply these first):
#     - Bus AC Preference: {bus_ac_type} (Only include buses matching this preference; if 'Any', include both AC and Non-AC).
#     - Seat Type Preference: {seat_type} (Only evaluate fares for this exact seat tier; if 'Any', evaluate all tiers separately).
#
#     COMPARISON RULES:
#     1. Match buses across PREVIOUS_FARES and CURRENT_FARES by Operator Name, Bus Type, and Departure Time.
#     2. Ignore cases where a lower price appears simply because a higher-priced tier sold out and only a cheaper tier remains.
#     3. Only flag a GENUINE price drop if the matching seat tier dropped in fare by at least Rs.{min_drop_amount}.
#     4. If there is NO genuine drop matching the filters, reply with exact text: NO_DROP
#     5. If there IS a genuine drop, reply with a concise alert showing:
#        - Operator & Bus Type
#        - Departure Time
#        - Seat Type & Old Fare -> New Fare
#
#     PREVIOUS_FARES:
#     {json.dumps(previous_buses)}
#
#     CURRENT_FARES:
#     {json.dumps(current_buses)}
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
            "APIFY_TOKEN": APIFY_TOKEN,
            "APIFY_ACTOR_ID": APIFY_ACTOR_ID,
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
    current_buses = fetch_latest_buses(actor_input)

    if not current_buses:
        # An empty response (past date, actor failure) must not overwrite the baseline,
        # or the next run would compare against nothing and stay silent forever.
        print("No buses returned. Leaving history untouched.")
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
            # No notification on a quiet run: the cron fires frequently and a
            # "no drop" push each time is noise.
            print(f"No fare drop of at least Rs.{config.get('min_drop_amount', 50)} detected. "
                  f"No alert sent.")
    else:
        print("New trip baseline saved. Will compare on the next run.")

    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump({"signature": search_signature, "buses": current_buses}, f, indent=2)


if __name__ == "__main__":
    main()
