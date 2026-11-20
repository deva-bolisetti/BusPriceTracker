"""RedBus-direct tracker: fetches fares for free directly from redBus.

Uses the same config.json, history.json and ntfy alerting as tracker_apify.py, but
replaces the Apify fetch and the Gemini analysis with deterministic Python.
No paid API is involved anywhere in this runner.

Usage:
    python src/tracker_direct.py
"""

import json
import os
import re
from pathlib import Path

import redbus_client

REPO_ROOT = Path(__file__).resolve().parents[1]

NTFY_TOPIC = os.environ.get("NTFY_TOPIC")

CONFIG_FILE = REPO_ROOT / "config.json"
HISTORY_FILE = REPO_ROOT / "data" / "history.json"

# Gemini is no longer called; alert text is formatted locally by format_alert_message().
# Kept here only to document which models the old analysis used, in case AI phrasing is
# ever reintroduced (see the commented-out analyze_with_ai block further down).
# AI_MODELS = ("gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-3.1-flash-lite")


def load_json(filepath, default):
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def load_config(filepath=CONFIG_FILE):
    """Load config.json, allowing // comments so allowed values are discoverable."""
    return redbus_client.load_jsonc(str(filepath), {})


def scraper_input(config):
    """Mirror tracker_apify.py so the trip signature stays byte-compatible."""
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
        city_cache_file=str(REPO_ROOT / "data" / "cities.json"),
    )


SEAT_TIER_KEYS = {
    # redBus sells single and double berths as separate tiers with separate prices
    # (a bus offering SLEEPER at Rs.2500 also offers SINGLE_SLEEPER at Rs.2750), so
    # a plain "Sleeper" request deliberately covers both. Every bus in the baseline
    # that has SINGLE_SLEEPER also has SLEEPER, so this loses no coverage.
    "single sleeper": ["SINGLE_SLEEPER"],
    "double sleeper": ["SLEEPER"],
    "sleeper": ["SLEEPER", "SINGLE_SLEEPER"],
    "seater": ["SEATER"],
}

ALL_TIER_KEYS = ["SEATER", "SLEEPER", "SINGLE_SLEEPER"]


def resolve_tier_keys(seat_pref):
    """Map a seat_type value onto the tier keys to compare.

    Accepts a single name ("Sleeper"), a list, or a delimited string
    ("Single Sleeper, Double Sleeper"). Unknown input falls back to every tier.
    """
    if isinstance(seat_pref, (list, tuple)):
        parts = list(seat_pref)
    else:
        parts = re.split(r"[,/&+]", str(seat_pref or ""))

    keys = []
    for part in parts:
        for key in SEAT_TIER_KEYS.get(str(part).strip().lower(), []):
            if key not in keys:
                keys.append(key)
    return keys or list(ALL_TIER_KEYS)


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

    tier_keys = resolve_tier_keys(config.get("seat_type", "Any"))

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


def fetch_identity(config):
    """The fields that decide *what gets fetched*, and therefore what a snapshot means.

    seat_type and bus_ac_type are deliberately absent: they only filter the two
    snapshots during comparison, never the fetch. Both snapshots store the full
    inventory with every fare tier and isAc, so changing either filter can reuse an
    existing baseline instead of throwing it away.
    """
    return {
        "source": config.get("source"),
        "destination": config.get("destination"),
        "dateOfJourney": config.get("dateOfJourney", config.get("date", "")),
        "fromCity": config.get("fromCity"),
        "toCity": config.get("toCity"),
    }


def search_signature_for(config):
    """Signature stored beside the buses, describing which trip they belong to."""
    return json.dumps({"v": 2, "fetch": fetch_identity(config)}, sort_keys=True)


def _stored_fetch_identity(stored_signature):
    """Read the fetch identity out of a signature in either the current or legacy format."""
    try:
        parsed = json.loads(stored_signature or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None

    # Current format: {"v": 2, "fetch": {...}}
    if isinstance(parsed.get("fetch"), dict):
        return parsed["fetch"]

    # Legacy format: the flat Apify-style input plus the two filter strings. Buses in
    # a legacy baseline are still valid for the same trip, so adopt them rather than
    # forcing a needless re-baseline on upgrade.
    if "source" in parsed:
        identity = {key: parsed.get(key) for key in ("source", "destination", "dateOfJourney")}
        for key in ("fromCity", "toCity"):
            if key in parsed:
                identity[key] = parsed.get(key)
        return identity

    return None


def signature_buses_are_reusable(stored_signature, config):
    """True when a stored snapshot may be compared against the configured trip."""
    stored = _stored_fetch_identity(stored_signature)
    if not stored:
        return False

    current = fetch_identity(config)
    if any(stored.get(key) != current[key] for key in ("source", "destination", "dateOfJourney")):
        return False

    # Only enforced when the stored signature actually recorded city IDs.
    if any(key in stored for key in ("fromCity", "toCity")):
        if any(stored.get(key) != current[key] for key in ("fromCity", "toCity")):
            return False

    return True


def describe_signature_change(stored_signature, config):
    """Human-readable list of what changed about the trip, for logs and the push notice."""
    stored = _stored_fetch_identity(stored_signature) or {}
    current = fetch_identity(config)
    return [
        f"{key}: {stored.get(key)!r} -> {current[key]!r}"
        for key in sorted(current)
        if stored.get(key) != current[key]
    ]


def send_baseline_notice(config, changes):
    """Tell the phone that tracking restarted, so a silent reset is not a surprise.

    Low priority: this is informational, not an alert. A failure here must not
    stop the baseline from being written.
    """
    import requests

    source = config.get("source", "?")
    destination = config.get("destination", "?")
    journey_date = config.get("dateOfJourney", config.get("date", "")) or "unspecified"

    lines = [
        f"Tracking restarted: {source} to {destination} on {journey_date}",
        "",
        "config.json changed, so a new baseline was saved.",
        "Fare comparison starts on the next run.",
    ]
    if changes:
        lines.append("")
        lines.extend(f"- {change}" for change in changes)

    ntfy_url = NTFY_TOPIC if NTFY_TOPIC.startswith("http") else f"https://ntfy.sh/{NTFY_TOPIC}"
    try:
        requests.post(
            ntfy_url,
            data="\n".join(lines).encode("utf-8"),
            headers={
                "Title": f"Tracker re-baselined: {source} to {destination}",
                "Priority": "low",
                "Tags": "arrows_counterclockwise",
            },
            timeout=15,
        )
    except Exception as error:
        print(f"  could not send baseline notice: {type(error).__name__}: {error}")


def main():
    config = load_config(CONFIG_FILE)
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

    search_signature = search_signature_for(config)

    history = load_json(HISTORY_FILE, {})
    previous_signature = history.get("signature")
    reusable = bool(previous_signature) and signature_buses_are_reusable(previous_signature, config)
    signature_changed = bool(previous_signature) and not reusable
    signature_changes = (
        describe_signature_change(previous_signature, config)
        if signature_changed
        else []
    )

    if signature_changed:
        print("Trip changed since the last run:")
        for change in signature_changes:
            print(f"  {change}")
        print("Discarding the old baseline; this run establishes a new one.")
    elif history and not previous_signature:
        print("Stored history has no signature (older format); treating this run as a fresh baseline.")

    previous_buses = history.get("buses", []) if reusable else []

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
        if signature_changed:
            print("Baseline reset after a config change; no comparison this run.")
            send_baseline_notice(config, signature_changes)
        else:
            print("New trip baseline saved. Will compare on the next run.")

    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump({"signature": search_signature, "buses": current_buses}, f, indent=2)


if __name__ == "__main__":
    main()
