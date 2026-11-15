import json
import os
import requests
from google import genai

APIFY_TOKEN = os.environ.get("APIFY_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")
APIFY_API_URL = (os.environ.get("APIFY_API_URL") or "https://api.apify.com").rstrip("/")
APIFY_ACTOR_ID = os.environ.get("APIFY_ACTOR_ID")

if not APIFY_TOKEN:
    raise RuntimeError("APIFY_TOKEN is not set. Use the raw token only, not a URL.")
if not APIFY_ACTOR_ID:
    raise RuntimeError("APIFY_ACTOR_ID is not set. Add the Apify actor ID.")
if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY is not set.")
if not NTFY_TOPIC:
    raise RuntimeError("NTFY_TOPIC is not set.")

CONFIG_FILE = "config.json"
HISTORY_FILE = "history.json"


def load_json(filepath, default):
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def fetch_latest_buses(source, destination, date):
    url = f"{APIFY_API_URL}/v2/acts/{APIFY_ACTOR_ID}/run-sync-get-dataset-items"

    payload = {
        "source": source,
        "destination": destination,
        "departureDate": date
    }

    response = requests.post(
        url,
        headers={"Authorization": f"Bearer {APIFY_TOKEN}"},
        json=payload,
        timeout=120,
    )
    response.raise_for_status()
    data = response.json()

    if isinstance(data, dict) and "items" in data:
        return data["items"]
    if isinstance(data, list):
        return data
    return data


def analyze_with_ai(config, previous_buses, current_buses):
    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = f"""
    You are a bus fare analyst tracking {config['source']} to {config['destination']} on {config['date']}.
    
    STRICT FILTERS (Apply these first):
    - Bus AC Preference: {config['bus_ac_type']} (Only include buses matching this preference; if 'Any', include both AC and Non-AC).
    - Seat Type Preference: {config['seat_type']} (Only evaluate fares for this exact seat tier; if 'Any', evaluate all tiers separately).
    
    COMPARISON RULES:
    1. Match buses across PREVIOUS_FARES and CURRENT_FARES by Operator Name, Bus Type, and Departure Time.
    2. Ignore cases where a lower price appears simply because a higher-priced tier sold out and only a cheaper tier remains.
    3. Only flag a GENUINE price drop if the matching seat tier dropped in fare by at least ₹{config['min_drop_amount']}.
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
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
    )
    return response.text.strip()


def send_android_alert(config, message):
    ntfy_url = NTFY_TOPIC if NTFY_TOPIC.startswith("http") else f"https://ntfy.sh/{NTFY_TOPIC}"
    requests.post(
        ntfy_url,
        data=message.encode("utf-8"),
        headers={
            "Title": f"Bus Drop ({config['seat_type']}): {config['source']} to {config['destination']}",
            "Priority": "high",
            "Tags": "bus,money_with_wings"
        },
        timeout=15
    )


def main():
    config = load_json(CONFIG_FILE, {})
    if not config.get("active", True):
        print("Tracker is paused in config.json. Exiting.")
        return

    history = load_json(HISTORY_FILE, {})
    search_signature = f"{config['source']}|{config['destination']}|{config['date']}|{config['bus_ac_type']}|{config['seat_type']}"

    previous_buses = history.get("buses", []) if history.get("signature") == search_signature else []
    current_buses = fetch_latest_buses(config["source"], config["destination"], config["date"])

    if previous_buses and current_buses:
        verdict = analyze_with_ai(config, previous_buses, current_buses)
        if verdict != "NO_DROP":
            send_android_alert(config, verdict)
            print("Alert sent:\n", verdict)
        else:
            print("No genuine price drop detected.")
    else:
        print("New trip baseline saved. Will compare on the next run.")

    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump({"signature": search_signature, "buses": current_buses}, f, indent=2)


if __name__ == "__main__":
    main()
