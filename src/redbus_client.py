"""Direct redBus client: replaces the Apify actor with free JSON API calls.

Verified endpoints (November 2026):
  GET  /Home/SolarSearch?search=<name>   -> response.docs[] city records
  POST /rpw/api/searchResults?...        -> data.inventories[] bus inventory

Output is normalised to the same field names the Apify actor returned, so the
existing history.json baseline keeps matching and no re-baseline is needed.
"""

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]

REDBUS_BASE = "https://www.redbus.in"
SEARCH_PATH = "/rpw/api/searchResults"
SOLAR_PATH = "/Home/SolarSearch"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36"
)

PAGE_SIZE = 100
MAX_PAGES = 10
REQUEST_TIMEOUT = 30
RETRIES = 3
RETRY_BACKOFF = 2.0

CITY_CACHE_FILE = str(REPO_ROOT / "data" / "cities.json")

EMPTY_FILTER_BODY: Dict[str, Any] = {
    "appliedFilterCount": 0,
    "onlyShow": [],
    "dt": [],
    "SeaterType": [],
    "AcType": [],
    "travelsList": [],
    "amtList": [],
    "bpList": [],
    "dpList": [],
    "CampaignFilter": [],
    "at": [],
    "persuasionList": [],
    "bpIdentifier": [],
    "dpIdentifier": [],
    "bcf": [],
    "opBusTypeFilterList": [],
    "priceRange": [],
    "RouteIds": [],
    "bpKeys": [],
    "dpKeys": [],
    "streaksFilter": [],
    "preRouteFilters": None,
}


class RedbusError(RuntimeError):
    pass


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-IN,en;q=0.9",
            "Referer": f"{REDBUS_BASE}/",
        }
    )
    return session


def _request(session: requests.Session, method: str, url: str, **kwargs) -> requests.Response:
    last_error: Optional[Exception] = None
    for attempt in range(1, RETRIES + 1):
        try:
            response = session.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
            if response.status_code == 200:
                return response
            last_error = RedbusError(f"HTTP {response.status_code} for {url}")
            if response.status_code in (401, 403, 429):
                print(f"  redBus returned {response.status_code} (likely bot challenge); "
                      f"attempt {attempt}/{RETRIES}")
        except Exception as error:
            last_error = error
            print(f"  request error on attempt {attempt}/{RETRIES}: {type(error).__name__}: {error}")
        if attempt < RETRIES:
            time.sleep(RETRY_BACKOFF * attempt)
    raise RedbusError(f"redBus request failed after {RETRIES} attempts: {last_error}")


def format_doj(date_string: str) -> str:
    """Accept YYYY-MM-DD or DD-Mon-YYYY and return redBus' DD-Mon-YYYY."""
    for fmt in ("%Y-%m-%d", "%d-%b-%Y"):
        try:
            return datetime.strptime(date_string, fmt).strftime("%d-%b-%Y")
        except ValueError:
            continue
    raise ValueError(f"Unrecognised date format: {date_string!r} (expected YYYY-MM-DD)")


def load_city_cache(path: str = CITY_CACHE_FILE) -> Dict[str, Any]:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_city_cache(cache: Dict[str, Any], path: str = CITY_CACHE_FILE) -> None:
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(cache, handle, indent=2, sort_keys=True)
    except OSError as error:
        print(f"  could not write city cache: {error}")


def _pick_city(docs: List[Dict[str, Any]], name: str) -> Optional[Dict[str, Any]]:
    """Prefer an exact CITY-level match, then the highest-ranked city."""
    wanted = name.strip().lower()

    cities = [d for d in docs if str(d.get("locationType", "")).upper() == "CITY"]
    for doc in cities:
        if str(doc.get("Name", "")).strip().lower() == wanted:
            return doc
    for doc in cities:
        if wanted in str(doc.get("Name", "")).strip().lower():
            return doc
    if cities:
        return max(cities, key=lambda d: d.get("rank") or 0)

    for doc in docs:
        if str(doc.get("Name", "")).strip().lower() == wanted:
            return doc
    return docs[0] if docs else None


def resolve_city_id(
    session: requests.Session,
    name: str,
    cache: Optional[Dict[str, Any]] = None,
) -> int:
    """Resolve a city name to a redBus city ID via the SolarSearch JSON API."""
    key = name.strip().lower()
    if cache is not None and key in cache:
        return int(cache[key]["id"])

    response = _request(
        session,
        "GET",
        f"{REDBUS_BASE}{SOLAR_PATH}",
        params={"search": name},
        headers={"X-Requested-With": "XMLHttpRequest", "Referer": f"{REDBUS_BASE}/"},
    )

    payload = response.json()
    docs = ((payload.get("response") or {}).get("docs")) or []
    if not docs:
        raise RedbusError(f"SolarSearch returned no cities for {name!r}")

    doc = _pick_city(docs, name)
    if not doc or doc.get("ID") is None:
        raise RedbusError(f"Could not pick a city match for {name!r}")

    city_id = int(doc["ID"])
    print(f"  resolved {name!r} -> id={city_id} "
          f"(name={doc.get('Name')!r}, type={doc.get('locationType')})")

    if cache is not None:
        cache[key] = {
            "id": city_id,
            "name": doc.get("Name"),
            "locationType": doc.get("locationType"),
        }
    return city_id


def _search_params(from_id: int, to_id: int, doj: str, offset: int, limit: int) -> Dict[str, Any]:
    return {
        "fromCity": from_id,
        "toCity": to_id,
        "DOJ": doj,
        "limit": limit,
        "offset": offset,
        "meta": "true",
        "groupId": 0,
        "sectionId": 0,
        "sort": 0,
        "sortOrder": 0,
        "from": "initialLoad",
        "getUuid": "true",
        "bT": 1,
        "clearLMBFilter": "undefined",
        "isFilterApplied": "false",
    }


def fetch_raw_inventories(
    session: requests.Session,
    from_id: int,
    to_id: int,
    doj: str,
    source: str,
    destination: str,
) -> List[Dict[str, Any]]:
    """Page through searchResults until every advertised bus is collected."""
    referer = (
        f"{REDBUS_BASE}/bus-tickets/{source.strip().lower().replace(' ', '-')}"
        f"-to-{destination.strip().lower().replace(' ', '-')}"
        f"?fromCityId={from_id}&fromCityName={quote(source)}"
        f"&toCityId={to_id}&toCityName={quote(destination)}"
        f"&onward={doj}&doj={doj}&ref=search"
    )
    headers = {
        "Content-Type": "application/json",
        "Origin": REDBUS_BASE,
        "Referer": referer,
    }

    collected: List[Dict[str, Any]] = []
    seen: set = set()
    total: Optional[int] = None

    for page in range(MAX_PAGES):
        offset = page * PAGE_SIZE
        response = _request(
            session,
            "POST",
            f"{REDBUS_BASE}{SEARCH_PATH}",
            params=_search_params(from_id, to_id, doj, offset, PAGE_SIZE),
            json=EMPTY_FILTER_BODY,
            headers=headers,
        )
        payload = response.json()
        data = payload.get("data") or {}
        inventories = data.get("inventories") or []

        if total is None:
            total = (data.get("metaData") or {}).get("totalCount")
            print(f"  redBus reports totalCount={total}")

        new_items = 0
        for bus in inventories:
            key = (bus.get("routeId"), bus.get("serviceId"), bus.get("departureTime"))
            if key in seen:
                continue
            seen.add(key)
            collected.append(bus)
            new_items += 1

        print(f"  page {page + 1}: offset={offset} returned={len(inventories)} new={new_items} "
              f"collected={len(collected)}")

        if not inventories or new_items == 0:
            break
        if total is not None and len(collected) >= int(total):
            break

    return collected


def _seat_fare_map(fare_details: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise to the Apify shape: {tier: [{originalPrice, discountedPrice, count}]}.

    Apify emitted a list per tier, so the existing history.json baseline and the
    drop detector expect lists. Keep that shape exactly.
    """
    mapped: Dict[str, Any] = {}
    if not isinstance(fare_details, dict):
        return mapped
    for tier, entries in fare_details.items():
        normalised: List[Dict[str, Any]] = []
        for entry in entries if isinstance(entries, list) else [entries]:
            if isinstance(entry, dict):
                record: Dict[str, Any] = {"originalPrice": entry.get("originalPrice")}
                if entry.get("discountedPrice") is not None:
                    record["discountedPrice"] = entry["discountedPrice"]
                if entry.get("count") is not None:
                    record["count"] = entry["count"]
                normalised.append(record)
            elif isinstance(entry, (int, float)):
                normalised.append({"originalPrice": entry})
        if normalised:
            mapped[tier] = normalised
    return mapped


def normalize_bus(raw: Dict[str, Any], source: str, destination: str) -> Dict[str, Any]:
    """Map redBus inventory JSON onto the Apify field names used by history.json."""
    fare_details = _seat_fare_map(raw.get("fareDetailsBySeatType") or {})
    fare_list = raw.get("fareList") or []

    numeric_fares = [f for f in fare_list if isinstance(f, (int, float))]
    min_fare = min(numeric_fares) if numeric_fares else None
    max_fare = max(numeric_fares) if numeric_fares else None

    if raw.get("isAc") is not None:
        is_ac = raw.get("isAc")
    elif raw.get("isNonAc"):
        is_ac = False
    else:
        is_ac = None

    if raw.get("isSleeper") is not None:
        is_sleeper = raw.get("isSleeper")
    elif raw.get("isSeater"):
        is_sleeper = False
    else:
        is_sleeper = None

    campaigns = ((raw.get("operatorOfferCampaign") or {}).get("CmpgList")) or []
    offers = [
        {
            "campaignCode": campaign.get("CampaignCode"),
            "description": campaign.get("CampaignDesc"),
            "type": campaign.get("CampaignType"),
            "discount": campaign.get("Discount"),
            "maxDiscount": campaign.get("maxDiscount"),
            "dealType": campaign.get("dealType"),
        }
        for campaign in campaigns
    ]

    operator_id = raw.get("operatorId")
    return {
        "id": raw.get("routeId"),
        "operatorId": operator_id,
        "travelsName": raw.get("travelsName"),
        "routeId": raw.get("routeId"),
        "serviceId": raw.get("serviceId"),
        "serviceName": raw.get("serviceName"),
        "operatorLogoUrl": (
            f"https://s3.rdbuz.com/images/{raw['operatorLogoPath']}"
            if raw.get("operatorLogoPath")
            else None
        ),
        "busType": raw.get("busType"),
        "busTypeId": raw.get("busTypeId"),
        "isAc": is_ac,
        "isSleeper": is_sleeper,
        "isDirectBus": None,
        "viaRoute": None,
        "amenities": raw.get("amenities") or [],
        "isPartialCancellationAllowed": raw.get("isPartialCancellationAllowed"),
        "isLiveTrackingAvailable": raw.get("isLiveTrackingAvailable"),
        "isMTicketEnabled": raw.get("isMticketEnabled"),
        "maxSeatsPerTransaction": raw.get("maxSeatsPerTransaction"),
        "serviceNotes": raw.get("serviceNotes"),
        "inventoryTags": raw.get("inventoryTags"),
        "departureTime": raw.get("departureTime"),
        "arrivalTime": raw.get("arrivalTime"),
        "journeyDurationMin": raw.get("journeyDurationMin"),
        "dateOfJourney": raw.get("doj"),
        "firstBpTime": raw.get("firstBpTime"),
        "boardingPoint": raw.get("standardBpName"),
        "boardingPointId": raw.get("standardBpIdentifier"),
        "droppingPoint": raw.get("standardDpName"),
        "droppingPointId": raw.get("standardDpIdentifier"),
        "boardingPointCount": raw.get("bpCount"),
        "droppingPointCount": raw.get("dpCount"),
        "fareList": fare_list,
        "minFare": min_fare,
        "maxFare": max_fare,
        "vendorCurrency": raw.get("vendorCurrency", "INR"),
        "fareDetailsBySeatType": fare_details,
        "availableSeats": raw.get("availableSeats"),
        "totalSeats": raw.get("totalSeats"),
        "availableWindowSeats": raw.get("availableWindowSeats"),
        "availableSingleSeats": raw.get("availableSingleSeats"),
        "availableAisleSeats": raw.get("availableAisleSeats"),
        "availableUpperSeats": raw.get("availableUpperSeats"),
        "availableLowerSeats": raw.get("availableLowerSeats"),
        "isSeatLayoutAvailable": raw.get("isSeatLayoutAvailable"),
        "rating": raw.get("totalRatings"),
        "numberOfReviews": raw.get("numberOfReviews"),
        "busScore": raw.get("busScore"),
        "perzScore": raw.get("perzScore"),
        "isRescheduled": raw.get("isRescheduled"),
        "rescheduleTime": raw.get("rescheduleTime"),
        "campaignType": raw.get("campaignType"),
        "programList": raw.get("programList"),
        "offers": offers,
        "busImageCount": raw.get("busImageCount"),
        "rs555": raw.get("rs555"),
        "locationSearchParams": raw.get("locationSearchParams"),
        "boReqParams": raw.get("boReqParams") or [],
        "source": source,
        "destination": destination,
    }


def fetch_buses(
    source: str,
    destination: str,
    date_of_journey: str,
    from_city_id: Optional[int] = None,
    to_city_id: Optional[int] = None,
    city_cache_file: str = CITY_CACHE_FILE,
) -> List[Dict[str, Any]]:
    """Fetch and normalise every bus for a route/date. Drop-in for the Apify actor."""
    if not source or not destination:
        raise ValueError("source and destination must be non-empty")

    doj = format_doj(date_of_journey)
    session = build_session()
    cache = load_city_cache(city_cache_file)

    try:
        if from_city_id is None:
            from_city_id = resolve_city_id(session, source, cache)
        if to_city_id is None:
            to_city_id = resolve_city_id(session, destination, cache)
    finally:
        save_city_cache(cache, city_cache_file)

    print(f"Fetching redBus inventory: {source} ({from_city_id}) -> "
          f"{destination} ({to_city_id}) for {doj}")

    raw = fetch_raw_inventories(session, from_city_id, to_city_id, doj, source, destination)
    buses = [normalize_bus(item, source, destination) for item in raw]
    buses.sort(key=lambda b: b.get("departureTime") or "")
    print(f"Normalised {len(buses)} buses")
    return buses
