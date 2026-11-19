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


def analyze_with_ai(config, previous_buses, current_buses):
    from google import genai

    client = genai.Client(api_key=GEMINI_API_KEY)
    journey_date = config.get("dateOfJourney", config.get("date", "")) or "unspecified"
    bus_ac_type = config.get("bus_ac_type", "Any")
    seat_type = config.get("seat_type", "Any")
    min_drop_amount = config.get("min_drop_amount", 50)
    prompt = f"""
    You are a bus fare analyst tracking {config['source']} to {config['destination']} on {journey_date}.

    STRICT FILTERS (Apply these first):
    - Bus AC Preference: {bus_ac_type} (Only include buses matching this preference; if 'Any', include both AC and Non-AC).
    - Seat Type Preference: {seat_type} (Only evaluate fares for this exact seat tier; if 'Any', evaluate all tiers separately).

    COMPARISON RULES:
    1. Match buses across PREVIOUS_FARES and CURRENT_FARES by Operator Name, Bus Type, and Departure Time.
    2. Ignore cases where a lower price appears simply because a higher-priced tier sold out and only a cheaper tier remains.
    3. Only flag a GENUINE price drop if the matching seat tier dropped in fare by at least ₹{min_drop_amount}.
    4. If there is NO genuine drop matching the filters, reply with exact text: NO_DROP
    5. If there IS a genuine drop, reply with a concise alert showing:
       - Operator & Bus Type
       - Departure Time
       - Seat Type & Old Fare -> New Fare

    PREVIOUS_FARES:
    {json.dumps(previous_buses)}

    CURRENT_FARES:
    {json.dumps(current_buses)}
    """

    for model in AI_MODELS:
        try:
            print("Trying with model: ", model)
            response = client.models.generate_content(
                model=model,
                contents=prompt,
            )
            verdict = (response.text or "").strip()
            print("Raw Response from AI: ", response)
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
