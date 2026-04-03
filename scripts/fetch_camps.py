#!/usr/bin/env python3
"""
HorseCamp Data Fetcher
Runs nightly via GitHub Actions.
Calls Recreation.gov (RIDB) and NPS APIs, writes results to camps.json
which is served at horsecampfinder.com/camps.json for the iOS app.

Required GitHub Secrets:
  RIDB_API_KEY  — from ridb.recreation.gov/profile
  NPS_API_KEY   — from developer.nps.gov/signup
"""

import os, json, time, re, requests
from datetime import datetime, timezone

RIDB_KEY   = os.environ.get("RIDB_API_KEY", "")
NPS_KEY    = os.environ.get("NPS_API_KEY", "")
GOOGLE_KEY = os.environ.get("GOOGLE_PLACES_KEY", "")

RIDB_BASE = "https://ridb.recreation.gov/api/v1"
NPS_BASE  = "https://developer.nps.gov/api/v1"
NWS_BASE  = "https://api.weather.gov"
FIRE_BASE = "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/WFIGS_Incident_Locations_Current/FeatureServer/0/query"

STATES = [
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID",
    "IL","IN","IA","KS","KY","LA","ME","MD","MA","MI","MN","MS",
    "MO","MT","NE","NV","NH","NJ","NM","NY","NC","ND","OH","OK",
    "OR","PA","RI","SC","SD","TN","TX","UT","VT","VA","WA","WV","WI","WY"
]

EQUESTRIAN_KEYWORDS = [
    "horse", "equestrian", "corral", "stall", "horseback",
    "highline", "high line", "tie rail", "paddock", "horse camp",
    "horse trail", "pack station", "mule", "llama"
]

def strip_html(text):
    return re.sub(r'<[^>]+>', '', text or '').strip()

def is_equestrian(text_blob):
    low = text_blob.lower()
    return any(k in low for k in EQUESTRIAN_KEYWORDS)

def safe_get(url, headers=None, params=None, retries=3):
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=headers, params=params, timeout=15)
            if r.status_code == 200:
                return r.json()
            elif r.status_code == 429:
                print(f"  Rate limited — waiting 10s...")
                time.sleep(10)
            else:
                print(f"  HTTP {r.status_code} for {url}")
                return None
        except Exception as e:
            print(f"  Request error (attempt {attempt+1}): {e}")
            time.sleep(3)
    return None

# ── RIDB HELPERS ──────────────────────────────────────────────────────
MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}

def parse_season(facility):
    """Extract real open/close months from FACILITYSEASON array or description."""
    seasons = facility.get("FACILITYSEASON") or []
    start_month, end_month = 1, 12  # fallback to year-round when unknown

    for season in seasons:
        start_str = season.get("StartDate", "") or ""
        end_str   = season.get("EndDate", "")   or ""
        # Dates come as "YYYY-MM-DD" or "MM/DD" or month names
        try:
            if "-" in start_str:
                start_month = int(start_str.split("-")[1])
            if "-" in end_str:
                end_month = int(end_str.split("-")[1])
            break
        except:
            pass

    # Also try the description text as fallback
    if start_month == 5 and end_month == 10:
        desc = (facility.get("FacilitySeasonDescription") or "").lower()
        for name, num in MONTH_MAP.items():
            if name in desc:
                # First month mentioned = start, last = end
                if num < start_month or start_month == 5:
                    start_month = num
                if num > end_month or end_month == 10:
                    end_month = num

    return max(1, min(12, start_month)), max(1, min(12, end_month))

def parse_rig_length(facility):
    """Extract max vehicle/rig length from campsite data."""
    campsites = facility.get("CAMPSITE") or []
    max_len = 0
    for site in campsites:
        # MaxVehicleLength is in feet
        try:
            length = int(site.get("MaxVehicleLength") or site.get("CampsiteMaxLength") or 0)
            if length > max_len:
                max_len = length
        except:
            pass
    return max_len if max_len > 0 else 60  # fallback to 60 if not specified

def parse_stall_count(facility):
    """Extract actual stall count from amenities."""
    amenities = facility.get("FACILITYAMENITY") or []
    for a in amenities:
        name = (a.get("AmenityName") or "").lower()
        if "stall" in name:
            try:
                qty = int(a.get("AmenityValue") or a.get("Quantity") or 0)
                if qty > 0:
                    return qty
            except:
                pass
    return 0

def parse_paddock_count(facility):
    """Extract actual corral/paddock count from amenities."""
    amenities = facility.get("FACILITYAMENITY") or []
    for a in amenities:
        name = (a.get("AmenityName") or "").lower()
        if "corral" in name or "paddock" in name:
            try:
                qty = int(a.get("AmenityValue") or a.get("Quantity") or 0)
                if qty > 0:
                    return qty
            except:
                pass
    return 0

def parse_price(fid, headers):
    """Fetch lowest nightly rate from campsites under this facility."""
    data = safe_get(
        f"{RIDB_BASE}/facilities/{fid}/campsites",
        headers=headers,
        params={"limit": 50, "offset": 0}
    )
    if not data:
        return 0.0

    lowest = 0.0
    for site in (data.get("RECDATA") or []):
        # Campsites have a SITEGROUP with fees, or CAMPSITE_FEES directly
        for fee in (site.get("CAMPSITE_FEES") or []):
            try:
                amount = float(fee.get("FeeAmount") or 0)
                if amount > 0 and (lowest == 0.0 or amount < lowest):
                    lowest = amount
            except:
                pass
        # Also check PERMITTEDEQUIPMENT for fee data
        for eq in (site.get("PERMITTEDEQUIPMENT") or []):
            try:
                amount = float(eq.get("MaxLength") or 0)  # not fee, skip
            except:
                pass

    return lowest

# ── RIDB ───────────────────────────────────────────────────────────────
def fetch_ridb_state(state):
    camps = {}
    headers = {"apikey": RIDB_KEY}
    search_terms = [
        ("activity", "9"),           # activity 9 = Horseback Riding
        ("query", "horse corral"),
        ("query", "equestrian"),
        ("query", "horse camp"),
        ("query", "horse stall"),
    ]

    for param_key, param_val in search_terms:
        offset = 0
        while True:
            params = {
                param_key: param_val,
                "state":   state,
                "limit":   50,
                "offset":  offset,
                "full":    "true",
            }
            data = safe_get(f"{RIDB_BASE}/facilities", headers=headers, params=params)
            if not data:
                break
            facilities = data.get("RECDATA", [])
            if not facilities:
                break

            for f in facilities:
                fid = str(f.get("FacilityID", ""))
                if not fid or fid in camps:
                    continue

                lat = float(f.get("FacilityLatitude", 0) or 0)
                lng = float(f.get("FacilityLongitude", 0) or 0)
                if abs(lat) < 0.1 or abs(lng) < 0.1:
                    continue

                amenities  = [a.get("AmenityName", "") for a in (f.get("FACILITYAMENITY") or [])]
                activities = [a.get("ActivityName", "") for a in (f.get("ACTIVITY") or [])]
                desc       = strip_html(f.get("FacilityDescription", ""))
                blob       = " ".join(amenities + activities + [desc])

                if not is_equestrian(blob):
                    continue

                addr  = (f.get("FACILITYADDRESS") or [{}])[0]
                city  = addr.get("City", "")
                fstate = addr.get("AddressStateCode", state)

                blob_lower = blob.lower()

                hookups = []
                if "50 amp" in blob_lower or "50-amp" in blob_lower: hookups.append("50A")
                if "30 amp" in blob_lower or "30-amp" in blob_lower: hookups.append("30A")
                if "water hookup" in blob_lower:                       hookups.append("Water")
                if "dump station" in blob_lower or "sewer" in blob_lower: hookups.append("Dump Station")
                if not hookups: hookups.append("No Hookups")

                accommodations = []
                if "stall"    in blob_lower: accommodations.append("Stalls")
                if "corral"   in blob_lower: accommodations.append("Corrals")
                if "highline" in blob_lower or "high line" in blob_lower or "tie rail" in blob_lower:
                    accommodations.append("Highlines")
                if "wash rack" in blob_lower: accommodations.append("Wash Rack")
                if "paddock"  in blob_lower: accommodations.append("Paddocks")
                accommodations.append("Trails")

                season_start, season_end = parse_season(f)
                price = parse_price(fid, headers)
                camps[fid] = {
                    "id":                  f"ridb-{fid}",
                    "name":                f.get("FacilityName", "Unknown Camp"),
                    "location":            f"{city}, {fstate}".strip(", "),
                    "state":               fstate,
                    "latitude":            lat,
                    "longitude":           lng,
                    "pricePerNight":       price,
                    "horseFeePerNight":    0.0,
                    "hookups":             list(dict.fromkeys(hookups)),
                    "accommodations":      list(dict.fromkeys(accommodations)),
                     "maxRigLength":        parse_rig_length(f),
                     "stallCount":          parse_stall_count(f),
                     "paddockCount":        parse_paddock_count(f),
                    "phone":               f.get("FacilityPhone", ""),

                    "website":             f.get("FacilityReservationURL", "") or f"https://www.recreation.gov/camping/campgrounds/{fid}",
                    "description":         desc[:2000],
                    "isVerified":          True,
                     "seasonStart":         season_start,
                     "seasonEnd":           season_end,
                    "hasWashRack":         "wash rack" in blob_lower,
                    "hasDumpStation":      "dump" in blob_lower,
                    "hasWifi":             "wifi" in blob_lower or "internet" in blob_lower,
                    "hasBathhouse":        "shower" in blob_lower or "bathhouse" in blob_lower,
                    "pullThroughAvailable": "pull-through" in blob_lower or "pull through" in blob_lower,
                    "rating":              0.0,
                    "reviewCount":         0,
                    "imageColors":         ["5C7A4E", "D4A853"],
                    "source":              "RIDB",
                }

            offset += 50
            if len(facilities) < 50:
                break
            time.sleep(0.5)

        time.sleep(0.3)

    return list(camps.values())


# ── NPS ────────────────────────────────────────────────────────────────
def fetch_nps_state(state):
    camps = []
    headers = {"X-Api-Key": NPS_KEY}
    params  = {"stateCode": state, "limit": 100, "start": 0}

    data = safe_get(f"{NPS_BASE}/campgrounds", headers=headers, params=params)
    if not data:
        return camps

    for c in data.get("data", []):
        desc       = c.get("description", "")
        amenities  = c.get("amenities", {})
        blob       = " ".join([
            desc,
            amenities.get("horseTrailsOnsite", ""),
            amenities.get("corralOrPaddockOnsite", ""),
            amenities.get("stableNearby", ""),
        ])

        if not is_equestrian(blob):
            continue

        try:
            lat = float(c.get("latitude", 0))
            lng = float(c.get("longitude", 0))
        except:
            continue
        if abs(lat) < 0.1 or abs(lng) < 0.1:
            continue

        addr    = (c.get("addresses") or [{}])[0]
        city    = addr.get("city", "")
        fee     = 0.0
        fees    = c.get("fees") or []
        if fees:
            try: fee = float(fees[0].get("cost", 0))
            except: pass

        hookups = []
        if amenities.get("electricalHookups") == "Yes": hookups.append("30A")
        if not hookups: hookups.append("No Hookups")

        accommodations = ["Trails"]
        if amenities.get("corralOrPaddockOnsite") == "Yes": accommodations.append("Corrals")
        if amenities.get("stableNearby") == "Yes":          accommodations.append("Stalls")

        contacts = c.get("contacts", {})
        phones   = contacts.get("phoneNumbers", [])
        phone    = phones[0].get("phoneNumber", "") if phones else ""

        camps.append({
            "id":                  f"nps-{c['id']}",
            "name":                c.get("name", "NPS Camp"),
            "location":            f"{city}, {state}".strip(", "),
            "state":               state,
            "latitude":            lat,
            "longitude":           lng,
            "pricePerNight":       fee,
            "horseFeePerNight":    0.0,
            "hookups":             hookups,
            "accommodations":      list(dict.fromkeys(accommodations)),
            "maxRigLength":        55,
            "stallCount":          6 if amenities.get("stableNearby") == "Yes" else 0,
            "paddockCount":        4 if amenities.get("corralOrPaddockOnsite") == "Yes" else 0,
            "phone":               phone,
            "website":             c.get("url", f"https://www.nps.gov/{c.get('parkCode', '')}/"),
            "description":         desc[:2000],
            "isVerified":          True,
            "seasonStart":         1,
            "seasonEnd":           12,
            "hasWashRack":         False,
            "hasDumpStation":      amenities.get("dumpStation") == "Yes",
            "hasWifi":             amenities.get("internetConnectivity") == "Yes",
            "hasBathhouse":        "shower" in str(amenities.get("showers", "") or "").lower(),
            "pullThroughAvailable": False,
            "rating":              0.0,
            "reviewCount":         0,
            "imageColors":         ["4A7FA5", "5C7A4E"],
            "source":              "NPS",
        })

    return camps



# ── GOOGLE PLACES ──────────────────────────────────────────────────────
def fetch_google_places(existing_camps):
    """
    Fetches equestrian camps from Google Places API.
    Deduplicates against existing RIDB/NPS camps by proximity (500m radius).
    Requires GOOGLE_PLACES_KEY secret in GitHub.
    """
    if not GOOGLE_KEY:
        print("  GOOGLE_PLACES_KEY not set — skipping Google Places")
        return []

    import math

    def haversine_meters(lat1, lon1, lat2, lon2):
        R = 6371000
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlam = math.radians(lon2 - lon1)
        a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

    def is_duplicate(lat, lng, threshold_m=500):
        for camp in existing_camps.values():
            if haversine_meters(lat, lng, camp["latitude"], camp["longitude"]) < threshold_m:
                return True
        return False

    queries = [
        "equestrian campground",
        "horse camp overnight",
        "horse corral camping",
        "equestrian park camping",
    ]

    # Google Places Text Search covers the whole US
    # We search nationally and let proximity dedup handle overlap
    base_url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
    camps = {}
    seen = set()

    for query in queries:
        params = {
            "query": query,
            "type": "campground",
            "key": GOOGLE_KEY,
            "region": "us",
        }
        page_token = None
        pages = 0

        while pages < 3:  # Google returns max 3 pages (60 results) per query
            if page_token:
                params = {"pagetoken": page_token, "key": GOOGLE_KEY}
                time.sleep(2)  # Google requires 2s delay between page_token requests

            data = safe_get(base_url, params=params)
            if not data or data.get("status") not in ("OK", "ZERO_RESULTS"):
                break

            for place in data.get("results", []):
                pid = place.get("place_id", "")
                if not pid or pid in seen:
                    continue
                seen.add(pid)

                loc = place.get("geometry", {}).get("location", {})
                lat = loc.get("lat", 0)
                lng = loc.get("lng", 0)
                if not lat or not lng:
                    continue

                # Skip if too close to an existing RIDB/NPS camp
                if is_duplicate(lat, lng):
                    continue

                name = place.get("name", "")
                if not name or not is_equestrian(name + " " + " ".join(place.get("types", []))):
                    continue

                # Fetch place details for phone, website, address
                detail_data = safe_get(
                    "https://maps.googleapis.com/maps/api/place/details/json",
                    params={
                        "place_id": pid,
                        "fields": "name,formatted_address,formatted_phone_number,website,url",
                        "key": GOOGLE_KEY,
                    }
                )
                detail = detail_data.get("result", {}) if detail_data else {}

                address = detail.get("formatted_address", place.get("formatted_address", ""))
                # Extract state from address (last part before ZIP)
                parts = [p.strip() for p in address.split(",")]
                state = ""
                for part in reversed(parts):
                    # State abbreviation is 2 uppercase letters
                    words = part.strip().split()
                    for w in words:
                        if len(w) == 2 and w.isupper():
                            state = w
                            break
                    if state:
                        break

                # Only include US camps
                if not state or state not in [
                    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID",
                    "IL","IN","IA","KS","KY","LA","ME","MD","MA","MI","MN","MS",
                    "MO","MT","NE","NV","NH","NJ","NM","NY","NC","ND","OH","OK",
                    "OR","PA","RI","SC","SD","TN","TX","UT","VT","VA","WA","WV","WI","WY"
                ]:
                    continue

                city = parts[-3] if len(parts) >= 3 else ""

                camps[pid] = {
                    "id":                  f"google-{pid}",
                    "name":                name,
                    "location":            f"{city}, {state}".strip(", "),
                    "state":               state,
                    "latitude":            lat,
                    "longitude":           lng,
                    "pricePerNight":       0.0,
                    "horseFeePerNight":    0.0,
                    "hookups":             ["No Hookups"],
                    "accommodations":      ["Trails"],
                    "maxRigLength":        55,
                    "stallCount":          0,
                    "paddockCount":        0,
                    "phone":               detail.get("formatted_phone_number", ""),
                    "website":             detail.get("website", detail.get("url", "")),
                    "description":         f"Equestrian facility in {city}, {state}. Verify amenities before arrival.",
                    "isVerified":          False,
                    "seasonStart":         1,
                    "seasonEnd":           12,
                    "hasWashRack":         False,
                    "hasDumpStation":      False,
                    "hasWifi":             False,
                    "hasBathhouse":        False,
                    "pullThroughAvailable": False,
                    "rating":              float(place.get("rating", 0)),
                    "reviewCount":         place.get("user_ratings_total", 0),
                    "imageColors":         ["8B5E3C", "D4A853"],
                    "source":              "Google Places",
                }

            next_token = data.get("next_page_token")
            if not next_token:
                break
            page_token = next_token
            pages += 1

        time.sleep(0.5)

    result = list(camps.values())
    print(f"  Google Places: {len(result)} new camps (not in RIDB/NPS within 500m)")
    return result



# ── MAIN ───────────────────────────────────────────────────────────────
def main():
    print(f"HorseCamp data fetch starting — {datetime.now(timezone.utc).isoformat()}")
    print(f"RIDB key present: {'Yes' if RIDB_KEY else 'NO — set RIDB_API_KEY secret'}")
    print(f"NPS key present:  {'Yes' if NPS_KEY  else 'NO — set NPS_API_KEY secret'}")
    print(f"Google key present: {'Yes' if GOOGLE_KEY else 'NO — set GOOGLE_PLACES_KEY secret'}")

    all_camps = {}
    total_ridb = 0
    total_nps  = 0

    for i, state in enumerate(STATES):
        print(f"[{i+1}/{len(STATES)}] {state}...", end=" ", flush=True)

        ridb_camps = fetch_ridb_state(state) if RIDB_KEY else []
        nps_camps  = fetch_nps_state(state)  if NPS_KEY  else []

        state_new = 0
        for camp in ridb_camps + nps_camps:
            cid = camp["id"]
            if cid not in all_camps:
                all_camps[cid] = camp
                state_new += 1

        total_ridb += len(ridb_camps)
        total_nps  += len(nps_camps)
        print(f"{len(ridb_camps)} RIDB + {len(nps_camps)} NPS = {state_new} new")

        time.sleep(0.5)  # be polite to APIs

    # Google Places — deduplicated against RIDB/NPS by proximity
    print("\nFetching from Google Places (deduplication against existing camps)...")
    google_camps = fetch_google_places(all_camps)
    for camp in google_camps:
        cid = camp["id"]
        if cid not in all_camps:
            all_camps[cid] = camp

    camps_list = sorted(all_camps.values(), key=lambda c: c["state"])

    output = {
        "generated":  datetime.now(timezone.utc).isoformat(),
        "count":      len(camps_list),
        "sources":    ["Recreation.gov RIDB", "NPS API"],
        "camps":      camps_list,
    }

    # Write to root so GitHub Pages serves it at horsecampfinder.com/camps.json
    with open("camps.json", "w") as f:
        json.dump(output, f, indent=2)

    google_count = sum(1 for c in camps_list if c.get("source") == "Google Places")
    print(f"\nDone. {len(camps_list)} total camps written to camps.json")
    print(f"  RIDB:         {total_ridb}")
    print(f"  NPS:          {total_nps}")
    print(f"  Google Places:{google_count}")
    print(f"  Unique total: {len(camps_list)}")


if __name__ == "__main__":
    main()
