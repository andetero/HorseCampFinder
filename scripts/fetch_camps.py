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
    """Extract max rig length from PERMITTEDEQUIPMENT on campsites."""
    campsites = facility.get("CAMPSITE") or []
    max_len = 0
    for site in campsites:
        for eq in (site.get("PERMITTEDEQUIPMENT") or []):
            eq_name = (eq.get("EquipmentName") or "").lower()
            # Only care about trailer/RV type equipment
            if any(k in eq_name for k in ["trailer", "rv", "motorhome", "camper", "horse"]):
                try:
                    length = int(eq.get("MaxLength") or 0)
                    if length > max_len:
                        max_len = length
                except:
                    pass
    return max_len if max_len > 0 else 0  # 0 = unknown, app shows nothing

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
                camps[fid] = {
                    "id":                  f"ridb-{fid}",
                    "name":                f.get("FacilityName", "Unknown Camp"),
                    "location":            f"{city}, {fstate}".strip(", "),
                    "state":               fstate,
                    "latitude":            lat,
                    "longitude":           lng,
                    "pricePerNight":       0.0,
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




# ── LAYOVER LISTINGS ───────────────────────────────────────────────────
# Curated horse layover facilities — private barns and farms that
# welcome overnight horse travelers. Call to verify before arrival.
# Source: community-curated list, names/coords/phones only.
def fetch_layovers():
    return [
    {
        "id": "layover-1140081",
        "name": "Stable Stay: Horse Hotel & AirBnB",
        "location": "Altoona, AL",
        "state": "AL",
        "latitude": 34.1336666666667,
        "longitude": -86.4018611111111,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "205-446-3994",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8749829",
        "name": "Camp McClellan Horse Trails Campground",
        "location": "Anniston, AL",
        "state": "AL",
        "latitude": 33.6958333333333,
        "longitude": -85.7756666666667,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "256-954-1623",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3368884",
        "name": "Hickory Hill Farm",
        "location": "Brundidge, AL",
        "state": "AL",
        "latitude": 31.6095833333333,
        "longitude": -85.7933055555555,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "205-531-6952",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8694051",
        "name": "Bin Swindled Farm",
        "location": "Cropwell, AL",
        "state": "AL",
        "latitude": 33.510385,
        "longitude": -86.319631,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "205-365-3185",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4349553",
        "name": "RED STONE BED & BARN",
        "location": "Hamilton, AL",
        "state": "AL",
        "latitude": 34.0845277777778,
        "longitude": -88.0200277777778,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "850-225-0302",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1041228",
        "name": "Spyglass Farms",
        "location": "Mobile, AL",
        "state": "AL",
        "latitude": 30.617988,
        "longitude": -88.267061,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "251-401-6228",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4258308",
        "name": "4-R Arena",
        "location": "Ralph, AL",
        "state": "AL",
        "latitude": 33.05069,
        "longitude": -87.76482,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "205-799-4601",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8633441",
        "name": "Rocking \"A\" Ranch",
        "location": "Silverhill, AL",
        "state": "AL",
        "latitude": 30.51381,
        "longitude": -87.78918,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "251-747-6464",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9564195",
        "name": "Caddo Equestrian",
        "location": "Trinity, AL",
        "state": "AL",
        "latitude": 34.56831,
        "longitude": -87.14312,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "256-476-8335",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7285381",
        "name": "Tok Line Camp B & B",
        "location": "Tok, AK",
        "state": "AK",
        "latitude": 63.3336,
        "longitude": -143.03632,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "907-883-5506",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4629110",
        "name": "The Aspen Lodge",
        "location": "Alpine, AZ",
        "state": "AZ",
        "latitude": 33.8439444444444,
        "longitude": -109.159555555556,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "866-322-7736",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3531255",
        "name": "Christina Dora Equestrian, LLC",
        "location": "Apache Junction, AZ",
        "state": "AZ",
        "latitude": 33.427641,
        "longitude": -111.526706,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "602-550-0823",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1983805",
        "name": "Lazee Day Dairy",
        "location": "Apache Junction, AZ",
        "state": "AZ",
        "latitude": 33.3961666666667,
        "longitude": -111.511583333333,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "780-991-6995",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8110604",
        "name": "Bobbie McGhee Stables",
        "location": "Apache Junction, AZ",
        "state": "AZ",
        "latitude": 33.4145833333333,
        "longitude": -111.553527777778,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "402-276-1014",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9387505",
        "name": "Nicolls Country Ranch",
        "location": "Ash Fork, AZ",
        "state": "AZ",
        "latitude": 35.25622,
        "longitude": -112.48015,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "928-606-1058",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5050401",
        "name": "Spears Ranch",
        "location": "Benson, AZ",
        "state": "AZ",
        "latitude": 31.933815,
        "longitude": -110.256445,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "520 349 5940",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8806349",
        "name": "Montezuma Old West Ranch House",
        "location": "Camp Verde, AZ",
        "state": "AZ",
        "latitude": 34.591341,
        "longitude": -111.865666,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "602-526-4276",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4792596",
        "name": "ARIZONA Horse Layovers * Nashwa Farms",
        "location": "Camp Verde, AZ",
        "state": "AZ",
        "latitude": 34.640611,
        "longitude": -111.916222,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "602-525-0630",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6571646",
        "name": "Stewart Ranch",
        "location": "Cave Creek, AZ",
        "state": "AZ",
        "latitude": 33.820767,
        "longitude": -112.017048,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "602-702-1786",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2455466",
        "name": "Cave Creek Ranch",
        "location": "Cave Creek, AZ",
        "state": "AZ",
        "latitude": 33.815304,
        "longitude": -111.976525,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "623-329-5416",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1609564",
        "name": "Heidi Inman",
        "location": "Cave Creek, AZ",
        "state": "AZ",
        "latitude": 33.75752,
        "longitude": -111.95941,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "360-975-2836",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3936921",
        "name": "Rocky Mt Ranches",
        "location": "Coolidge, AZ",
        "state": "AZ",
        "latitude": 32.9876111111111,
        "longitude": -111.452,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "602-300-0620",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6493384",
        "name": "The Ranch at Trails End, LLC",
        "location": "Cottonwood, AZ",
        "state": "AZ",
        "latitude": 34.7212222222222,
        "longitude": -111.975972222222,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "928-301-8523",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6183345",
        "name": "Jon Ray Ranch",
        "location": "Douglas, AZ",
        "state": "AZ",
        "latitude": 31.404459,
        "longitude": -109.558112,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "520-368-1502",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5167431",
        "name": "The Happy Trails Cabin",
        "location": "Elgin, AZ",
        "state": "AZ",
        "latitude": 31.5936388888889,
        "longitude": -110.601472222222,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "928-350-5576",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9927798",
        "name": "7 bar M Ranch",
        "location": "Eloy, AZ",
        "state": "AZ",
        "latitude": 32.6745,
        "longitude": -111.51625,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "360-489-5892",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5626333",
        "name": "Summer Sage Stables",
        "location": "Flagstaff, AZ",
        "state": "AZ",
        "latitude": 35.2145833333333,
        "longitude": -111.557305555556,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "928-853-9005",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-335624",
        "name": "Sycamore Ranch",
        "location": "Flagstaff, AZ",
        "state": "AZ",
        "latitude": 35.3085,
        "longitude": -111.542444444444,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "928-525-4756",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-165263",
        "name": "HAPPY HORSE & DOG PET RESORT",
        "location": "Fredonia, AZ",
        "state": "AZ",
        "latitude": 36.964835,
        "longitude": -112.529376,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "435-899-9335",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3143237",
        "name": "Tumbling T Ranch",
        "location": "Goodyear, AZ",
        "state": "AZ",
        "latitude": 33.4557,
        "longitude": -112.4021,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "602-369-0945",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4923584",
        "name": "Goodwater Ranch, LLC",
        "location": "Holbrook, AZ",
        "state": "AZ",
        "latitude": 34.9775,
        "longitude": -109.883916666667,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "602-300-8176",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8769104",
        "name": "Blake Ranch RV Park & Horse Motel",
        "location": "Kingman, AZ",
        "state": "AZ",
        "latitude": 35.1796,
        "longitude": -113.78897,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "928-757-3336",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5901091",
        "name": "Wild West Town & Ranch",
        "location": "Mesa, AZ",
        "state": "AZ",
        "latitude": 33.3071944444444,
        "longitude": -111.582166666667,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "303-594-4433",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3398385",
        "name": "Wickenburg Horse Motel",
        "location": "Morristown, AZ",
        "state": "AZ",
        "latitude": 33.871307,
        "longitude": -112.640245,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "928-671-1482",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-675381",
        "name": "Hale Family Ranch",
        "location": "Payson, AZ",
        "state": "AZ",
        "latitude": 34.1060555555556,
        "longitude": -111.275166666667,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "480-229-8765",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8941474",
        "name": "Lazy Horse Ranch",
        "location": "Pearce, AZ",
        "state": "AZ",
        "latitude": 31.930777,
        "longitude": -109.755964,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "602-620-0247",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5077013",
        "name": "Clover Equine",
        "location": "Phoenix, AZ",
        "state": "AZ",
        "latitude": 33.836639,
        "longitude": -112.130417,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "602-526-5555",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8409272",
        "name": "Windrush Ranch, LLC",
        "location": "Phoenix, AZ",
        "state": "AZ",
        "latitude": 33.801556,
        "longitude": -112.0525,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "602-390-1460",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-154375",
        "name": "Bob's Big City Barn",
        "location": "Phoenix, AZ",
        "state": "AZ",
        "latitude": 33.6180277777778,
        "longitude": -112.149805555556,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "602-684-1092",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6198038",
        "name": "Lost Cowboy Ranch",
        "location": "Scottsdale, AZ",
        "state": "AZ",
        "latitude": 33.75697,
        "longitude": -111.73426,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "602-697-3260",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6148670",
        "name": "Rio Rancho Verde",
        "location": "Scottsdale, AZ",
        "state": "AZ",
        "latitude": 33.81338,
        "longitude": -111.84219,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "480-227-0568",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-33914",
        "name": "Red Rock",
        "location": "Sedona, AZ",
        "state": "AZ",
        "latitude": 34.8333055555556,
        "longitude": -111.865583333333,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6288798",
        "name": "Irbinskas Stables RV Horse Camp",
        "location": "Sonoita, AZ",
        "state": "AZ",
        "latitude": 31.744968,
        "longitude": -110.641492,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "520-444-2460",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-587614",
        "name": "Tombstone Livery Stable",
        "location": "Tombstone, AZ",
        "state": "AZ",
        "latitude": 31.74794,
        "longitude": -110.10391,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "520-226-5915",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7066880",
        "name": "Stewart Runner's Horse Motel, RV Park and Arena",
        "location": "Tonopah, AZ",
        "state": "AZ",
        "latitude": 33.49395,
        "longitude": -112.81879,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "623-386-5124",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7828185",
        "name": "China Cabinet Ranch",
        "location": "Tucson, AZ",
        "state": "AZ",
        "latitude": 32.333667,
        "longitude": -111.175222,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "520-400-3877",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5166130",
        "name": "Rocking M Ranch",
        "location": "Tucson, AZ",
        "state": "AZ",
        "latitude": 32.32008,
        "longitude": -111.10781,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "520-744-2457",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9831016",
        "name": "Lazy H Bar Desert Outpost Vacation Rental",
        "location": "Tucson, AZ",
        "state": "AZ",
        "latitude": 31.9871111111111,
        "longitude": -111.299777777778,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "707-939-5616",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9881026",
        "name": "Moody Ranch",
        "location": "Tucson, AZ",
        "state": "AZ",
        "latitude": 32.3566388888889,
        "longitude": -111.079388888889,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "520-405-6041",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6972724",
        "name": "Wandering Rose Farm and Stables",
        "location": "Tucson, AZ",
        "state": "AZ",
        "latitude": 32.20201,
        "longitude": -111.169,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "208-880-1294",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9379132",
        "name": "Deseret Akers",
        "location": "Wickenburg, AZ",
        "state": "AZ",
        "latitude": 33.97195,
        "longitude": -112.7243,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "970-215-2659",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4209902",
        "name": "Two Bit Ranch",
        "location": "Wickenburg, AZ",
        "state": "AZ",
        "latitude": 33.9699166666667,
        "longitude": -112.809666666667,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "602-509-0297",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6038896",
        "name": "Lainee Shearer Quarter Horses",
        "location": "Wittmann, AZ",
        "state": "AZ",
        "latitude": 33.8075555555556,
        "longitude": -112.519333333333,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "605-200-1777",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4477636",
        "name": "McClure Ranch",
        "location": "Yuma, AZ",
        "state": "AZ",
        "latitude": 32.70425,
        "longitude": -114.555694,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "928-210-2128",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4035405",
        "name": "Circle L Equine",
        "location": "Blytheville, AR",
        "state": "AR",
        "latitude": 35.951,
        "longitude": -89.90894,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "870-623-2824",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6005998",
        "name": "Rocking P Stables",
        "location": "Brinkley, AR",
        "state": "AR",
        "latitude": 34.915165,
        "longitude": -91.194457,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "501-626-3926",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3486277",
        "name": "The Bunkhouse",
        "location": "AR, Clarksville",
        "state": "",
        "latitude": 35.5014444444444,
        "longitude": -93.4721388888889,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "479-979-7662",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9394004",
        "name": "Hobby Horse Hotel",
        "location": "Conway, AR",
        "state": "AR",
        "latitude": 35.1181388888889,
        "longitude": -92.4095,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "501-215-3881",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1121795",
        "name": "Blue Line Farm,",
        "location": "Fouke, AR",
        "state": "AR",
        "latitude": 33.280722,
        "longitude": -93.872194,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "870-653-6227",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7419282",
        "name": "Shady Trail Horse Motel",
        "location": "Greenbrier, AR",
        "state": "AR",
        "latitude": 35.24054,
        "longitude": -92.49138,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "501-679-3606",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2518511",
        "name": "Norton Farm Stays",
        "location": "Harrison, AR",
        "state": "AR",
        "latitude": 36.1433611111111,
        "longitude": -93.1259722222222,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "870-715-7255",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9117262",
        "name": "Saddle Inn Hazen",
        "location": "Hazen, AR",
        "state": "AR",
        "latitude": 34.810299,
        "longitude": -91.567019,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "218-838-6541",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1255093",
        "name": "KS Horse Motel",
        "location": "Mabelvale, AR",
        "state": "AR",
        "latitude": 34.6409722222222,
        "longitude": -92.4086666666667,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "501-776-7500",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8765079",
        "name": "I40 Horse Hotel",
        "location": "Mulberry, AR",
        "state": "AR",
        "latitude": 35.5565555555556,
        "longitude": -94.1246666666667,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "479-430-0623",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8723235",
        "name": "NFC Rodeo",
        "location": "Ozark, AR",
        "state": "AR",
        "latitude": 35.497365,
        "longitude": -93.85671,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "479-209-0741",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1910287",
        "name": "2B Farms",
        "location": "Russellville, AR",
        "state": "AR",
        "latitude": 35.34863,
        "longitude": -93.01641,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "479-968-3651",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-654704",
        "name": "Buckaroo Ranch",
        "location": "Russellville, AR",
        "state": "AR",
        "latitude": 35.33314,
        "longitude": -93.083505,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "479-264-3554",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2281257",
        "name": "Happy Hooves Horse Hotel",
        "location": "Anderson, CA",
        "state": "CA",
        "latitude": 40.45001,
        "longitude": -122.32448,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "530-941-0544",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8058180",
        "name": "NorCal Riding Stables",
        "location": "Anderson, CA",
        "state": "CA",
        "latitude": 40.435722,
        "longitude": -122.260444,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "530-515-8958",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1750144",
        "name": "Galbraith's End of the Road Ranch",
        "location": "Bakersfield, CA",
        "state": "CA",
        "latitude": 35.31097,
        "longitude": -118.90387,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "661-845-3013",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7070839",
        "name": "JKC Ranch",
        "location": "Bakersfield, CA",
        "state": "CA",
        "latitude": 35.379714,
        "longitude": -119.211122,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "682-444-1701",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3096936",
        "name": "Happi Ranch",
        "location": "Barstow, CA",
        "state": "CA",
        "latitude": 34.90955,
        "longitude": -117.08502,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "254-368-7891",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3581097",
        "name": "Silver Glen Stables",
        "location": "Fresno, CA",
        "state": "CA",
        "latitude": 36.79238,
        "longitude": -119.93721,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "559-285-1153",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8412643",
        "name": "Rancho Ruiz",
        "location": "Gilroy, CA",
        "state": "CA",
        "latitude": 37.04052,
        "longitude": -121.60414,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "408-309-0538",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7903157",
        "name": "Collins Ranch",
        "location": "Hemet, CA",
        "state": "CA",
        "latitude": 33.612838,
        "longitude": -116.942405,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "951-541-4822",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-154528",
        "name": "McGrady Livestock and Roping Arena",
        "location": "Hollister, CA",
        "state": "CA",
        "latitude": 36.924186,
        "longitude": -121.36061,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "831-455-5098",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3988680",
        "name": "Bonfire Ranch and Horse Motel",
        "location": "Inyokern, CA",
        "state": "CA",
        "latitude": 35.77831,
        "longitude": -117.86353,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "760-377-4088",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6038838",
        "name": "Harris Rustic Ranch",
        "location": "Loomis, CA",
        "state": "CA",
        "latitude": 38.818167,
        "longitude": -121.190444,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "916-663-7032",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3089308",
        "name": "LAZY LEE RANCH & STAYS",
        "location": "Los Molinos, CA",
        "state": "CA",
        "latitude": 40.0474166666667,
        "longitude": -122.102777777778,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "530-949-9566",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2444153",
        "name": "Fogline Ridge Ranch",
        "location": "McKinleyville, CA",
        "state": "CA",
        "latitude": 40.9173055555556,
        "longitude": -124.069222222222,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "925-918-2848",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4593858",
        "name": "Larry Tarpley Training Center at RET Park Ranch",
        "location": "Norco, CA",
        "state": "CA",
        "latitude": 33.935708,
        "longitude": -117.541301,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "714-357-5799",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6141706",
        "name": "RockinCW",
        "location": "Pine Valley, CA",
        "state": "CA",
        "latitude": 32.8316944444444,
        "longitude": -116.529111111111,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "858-775-7299",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3797307",
        "name": "Valley Horse Center",
        "location": "Red Bluff, CA",
        "state": "CA",
        "latitude": 40.260083,
        "longitude": -122.197187,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "707-688-1741",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6695393",
        "name": "Rusty Wire Ranch",
        "location": "Red Bluff, CA",
        "state": "CA",
        "latitude": 40.1255833333333,
        "longitude": -122.175166666667,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "971-209-5314",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6074017",
        "name": "2 Bar M Ranch",
        "location": "Red Bluff, CA",
        "state": "CA",
        "latitude": 40.1720833333333,
        "longitude": -122.290666666667,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "907-230-9959",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1705536",
        "name": "FIG TREE RANCH",
        "location": "Riverside, CA",
        "state": "CA",
        "latitude": 33.8553055555556,
        "longitude": -117.361111111111,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "714-357-8955",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9688158",
        "name": "Golden Horse Ranch Boarding",
        "location": "Shadow Hills, CA",
        "state": "CA",
        "latitude": 34.24978,
        "longitude": -118.34878,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "818-641-6745",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2091181",
        "name": "Stevinson Ranch and Stables",
        "location": "Stevinson, CA",
        "state": "CA",
        "latitude": 37.34194,
        "longitude": -120.83425,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "209-226-5145",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7994659",
        "name": "D'Alonzo Ranch Equestrian Center",
        "location": "Stockton, CA",
        "state": "CA",
        "latitude": 37.92522,
        "longitude": -121.35377,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "209-406-4075",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1189391",
        "name": "Bill & Amy Todd",
        "location": "Tehachapi, CA",
        "state": "CA",
        "latitude": 35.126451,
        "longitude": -118.510211,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "661-645-2088",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5465727",
        "name": "Hacienda Laubela, LLC",
        "location": "Temecula, CA",
        "state": "CA",
        "latitude": 33.4811388888889,
        "longitude": -117.131888888889,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "951-746-7309",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5258291",
        "name": "Gentle Winds Ranch",
        "location": "Templeton, CA",
        "state": "CA",
        "latitude": 35.54425,
        "longitude": -120.713861111111,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "650-400-3604",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6477568",
        "name": "Boswell Kennel & Stable",
        "location": "Three Rivers, CA",
        "state": "CA",
        "latitude": 36.4698888888889,
        "longitude": -118.91925,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "559-553-3931",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7994534",
        "name": "Double H Ranch",
        "location": "Yreka, CA",
        "state": "CA",
        "latitude": 41.7309722222222,
        "longitude": -122.634166666667,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "530-598-9722",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5917532",
        "name": "Sutter Buttes Polo Club",
        "location": "Yuba City, CA",
        "state": "CA",
        "latitude": 39.007042,
        "longitude": -121.609117,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "530-306-1058",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6420082",
        "name": "Parkinson Ranch",
        "location": "Yucaipa, CA",
        "state": "CA",
        "latitude": 34.03407,
        "longitude": -117.05643,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "909-224-3191",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1139630",
        "name": "Big Iron Ranch",
        "location": "Aurora/Watkins, CO",
        "state": "CO",
        "latitude": 39.710278,
        "longitude": -104.577722,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "303-344-5009",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-697257",
        "name": "Lost Creek Acres Horse Boarding and Hoteling",
        "location": "Bennett, CO",
        "state": "CO",
        "latitude": 39.719825,
        "longitude": -104.527148,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "719-440-1344",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7079329",
        "name": "Elevate Ranch",
        "location": "Bennett, CO",
        "state": "CO",
        "latitude": 39.6975833333333,
        "longitude": -104.527361111111,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "303-644-9333",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9330357",
        "name": "Andersen Farms",
        "location": "Broomfield, CO",
        "state": "CO",
        "latitude": 39.986469,
        "longitude": -104.974589,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "720-201-4672",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2748931",
        "name": "Mondorosa Bed & Barn",
        "location": "CO\u00a0 Canon City",
        "state": "",
        "latitude": 38.6575555555556,
        "longitude": -105.403861111111,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "312-446-4736",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9211637",
        "name": "Higher Ground Horsemanship & Performance Horses",
        "location": "Cimarron, CO",
        "state": "CO",
        "latitude": 38.3639444444444,
        "longitude": -107.587722222222,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "970-596-0911",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9717032",
        "name": "Indigo Winds Farm",
        "location": "Colorado Springs, CO",
        "state": "CO",
        "latitude": 38.68366,
        "longitude": -104.47805,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "719-717-0202",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2935194",
        "name": "Double Check Arena",
        "location": "Eaton, CO",
        "state": "CO",
        "latitude": 40.545335,
        "longitude": -104.658733,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "970-284-5555",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3869238",
        "name": "Amazing Grace Ranch, LLC",
        "location": "Ellicott, CO",
        "state": "CO",
        "latitude": 38.86154,
        "longitude": -104.38764,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "719-460-6361",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2693612",
        "name": "Turtle Creek Farm, LLC",
        "location": "Fort Collins, CO",
        "state": "CO",
        "latitude": 40.59211,
        "longitude": -104.9265,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "970-215-3536",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6543222",
        "name": "Salt of the Earth Ranch, LLC",
        "location": "Hesperus, CO",
        "state": "CO",
        "latitude": 37.1787777777778,
        "longitude": -108.146111111111,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "814-795-4757",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6385292",
        "name": "United Country Colorado Brokers/ AirbnB",
        "location": "Hotchkiss, CO",
        "state": "CO",
        "latitude": 38.8001944444444,
        "longitude": -107.773527777778,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "970-640-2741",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3156161",
        "name": "Riding2horses Farm",
        "location": "Loveland, CO",
        "state": "CO",
        "latitude": 40.3639722222222,
        "longitude": -105.028444444444,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "307-640-9222",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6630171",
        "name": "Hobo's Hideout Bed Breakfast and Barn",
        "location": "Mancos, CO",
        "state": "CO",
        "latitude": 37.39655,
        "longitude": -108.3493,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "970-560-3070",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8253366",
        "name": "Grand Mesa Stay With Your Horses",
        "location": "Mesa, CO",
        "state": "CO",
        "latitude": 39.1378055555556,
        "longitude": -108.154083333333,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "970-773-8390",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7320594",
        "name": "Elktrace Bed and Breakfast",
        "location": "Pagosa Springs, CO",
        "state": "CO",
        "latitude": 37.300098,
        "longitude": -107.088376,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "970-731-5868",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6677382",
        "name": "Mimosa Ridge, LLC",
        "location": "Parker, CO",
        "state": "CO",
        "latitude": 39.5083055555556,
        "longitude": -104.763333333333,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "512-413-6471",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7026382",
        "name": "Sunset Pines",
        "location": "Parker, CO",
        "state": "CO",
        "latitude": 39.4866666666667,
        "longitude": -104.624,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "720-320-7255",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4330988",
        "name": "Greenhorn Horse Hotel",
        "location": "Pueblo, CO",
        "state": "CO",
        "latitude": 37.99292,
        "longitude": -104.76582,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "719-924-0341",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8379534",
        "name": "Tally Ho Horse Hotel",
        "location": "Rifle, CO",
        "state": "CO",
        "latitude": 39.5483,
        "longitude": -107.70975,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "970-625-0208",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1703597",
        "name": "J Bar Equestrian",
        "location": "Salida, CO",
        "state": "CO",
        "latitude": 38.5217777777778,
        "longitude": -106.105555555556,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "970-901-9507",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1798815",
        "name": "Thor's Horse BnB and Icelandic Rescue",
        "location": "Steamboat Springs, CO",
        "state": "CO",
        "latitude": 40.6141388888889,
        "longitude": -107.058305555556,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "309-236-2672",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-434183",
        "name": "Koda Ranch",
        "location": "Watkins, CO",
        "state": "CO",
        "latitude": 39.6960833333333,
        "longitude": -104.583361111111,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "720-998-2163",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5366697",
        "name": "Interstate Stables",
        "location": "Wellington, CO",
        "state": "CO",
        "latitude": 40.7335,
        "longitude": -104.98889,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "970-484-5082",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8807003",
        "name": "Ravens Roost Ranch",
        "location": "Wellington, CO",
        "state": "CO",
        "latitude": 40.705189,
        "longitude": -104.998535,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "517-852-4484",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3436720",
        "name": "Ironstar Outpost West",
        "location": "Weston, CO",
        "state": "CO",
        "latitude": 37.2899166666667,
        "longitude": -104.955611111111,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "443-676-3811",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6245363",
        "name": "Aspenwood Stables",
        "location": "Woodland Park, CO",
        "state": "CO",
        "latitude": 39.03057,
        "longitude": -105.072701,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "661-675-5346",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1389126",
        "name": "Candlelight Farms Inn and the Hangar at Candlelight Farms",
        "location": "New Milford, CT",
        "state": "CT",
        "latitude": 41.567743,
        "longitude": -73.453724,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "860-210-0594",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8697635",
        "name": "S & A Farm, LLC",
        "location": "Oxford, CT",
        "state": "CT",
        "latitude": 41.473611,
        "longitude": -73.155333,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "203-910-4008",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8183263",
        "name": "Silver Bear Farm",
        "location": "Windham, CT",
        "state": "CT",
        "latitude": 41.684553,
        "longitude": -72.143696,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "860-208-8734",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6612130",
        "name": "Boone's Farm",
        "location": "Greenwood, DE",
        "state": "DE",
        "latitude": 38.8299444444445,
        "longitude": -75.6814166666667,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "410-340-6818",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6845404",
        "name": "Iona Stables and Inn",
        "location": "Milford, DE",
        "state": "DE",
        "latitude": 38.879236,
        "longitude": -75.350415,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "302-542-8138",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3569799",
        "name": "Dusty Rose Farm",
        "location": "Anthony, FL",
        "state": "FL",
        "latitude": 29.3118611111111,
        "longitude": -82.0869166666667,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "636-751-1200",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4223761",
        "name": "Spotted Dance Ranch",
        "location": "Brooksville, FL",
        "state": "FL",
        "latitude": 28.53346,
        "longitude": -82.25539,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "352-442-2737",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1295069",
        "name": "Cozy Cabin",
        "location": "Brooksville, FL",
        "state": "FL",
        "latitude": 28.6024722222222,
        "longitude": -82.3285,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8506146",
        "name": "Remington Ranch",
        "location": "Dade City, FL",
        "state": "FL",
        "latitude": 28.3501388888889,
        "longitude": -82.3660277777778,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "727-433-5865",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6438617",
        "name": "P&V Farms",
        "location": "Defuniak, FL",
        "state": "FL",
        "latitude": 30.8176944444444,
        "longitude": -86.0883888888889,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "850-596-6874",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2027224",
        "name": "Coastal Haven Stables",
        "location": "Freeport, FL",
        "state": "FL",
        "latitude": 30.484361,
        "longitude": -86.279901,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "850-543-8570",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8599126",
        "name": "Durham Farms",
        "location": "McAlpin, FL",
        "state": "FL",
        "latitude": 30.14681,
        "longitude": -82.98293,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "215-962-9173",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4200616",
        "name": "Tickety Boo Farm, LLC",
        "location": "Melrose, FL",
        "state": "FL",
        "latitude": 29.743348,
        "longitude": -81.920946,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "386-847-4642",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3737124",
        "name": "North Florida Horse Motel at Great Grace Farm",
        "location": "Middleburg, FL",
        "state": "FL",
        "latitude": 30.03072,
        "longitude": -81.81159,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "904-891-4788",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4251741",
        "name": "Green Acres Stables",
        "location": "Milton, FL",
        "state": "FL",
        "latitude": 30.50183,
        "longitude": -87.06534,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "850-207-2986",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1815651",
        "name": "RESTABITFARM",
        "location": "Milton, FL",
        "state": "FL",
        "latitude": 30.560722,
        "longitude": -87.10925,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "609-634-3402",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7857022",
        "name": "Flying B Ranch",
        "location": "New Smyrna Beach, FL",
        "state": "FL",
        "latitude": 29.05365,
        "longitude": -81.05183,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "386 478-1244",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6695407",
        "name": "Sundry Farm",
        "location": "Ocala, FL",
        "state": "FL",
        "latitude": 29.25469,
        "longitude": -82.1822,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "352-547-6665",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7314462",
        "name": "Dunwirkyn Farm, LLC",
        "location": "Ocala, FL",
        "state": "FL",
        "latitude": 29.31924,
        "longitude": -82.131409,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "352-693-0179",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3420655",
        "name": "HTH Farms",
        "location": "Ocala, FL",
        "state": "FL",
        "latitude": 29.187155,
        "longitude": -82.296735,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "352-877-2484",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6621807",
        "name": "Equestrian Training Center of Ocala",
        "location": "Ocala, FL",
        "state": "FL",
        "latitude": 29.278728,
        "longitude": -82.172569,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "678-777-4859",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4312903",
        "name": "Villa Azure",
        "location": "Ocala, FL",
        "state": "FL",
        "latitude": 29.0829166666667,
        "longitude": -82.1689166666667,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "727-217-5772",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8907639",
        "name": "Steel Magnolias Equine and Canine Aquatic Center",
        "location": "Pace, FL",
        "state": "FL",
        "latitude": 30.642259,
        "longitude": -87.156667,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "850-304-4381",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5012483",
        "name": "Mandeville Oaks Stables",
        "location": "Pensacola, FL",
        "state": "FL",
        "latitude": 30.4940833333333,
        "longitude": -87.3121111111111,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "850-449-8591",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-968206",
        "name": "H.E.C.K. Ranch",
        "location": "FL- Tallahassee",
        "state": "",
        "latitude": 30.5287222222222,
        "longitude": -84.0110833333333,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "850-688-5758",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8459480",
        "name": "Mahan Farm",
        "location": "Tallahassee, FL",
        "state": "FL",
        "latitude": 30.49051,
        "longitude": -84.13693,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "850-528-1267",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4156008",
        "name": "E-Bar-W Ranch",
        "location": "Ashburn, GA",
        "state": "GA",
        "latitude": 31.70509,
        "longitude": -83.6523,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "229-566-3095",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5525802",
        "name": "Shady Oaks Stables",
        "location": "Brunswick, GA",
        "state": "GA",
        "latitude": 31.297289,
        "longitude": -81.548426,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "912-222-7840",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4427466",
        "name": "Golden Isles Riding Academy",
        "location": "Brunswick, GA",
        "state": "GA",
        "latitude": 31.168738,
        "longitude": -81.621902,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "912-506-3325",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3224580",
        "name": "Anthem Ranch",
        "location": "Buena Vista, GA",
        "state": "GA",
        "latitude": 32.3506944444444,
        "longitude": -84.5623888888889,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "321-274-2044",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8558799",
        "name": "Raise A Glass Ranch",
        "location": "Forsythe, GA",
        "state": "GA",
        "latitude": 33.185003,
        "longitude": -83.899086,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "916-430-9750",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-638455",
        "name": "Along for the Ride",
        "location": "Guyton, GA",
        "state": "GA",
        "latitude": 32.37625,
        "longitude": -81.420056,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "912-531-9434",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1417877",
        "name": "Alliance Horsemanship, LLC",
        "location": "Homer, GA",
        "state": "GA",
        "latitude": 34.3407222222222,
        "longitude": -83.4811388888889,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "706-499-3758",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9372729",
        "name": "Frogcatcher Farm, LLC",
        "location": "Johns Creek, GA",
        "state": "GA",
        "latitude": 34.02494,
        "longitude": -84.21155,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "404-626-5137",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8671861",
        "name": "The Horse Motel, Leesburg GA",
        "location": "Leesburg, GA",
        "state": "GA",
        "latitude": 31.732593,
        "longitude": -84.171603,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "407-395-3863",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6425663",
        "name": "Goose Chase Farm, LLC",
        "location": "Locust Grove, GA",
        "state": "GA",
        "latitude": 33.37387,
        "longitude": -84.1089,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "770-868-7847",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7609867",
        "name": "B&G Stables, Inc.",
        "location": "Madison, GA",
        "state": "GA",
        "latitude": 33.480288,
        "longitude": -83.373579,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "706-818-2938",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4067823",
        "name": "Lightning P Ranch",
        "location": "Resaca, GA",
        "state": "GA",
        "latitude": 34.61548,
        "longitude": -84.88027,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "706-676-8430",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9284448",
        "name": "The Sky Is Not The Limit Farm",
        "location": "Richland, GA",
        "state": "GA",
        "latitude": 31.97625,
        "longitude": -84.6461388888889,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "229-321-8886",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6248863",
        "name": "Friendship Family Farms",
        "location": "Statesboro, GA",
        "state": "GA",
        "latitude": 32.5055833333333,
        "longitude": -81.8418055555556,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "912-667-5271",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5310484",
        "name": "Copperhill Farm, LLC",
        "location": "Sylvania, GA",
        "state": "GA",
        "latitude": 32.8986388888889,
        "longitude": -81.5881944444444,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "912-663-5036",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7169037",
        "name": "Cedar Rock Farm",
        "location": "Thomson, GA",
        "state": "GA",
        "latitude": 33.48484,
        "longitude": -82.52755,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "706-831-6564",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8226596",
        "name": "Trickum Creek Ranch",
        "location": "Tyrone, GA",
        "state": "GA",
        "latitude": 33.4965833333333,
        "longitude": -84.6067777777778,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "404-663-0514",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4203280",
        "name": "Southeastern Arena, LLC",
        "location": "Unadilla, GA",
        "state": "GA",
        "latitude": 32.23627,
        "longitude": -83.73872,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "478-627-2727",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4173394",
        "name": "Windy Willow Stables",
        "location": "Valdosta, GA",
        "state": "GA",
        "latitude": 30.7396,
        "longitude": -83.39407,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "229-560-2738",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1268135",
        "name": "Careywood Creek Ranch",
        "location": "Athol, ID",
        "state": "ID",
        "latitude": 48.0160555555556,
        "longitude": -116.614388888889,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "360-982-1316",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3225956",
        "name": "Snake River Arena",
        "location": "Blackfoot, ID",
        "state": "ID",
        "latitude": 43.247511,
        "longitude": -112.347408,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "208-230-4338",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6131442",
        "name": "'Ears The Place Equine Inn",
        "location": "Driggs, ID",
        "state": "ID",
        "latitude": 43.7814,
        "longitude": -111.10715,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "208 354-2471",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2416882",
        "name": "Mini Acres Farm",
        "location": "Emmett, ID",
        "state": "ID",
        "latitude": 43.8849444444444,
        "longitude": -116.700972222222,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "208-631-4587",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-514509",
        "name": "Elmore County Fairgrounds and Event Center",
        "location": "Glenns Ferry, ID",
        "state": "ID",
        "latitude": 42.959295,
        "longitude": -115.286391,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "208-366-2964",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3964168",
        "name": "Ellis Supreme Arabians",
        "location": "Idaho Falls, ID",
        "state": "ID",
        "latitude": 43.410444,
        "longitude": -112.062139,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "208-521-4473",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6969645",
        "name": "Cowboy Warrior Ranch",
        "location": "Idaho Falls, ID",
        "state": "ID",
        "latitude": 43.55775,
        "longitude": -112.023666666667,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "208-681-1059",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5724305",
        "name": "Carol's Horse Motel",
        "location": "Kimberly, ID",
        "state": "ID",
        "latitude": 42.53367,
        "longitude": -114.36476,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "208-731-3557",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3487195",
        "name": "Hidden Dove Ranch",
        "location": "Lava Hot Springs, ID",
        "state": "ID",
        "latitude": 42.616313,
        "longitude": -112.104898,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "520-253-0732",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1952338",
        "name": "Warren Wagon Ranch",
        "location": "McCall, ID",
        "state": "ID",
        "latitude": 44.9101111111111,
        "longitude": -116.100888888889,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "303-324-9044",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2176360",
        "name": "Laughing Horse Ranch",
        "location": "Meridian, ID",
        "state": "ID",
        "latitude": 43.54043,
        "longitude": -116.35445,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "208-941-1367",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4409471",
        "name": "Rafter H Arena & Horse Hotel",
        "location": "Meridian, ID",
        "state": "ID",
        "latitude": 43.530028,
        "longitude": -116.433849,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "208-869-0267",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2416377",
        "name": "Dunn Rite Ranch",
        "location": "Nampa, ID",
        "state": "ID",
        "latitude": 43.566528,
        "longitude": -116.473778,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "208-718-0166",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4220869",
        "name": "L&L Ranch",
        "location": "Rathdrum, ID",
        "state": "ID",
        "latitude": 47.8550833333333,
        "longitude": -116.803083333333,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9319465",
        "name": "Broken H Ranch",
        "location": "Rupert, ID",
        "state": "ID",
        "latitude": 42.6580555555556,
        "longitude": -113.692444444444,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "702-353-9130",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3013444",
        "name": "3X Livestock",
        "location": "Twin Falls, ID",
        "state": "ID",
        "latitude": 42.490894,
        "longitude": -114.451751,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "503-932-6110",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3321829",
        "name": "High Desert Equestrian Center, LLC",
        "location": "Twin Falls, ID",
        "state": "ID",
        "latitude": 42.48782,
        "longitude": -114.4516,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "509-560-3634",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5539283",
        "name": "Campground Stables",
        "location": "Altamont, IL",
        "state": "IL",
        "latitude": 39.03594,
        "longitude": -88.7497,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "618-267-6990",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-364100",
        "name": "Klocken Fast Farm",
        "location": "Annawan, IL",
        "state": "IL",
        "latitude": 41.35058,
        "longitude": -89.8865,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-377357",
        "name": "Sommers Gate Farms",
        "location": "Bingham, IL",
        "state": "IL",
        "latitude": 39.101039,
        "longitude": -89.201422,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "217-538-1188",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2073383",
        "name": "Victory Reigns Ranch Horse Hotel and Bed and Breakfast",
        "location": "Cherry Valley, IL",
        "state": "IL",
        "latitude": 42.217032,
        "longitude": -88.948766,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "608-346-1734",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3839180",
        "name": "Horse Motel & Bunkhouse at Cement Hollow Equine Ctr",
        "location": "East Carondelet, IL",
        "state": "IL",
        "latitude": 38.491234,
        "longitude": -90.165088,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "314-312-9678",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5169240",
        "name": "Triangle H Farm",
        "location": "Edwardsville, IL",
        "state": "IL",
        "latitude": 38.85242,
        "longitude": -90.02577,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "618-692-4823",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1615887",
        "name": "Allen Quarter Horses",
        "location": "Effingham, IL",
        "state": "IL",
        "latitude": 39.04219,
        "longitude": -88.54374,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "217-343-9201",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2971251",
        "name": "Pine Tree Stables",
        "location": "Effingham, IL",
        "state": "IL",
        "latitude": 39.14231,
        "longitude": -88.59403,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "217-868-5182",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5360853",
        "name": "Wildfire Ranch",
        "location": "Ewing, IL",
        "state": "IL",
        "latitude": 38.0485833333333,
        "longitude": -88.8213611111111,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "618-927-5853",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2578656",
        "name": "Dry Creek Performance Horses, LLC",
        "location": "Hampshire, IL",
        "state": "IL",
        "latitude": 42.1498611111111,
        "longitude": -88.5598055555556,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "847-514-7928",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4307239",
        "name": "Berndt Racing Stable, Inc.",
        "location": "Manhattan, IL",
        "state": "IL",
        "latitude": 41.3986666666667,
        "longitude": -87.9731666666667,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "815-370-7700",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7250658",
        "name": "C & M Clydesdales, LLC",
        "location": "Maple Park, IL",
        "state": "IL",
        "latitude": 41.9356944444444,
        "longitude": -88.4913333333333,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "209-837-3534",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4083660",
        "name": "Woodland Meadows, LLC",
        "location": "Marseilles, IL",
        "state": "IL",
        "latitude": 41.3395,
        "longitude": -88.67269,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5166110",
        "name": "CW Equestrian Center, Inc.",
        "location": "Mascoutah, IL",
        "state": "IL",
        "latitude": 38.480583,
        "longitude": -89.723116,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "618-910-7946",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7630247",
        "name": "Rock N 4L Training Center",
        "location": "Mt. Vernon, IL",
        "state": "IL",
        "latitude": 38.29094,
        "longitude": -88.97891,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "618-244-0700",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9956886",
        "name": "Jim & Katie Troccoli",
        "location": "Ottawa, IL",
        "state": "IL",
        "latitude": 41.362623,
        "longitude": -88.830115,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "815-228-2058",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-813162",
        "name": "Appy Trails",
        "location": "Osgood, IN",
        "state": "IN",
        "latitude": 39.132866,
        "longitude": -85.250007,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "812-569-6255",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-619349",
        "name": "Breaking Wind Farm",
        "location": "Paw Paw, IL",
        "state": "IL",
        "latitude": 41.688611,
        "longitude": -88.981278,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "815-501-4647",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8188496",
        "name": "Gateway Stables",
        "location": "Pontoon Beach, IL",
        "state": "IL",
        "latitude": 38.71505,
        "longitude": -90.07526,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9631446",
        "name": "The Running Wilde Ranch",
        "location": "Sandwich, IL",
        "state": "IL",
        "latitude": 41.666175,
        "longitude": -88.640902,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "561-385-1390",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6338060",
        "name": "Coyote Creek Stables",
        "location": "Sigel, IL",
        "state": "IL",
        "latitude": 39.26544,
        "longitude": -88.48904,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "217-821-7783",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9310291",
        "name": "Forest View Farms",
        "location": "IL- Tinley Park",
        "state": "",
        "latitude": 41.587632,
        "longitude": -87.747463,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "708-560-0306",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4211737",
        "name": "Rocky Road Arena",
        "location": "Vienna, IL",
        "state": "IL",
        "latitude": 37.433206,
        "longitude": -88.892679,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "404-406-0326",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9330592",
        "name": "Freckled Pony Farm",
        "location": "Watson, IL",
        "state": "IL",
        "latitude": 39.0198055555556,
        "longitude": -88.6036111111111,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "217-343-6912",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9434464",
        "name": "Jennifer Ferrell",
        "location": "Angola, IN",
        "state": "IN",
        "latitude": 41.5941388888889,
        "longitude": -84.939,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "260-579-6655",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8241965",
        "name": "Jackspur Ranch",
        "location": "Charlestown, IN",
        "state": "IN",
        "latitude": 38.4880277777778,
        "longitude": -85.6022777777778,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "502-525-3013",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8663246",
        "name": "3-2-1 Ranch",
        "location": "Chrisney, IN",
        "state": "IN",
        "latitude": 38.022124,
        "longitude": -87.008406,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "702-757-8146",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9127214",
        "name": "C Bar C Expo Center, LLC",
        "location": "Cloverdale, IN",
        "state": "IN",
        "latitude": 39.5261388888889,
        "longitude": -86.8199166666667,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "765-720-3251",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6675175",
        "name": "Goodestock Ranch",
        "location": "Columbus, IN",
        "state": "IN",
        "latitude": 39.244502,
        "longitude": -85.988989,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "812-343-9084",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6598500",
        "name": "JML Arena",
        "location": "Franklin, IN",
        "state": "IN",
        "latitude": 39.42075,
        "longitude": -86.0983,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "317-296-0522",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2078621",
        "name": "Barnard Performance Horses AND",
        "location": "Kendalville, IN",
        "state": "IN",
        "latitude": 41.44016,
        "longitude": -85.29027,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "260-347-5777",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1169257",
        "name": "Katavela Farms",
        "location": "Lafayette, IN",
        "state": "IN",
        "latitude": 40.401167,
        "longitude": -86.756694,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "765-404-6482",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1949880",
        "name": "Hartmeyer Stables",
        "location": "Muncie, IN",
        "state": "IN",
        "latitude": 40.2309444444444,
        "longitude": -85.4684166666667,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "765-759-9507",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8437268",
        "name": "Bobbinstone Farm",
        "location": "Oakland City, IN",
        "state": "IN",
        "latitude": 38.29224,
        "longitude": -87.39517,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "812-664-3454",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6482497",
        "name": "EAST FORK STABLES",
        "location": "Richmond, IN",
        "state": "IN",
        "latitude": 39.895413,
        "longitude": -84.852471,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "765-541-2884",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2379727",
        "name": "Park Elwood Stables",
        "location": "Richmond, IN",
        "state": "IN",
        "latitude": 39.8839166666667,
        "longitude": -84.8576666666667,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "765-400-0195",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6387805",
        "name": "Cuttin' Up Stables Horse Motel",
        "location": "Shirley, IN",
        "state": "IN",
        "latitude": 39.86783,
        "longitude": -85.55788,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "765-591-6370",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3560974",
        "name": "Copper Rise Ranch",
        "location": "Springport, IN",
        "state": "IN",
        "latitude": 40.0263888888889,
        "longitude": -85.4222222222222,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "765-524-1383",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-10401",
        "name": "AmazzingGrace Farm, LLC",
        "location": "Yoder, IN",
        "state": "IN",
        "latitude": 40.9314166666667,
        "longitude": -85.2675277777778,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "317-441-4249",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1690278",
        "name": "Alliance Equestrian Center",
        "location": "Yorktown, IN",
        "state": "IN",
        "latitude": 40.21259,
        "longitude": -85.52361,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "765-730-3993",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2074714",
        "name": "Westphalen Quarter Horses",
        "location": "Atlantic, IA",
        "state": "IA",
        "latitude": 41.47104,
        "longitude": -94.9558,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "712-250-4802",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4376958",
        "name": "MK Equestrian",
        "location": "Cedar Falls, IA",
        "state": "IA",
        "latitude": 42.434431,
        "longitude": -92.465877,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "319-215-5319",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3013158",
        "name": "Holley's Boarding Stable",
        "location": "Cedar Rapids, IA",
        "state": "IA",
        "latitude": 41.905343,
        "longitude": -91.613947,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "319-360-6370",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6463010",
        "name": "Hidden Creek Ranch",
        "location": "Des Moines, IA",
        "state": "IA",
        "latitude": 41.65873,
        "longitude": -93.53739,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "515-689-9914",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9898527",
        "name": "Prairie Rose Arena",
        "location": "Elkhart, IA",
        "state": "IA",
        "latitude": 41.761514,
        "longitude": -93.50267,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "515-250-2330",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-974637",
        "name": "A & W Ranch",
        "location": "Grinnell, IA",
        "state": "IA",
        "latitude": 41.747423,
        "longitude": -92.746832,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "641-990-8387",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4146739",
        "name": "Winds Reach Farm",
        "location": "Iowa City, IA",
        "state": "IA",
        "latitude": 41.719468,
        "longitude": -91.501572,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "319-270-9627",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-499569",
        "name": "Horse Outpost",
        "location": "Logan, IA",
        "state": "IA",
        "latitude": 41.574049,
        "longitude": -95.774778,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "712-592-2180",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2911788",
        "name": "Singing Mesa Ranch",
        "location": "Lucas, IA",
        "state": "IA",
        "latitude": 41.089875,
        "longitude": -93.470491,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "641-919-3671",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6069153",
        "name": "Charming Corrals",
        "location": "Nora Springs, IA",
        "state": "IA",
        "latitude": 43.1170277777778,
        "longitude": -92.9450555555556,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "319-480-5619",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1841470",
        "name": "Red Roof Stable and Farm",
        "location": "Princeton, IA",
        "state": "IA",
        "latitude": 41.720507,
        "longitude": -90.359618,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "563-343-2765",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8612122",
        "name": "Turkey Run Ranch",
        "location": "Solon, IA",
        "state": "IA",
        "latitude": 41.8071388888889,
        "longitude": -91.49275,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "763-453-2004",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-161585",
        "name": "Pinoak Acres",
        "location": "Walnut, IA",
        "state": "IA",
        "latitude": 41.4043055555556,
        "longitude": -95.2423055555556,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "712-250-1431",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6735573",
        "name": "Phalen Equestrian Center",
        "location": "Bonner Springs, KS",
        "state": "KS",
        "latitude": 39.080278,
        "longitude": -94.946056,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "913-424-3164",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1310767",
        "name": "Pretty-Horses.com",
        "location": "Chapman, KS",
        "state": "KS",
        "latitude": 38.97346,
        "longitude": -97.06717,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "785-761-5900",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1698715",
        "name": "Wichita Riding Academy, Inc.",
        "location": "Derby, KS",
        "state": "KS",
        "latitude": 37.61946,
        "longitude": -97.21298,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "316-651-0876",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9890262",
        "name": "Middle of the Map Horse Hotel",
        "location": "Edgerton, KS",
        "state": "KS",
        "latitude": 38.764929,
        "longitude": -95.010778,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "913-617-4396",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8248912",
        "name": "SlideonInn Horse Hotel, LLC",
        "location": "Edson, KS",
        "state": "KS",
        "latitude": 39.3391,
        "longitude": -101.58396,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "785-821-0199",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4928552",
        "name": "Five Point Horse Training /",
        "location": "Goodland, KS",
        "state": "KS",
        "latitude": 39.2730833333333,
        "longitude": -101.893583333333,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "303-968-7345",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3362917",
        "name": "Branded 5 Stable",
        "location": "Hays, KS",
        "state": "KS",
        "latitude": 38.895725,
        "longitude": -99.410457,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "785-623-9566",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4545582",
        "name": "Triple Eight Equine Center",
        "location": "Hays, KS",
        "state": "KS",
        "latitude": 38.8826388888889,
        "longitude": -99.2435833333333,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "785-259-1271",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-228804",
        "name": "North Fork Horse Motel",
        "location": "Oakley, KS",
        "state": "KS",
        "latitude": 39.191234,
        "longitude": -100.876983,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "785-672-0950",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6857605",
        "name": "RC Arena & Stables",
        "location": "Park City, KS",
        "state": "KS",
        "latitude": 37.804944,
        "longitude": -97.328806,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "316-807-6140",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7823879",
        "name": "Woods Creek Ranch",
        "location": "Silver Lake/Topeka, KS",
        "state": "KS",
        "latitude": 39.11625,
        "longitude": -95.85375,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "785-250-1843",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4106973",
        "name": "Hadden Equine Inn",
        "location": "South Haven, KS",
        "state": "KS",
        "latitude": 37.0491388888889,
        "longitude": -97.404,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "620-326-3001",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6918009",
        "name": "Two Pond Farm",
        "location": "Topeka, KS",
        "state": "KS",
        "latitude": 39.0646111111111,
        "longitude": -95.7066111111111,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "907-419-5569",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8864423",
        "name": "Wichita Stable Stop",
        "location": "Valley Center, KS",
        "state": "KS",
        "latitude": 37.86425,
        "longitude": -97.31769,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "512-961-9482",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5883858",
        "name": "Heartland Ranch",
        "location": "Wakarusa, KS",
        "state": "KS",
        "latitude": 38.89526,
        "longitude": -95.71906,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "785-969-9139",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4758475",
        "name": "Broken Spoke Stables",
        "location": "Winfield, KS",
        "state": "KS",
        "latitude": 37.239985,
        "longitude": -97.010044,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "620-229-4066",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9952974",
        "name": "Twin Pond Stables",
        "location": "Bloomfield, KY",
        "state": "KY",
        "latitude": 37.8635555555556,
        "longitude": -85.3326666666667,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "559-765-5005",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2781150",
        "name": "Misty Ridge Farm, LLC",
        "location": "Camp Springs, KY",
        "state": "KY",
        "latitude": 39.03162,
        "longitude": -84.39226,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "859-802-1050",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4370463",
        "name": "Carey Horse Farm",
        "location": "Cave City, KY",
        "state": "KY",
        "latitude": 37.1568055555556,
        "longitude": -85.9742777777778,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "270-646-7424",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6779693",
        "name": "The Farm at Grace Hill",
        "location": "Crestwood, KY",
        "state": "KY",
        "latitude": 38.324284,
        "longitude": -85.471511,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "502-376-3573",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-476277",
        "name": "Robyn's Nest Farm",
        "location": "Georgetown, KY",
        "state": "KY",
        "latitude": 38.2413333333333,
        "longitude": -84.45275,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "859-379-4980",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7387983",
        "name": "Muhlenberg County Agriculture Center",
        "location": "Greenville, KY",
        "state": "KY",
        "latitude": 37.234426,
        "longitude": -87.151633,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "270-820-7246",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3084311",
        "name": "Hang 'Em High Horse Camp",
        "location": "London, KY",
        "state": "KY",
        "latitude": 37.116871,
        "longitude": -84.190452,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "606-682-3370",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4502572",
        "name": "Hidden Creek Farm",
        "location": "Monticello, KY",
        "state": "KY",
        "latitude": 36.838922,
        "longitude": -84.872777,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "606-278-2600",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8554944",
        "name": "Three16 Ranch",
        "location": "Owenton, KY",
        "state": "KY",
        "latitude": 38.5934166666667,
        "longitude": -84.8185,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "859-240-5386",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6283048",
        "name": "The Art Farm Women\u2019s Retreat",
        "location": "Paducah, KY",
        "state": "KY",
        "latitude": 37.016184,
        "longitude": -88.780149,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "270-217-2170",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2390446",
        "name": "Painted Pastures Farm",
        "location": "Paducah, KY",
        "state": "KY",
        "latitude": 36.9820555555556,
        "longitude": -88.7081944444444,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "931-209-1094",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5454353",
        "name": "Manestream Equestrian and Livestock",
        "location": "Shelbyville, KY",
        "state": "KY",
        "latitude": 38.2196111111111,
        "longitude": -85.0819166666667,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "480-283-3664",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4387344",
        "name": "K BAR C West Camp",
        "location": "Versailles, KY",
        "state": "KY",
        "latitude": 38.0050277777778,
        "longitude": -84.7166388888889,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "859-797-4368",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3808980",
        "name": "K BAR C RANCH",
        "location": "Wilmore, KY",
        "state": "KY",
        "latitude": 37.90088,
        "longitude": -84.67219,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "859-797-4368",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3689575",
        "name": "4-E Horse Motel",
        "location": "Bossier City, LA",
        "state": "LA",
        "latitude": 32.60467,
        "longitude": -93.69485,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "318-458-6837",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8255125",
        "name": "Pit Stop Pastures",
        "location": "Calhoun, LA",
        "state": "LA",
        "latitude": 32.4953611111111,
        "longitude": -92.3688611111111,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "318-512-5901",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7413443",
        "name": "Cypress Creek Farm",
        "location": "Dubach, LA",
        "state": "LA",
        "latitude": 32.61726,
        "longitude": -92.6507,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "318-243-2910",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3004805",
        "name": "Reposo Farm & Feeds, LLC",
        "location": "Folsom, LA",
        "state": "LA",
        "latitude": 30.59656,
        "longitude": -90.214369,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "985-966-5591",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7632675",
        "name": "The Stables at Le Bocage",
        "location": "Lake Charles, LA",
        "state": "LA",
        "latitude": 30.142237,
        "longitude": -93.202118,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "337-842-7434",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7315062",
        "name": "Stable 31",
        "location": "LA-Opelousas",
        "state": "",
        "latitude": 30.5025,
        "longitude": -92.0448333333333,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "225-456-1303",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8805234",
        "name": "Marecage Stables",
        "location": "LA -\u00a0Ponchatoula",
        "state": "",
        "latitude": 30.4214444444444,
        "longitude": -90.4176666666667,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "985-974-1005",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9368152",
        "name": "Northshore Riding club, LLC",
        "location": "Slidell, LA",
        "state": "LA",
        "latitude": 30.25631,
        "longitude": -89.74425,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "504-578-6262",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9608100",
        "name": "2 Lazy 2 Stables, LLC",
        "location": "Walker, LA",
        "state": "LA",
        "latitude": 30.55757,
        "longitude": -90.86748,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "225-247-2002",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-163741",
        "name": "Hidden Valley Equestrian Center, Inc.",
        "location": "Cornville, ME",
        "state": "ME",
        "latitude": 44.823088,
        "longitude": -69.701811,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "207-474-3014",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5851512",
        "name": "Tamarack Farm",
        "location": "Greenwood, ME",
        "state": "ME",
        "latitude": 44.3478055555556,
        "longitude": -70.6619444444444,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "207-205-1268",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4136034",
        "name": "Camden Hills Equine Inn",
        "location": "Lincolnville, ME",
        "state": "ME",
        "latitude": 44.28494,
        "longitude": -69.03375,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "207-542-9660",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5317805",
        "name": "Campbell Horse Farm",
        "location": "Littleton, ME",
        "state": "ME",
        "latitude": 46.24175,
        "longitude": -67.8152777777778,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "207-532-8693",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1739061",
        "name": "Taildragger Stables, LLC",
        "location": "New Gloucester, ME",
        "state": "ME",
        "latitude": 43.98123,
        "longitude": -70.25948,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "207-595-0948",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6046490",
        "name": "Free Rein Farm, LLC",
        "location": "Gaithersburg, MD",
        "state": "MD",
        "latitude": 39.203472,
        "longitude": -77.100944,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "617-901-1465",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6453495",
        "name": "GemStone Stable",
        "location": "Hagerstown, MD",
        "state": "MD",
        "latitude": 39.718821,
        "longitude": -77.801077,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "717-729-2767",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5892515",
        "name": "Valley Meadow Farms Trails Getaway and Bed & Barn",
        "location": "Hancock, MD",
        "state": "MD",
        "latitude": 39.719165,
        "longitude": -78.304515,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "301-678-6584",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4690837",
        "name": "Fairwinds Farm",
        "location": "North East, MD",
        "state": "MD",
        "latitude": 39.68393,
        "longitude": -75.96813,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "410-658-8187",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3448177",
        "name": "Karen Brooks",
        "location": "Rising Sun, MD",
        "state": "MD",
        "latitude": 39.711983,
        "longitude": -76.044915,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "443-945-6761",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8144292",
        "name": "Ironstar Outpost East",
        "location": "Sykesville, MD",
        "state": "MD",
        "latitude": 39.341088,
        "longitude": -77.000529,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "443-676-3811",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5843678",
        "name": "Melissa Harlinski",
        "location": "Upper Marlboro, MD",
        "state": "MD",
        "latitude": 38.889564,
        "longitude": -76.699078,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "443-690-2525",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5385762",
        "name": "Muddy Boots Horse Farm and Rescue",
        "location": "Warwick, MD",
        "state": "MD",
        "latitude": 39.4774722222222,
        "longitude": -75.7759444444444,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "302-650-0197",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4379847",
        "name": "A HORSE HOTEL AT EQUINE BLVD",
        "location": "Agawam, MA",
        "state": "MA",
        "latitude": 42.043416,
        "longitude": -72.62931,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "413-246-0680",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3837925",
        "name": "Dream Spirit Farm, LLC",
        "location": "Amesbury, MA",
        "state": "MA",
        "latitude": 42.872285,
        "longitude": -70.939504,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "978-834-3013",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8307722",
        "name": "Hidden Creek Homestead",
        "location": "Brimfield, MA",
        "state": "MA",
        "latitude": 42.16625,
        "longitude": -72.2203333333333,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "774-573-0656",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3946807",
        "name": "Golden Moon Farm",
        "location": "Douglas, MA",
        "state": "MA",
        "latitude": 42.035389,
        "longitude": -71.706694,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "203-685-3050",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4249712",
        "name": "Ipswich River Lodge",
        "location": "Ipswitch, MA",
        "state": "MA",
        "latitude": 42.66225,
        "longitude": -70.8412777777778,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "978-212-9499",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8680113",
        "name": "Silvershell Inn",
        "location": "Marion, MA",
        "state": "MA",
        "latitude": 41.722052,
        "longitude": -70.774521,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "508-299-9810",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9326732",
        "name": "Huntersbrook Stable",
        "location": "Norwell, MA",
        "state": "MA",
        "latitude": 42.153302,
        "longitude": -70.798703,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "781-883-7090",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4687192",
        "name": "Oak Lane Equestrian",
        "location": "Bridgeport, MI",
        "state": "MI",
        "latitude": 43.3217222222222,
        "longitude": -83.8374444444444,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "716-997-2019",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3894901",
        "name": "Duke Farms",
        "location": "Temperance, MI",
        "state": "MI",
        "latitude": 41.8101388888889,
        "longitude": -83.5503888888889,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "419-410-7193",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9210359",
        "name": "Circle J Horse Farm, LLC",
        "location": "White Pigeon, MI",
        "state": "MI",
        "latitude": 41.7657,
        "longitude": -85.76718,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "269-816-7898",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9054989",
        "name": "Oak Acres Horse Boarding",
        "location": "Albert Lea, MN",
        "state": "MN",
        "latitude": 43.612373,
        "longitude": -93.348449,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "507-391-2260",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9615442",
        "name": "Winter Shows Arena",
        "location": "Crookston, MN",
        "state": "MN",
        "latitude": 47.7914166666667,
        "longitude": -96.6116388888889,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "701-720-6660",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3956368",
        "name": "Old 27 Ranch",
        "location": "Fergus Falls, MN",
        "state": "MN",
        "latitude": 46.3030277777778,
        "longitude": -96.0664722222222,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "218-731-9328",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3654690",
        "name": "Outback Ranch, Inc.",
        "location": "Houston, MN",
        "state": "MN",
        "latitude": 43.736847,
        "longitude": -91.611206,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "507-896-5550",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1407823",
        "name": "Clearview Ranch",
        "location": "Jackson, MN",
        "state": "MN",
        "latitude": 43.6303055555556,
        "longitude": -95.0903333333333,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "507-840-0345",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4530089",
        "name": "Bryan Stading",
        "location": "Lake Crystal, MN",
        "state": "MN",
        "latitude": 44.155728,
        "longitude": -94.349236,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "507-382-5679",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6054162",
        "name": "Faus Farms",
        "location": "Lake Park, MN",
        "state": "MN",
        "latitude": 46.9640833333333,
        "longitude": -96.1270555555555,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "218-298-2682",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6267027",
        "name": "Rock Hill Ranch",
        "location": "Milaca, MN",
        "state": "MN",
        "latitude": 45.7723888888889,
        "longitude": -93.55625,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "763-482-9062",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1592576",
        "name": "Cardinal Crest Farm, LLC",
        "location": "Monticello, MN",
        "state": "MN",
        "latitude": 45.29273,
        "longitude": -93.72546,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "612-597-8319",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8533283",
        "name": "3 GEMS TACK & STABLES, LLC",
        "location": "Moorhead, MN",
        "state": "MN",
        "latitude": 46.970189,
        "longitude": -96.802829,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "701-306-4478",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4188422",
        "name": "Moon Lake Farm",
        "location": "Belden, MS",
        "state": "MS",
        "latitude": 34.30808,
        "longitude": -88.837989,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "662-205-0660",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1445504",
        "name": "Green Woods Stables RV Park",
        "location": "Biloxi, MS",
        "state": "MS",
        "latitude": 30.46723,
        "longitude": -88.875069,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "228-297-5557",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6944962",
        "name": "Silver Creek Equestrian",
        "location": "MS -\u00a0Bovina",
        "state": "",
        "latitude": 32.314472,
        "longitude": -90.733667,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "601-618-8826",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-731629",
        "name": "Band-Aid Barn, LLC",
        "location": "Carriere, MS",
        "state": "MS",
        "latitude": 30.617501,
        "longitude": -89.650771,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "504-481-1137",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3407385",
        "name": "Bridgeport Farm",
        "location": "Edwards, MS",
        "state": "MS",
        "latitude": 32.36883,
        "longitude": -90.62859,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "601-953-8038",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4608495",
        "name": "Terrebonne Farm",
        "location": "Edwards, MS",
        "state": "MS",
        "latitude": 32.314925,
        "longitude": -90.61408,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "601-831-5194",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4485025",
        "name": "Golden Horse Stables",
        "location": "Florence, MS",
        "state": "MS",
        "latitude": 32.13508,
        "longitude": -90.1612,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "601-845-2093",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3976996",
        "name": "Sumrall Stables",
        "location": "Gulfport, MS",
        "state": "MS",
        "latitude": 30.4882222222222,
        "longitude": -89.1448611111111,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "225-278-8934",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1634186",
        "name": "Lucky 4 Farm",
        "location": "Hernando, MS",
        "state": "MS",
        "latitude": 34.74998,
        "longitude": -89.99443,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "662-910-9506",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2682619",
        "name": "Stable Place",
        "location": "Lucedale, MS",
        "state": "MS",
        "latitude": 30.9088611111111,
        "longitude": -88.4779722222222,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "251-599-2825",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9176174",
        "name": "Raintree Equestrian Center",
        "location": "Olive Branch, MS",
        "state": "MS",
        "latitude": 34.992916,
        "longitude": -89.861854,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "901-857-4074",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9536014",
        "name": "KMC Stables",
        "location": "Pass Christian, MS",
        "state": "MS",
        "latitude": 30.38528,
        "longitude": -89.2854,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "228-760-6351",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9215356",
        "name": "Big Hat Ranch MS, LLC",
        "location": "Perkinston, MS",
        "state": "MS",
        "latitude": 30.7283055555556,
        "longitude": -88.7785,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "228-243-4048",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9121331",
        "name": "Horse Haven Ranch",
        "location": "Poplarville, MS",
        "state": "MS",
        "latitude": 30.9168611111111,
        "longitude": -89.5366666666667,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "601-746-7460",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3510438",
        "name": "Blackwood Stables",
        "location": "Tupelo, MS",
        "state": "MS",
        "latitude": 34.2742777777778,
        "longitude": -88.6739166666667,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "662-260-3282",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3778286",
        "name": "Windmill Trails",
        "location": "Tylertown, MS",
        "state": "MS",
        "latitude": 31.01975,
        "longitude": -90.0526111111111,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "225-278-8934",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-278363",
        "name": "Sandhill Ranch",
        "location": "Vancleave, MS",
        "state": "MS",
        "latitude": 30.531,
        "longitude": -88.6476388888889,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "228-697-2120",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8561074",
        "name": "Duke's Bed & Board",
        "location": "Americus, MO",
        "state": "MO",
        "latitude": 38.78249,
        "longitude": -91.56325,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "573-252-4880",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8999138",
        "name": "Easy E Horse Hotel & Bunkhouse",
        "location": "Archie, MO",
        "state": "MO",
        "latitude": 38.5026388888889,
        "longitude": -94.2964166666667,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "816-304-3533",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8677644",
        "name": "Circle B Ranch",
        "location": "Big Spring, MO",
        "state": "MO",
        "latitude": 38.91032,
        "longitude": -91.44821,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "314-803-6279",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7340122",
        "name": "Kirkland Performance Horses",
        "location": "Buckner, MO",
        "state": "MO",
        "latitude": 39.135814,
        "longitude": -94.256514,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "816-529-3543",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5732230",
        "name": "Civil War Ranch and Arena",
        "location": "Carthage, MO",
        "state": "MO",
        "latitude": 37.225451,
        "longitude": -94.32589,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "417-237-0771",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7575886",
        "name": "Lazy S Overnights & Boarding",
        "location": "Carthage, MO",
        "state": "MO",
        "latitude": 37.238256,
        "longitude": -94.289619,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "417-388-0303",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1712852",
        "name": "Sunset Hill Stable, LLC",
        "location": "Country Club, MO",
        "state": "MO",
        "latitude": 39.85197,
        "longitude": -94.81581,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "816-355-4141",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2181925",
        "name": "JG Arena",
        "location": "Cuba, MO",
        "state": "MO",
        "latitude": 38.043472,
        "longitude": -91.496694,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "314-803-2516",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1871546",
        "name": "Story\u2019s Creek Campground",
        "location": "Eminence, MO",
        "state": "MO",
        "latitude": 37.162988,
        "longitude": -91.375064,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "573-276-8881",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3023431",
        "name": "Sunny Ridge Stables",
        "location": "Fulton, MO",
        "state": "MO",
        "latitude": 38.907972,
        "longitude": -92.130361,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "417-664-7949",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7908418",
        "name": "My Epiphany Stables",
        "location": "Hamilton, MO",
        "state": "MO",
        "latitude": 39.699548,
        "longitude": -94.093084,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "262-909-0628",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8884755",
        "name": "M Double O Horse Ranch, LLC",
        "location": "Hollister, MO",
        "state": "MO",
        "latitude": 36.538433,
        "longitude": -93.167249,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "417 598-8084",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3815296",
        "name": "GauxPro Performance Horses",
        "location": "Joplin, MO",
        "state": "MO",
        "latitude": 37.101272,
        "longitude": -94.585028,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "574-360-6868",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3957539",
        "name": "Newts Nodaway",
        "location": "Leasburg, MO",
        "state": "MO",
        "latitude": 38.09199,
        "longitude": -91.29653,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "573-915-9750",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4810558",
        "name": "JAWsome Farm",
        "location": "Lebanon, MO",
        "state": "MO",
        "latitude": 37.6812777777778,
        "longitude": -92.5862777777778,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "417-588-5145",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1076500",
        "name": "Triangle A Arena",
        "location": "Leeton, MO",
        "state": "MO",
        "latitude": 38.5686111111111,
        "longitude": -93.7456666666667,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "816-288-3507",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3920071",
        "name": "Buckskins & Blondes",
        "location": "Montgomery City, MO",
        "state": "MO",
        "latitude": 38.9403611111111,
        "longitude": -91.50775,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "636-377-9554",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2161350",
        "name": "Heritage Livestock",
        "location": "Mount Vernon, MO",
        "state": "MO",
        "latitude": 37.113259,
        "longitude": -93.764791,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "417-316-0119",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4829773",
        "name": "Diamond M Ranch, LLC",
        "location": "Perry, MO",
        "state": "MO",
        "latitude": 39.481,
        "longitude": -91.6861944444445,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "573-822-2920",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2179096",
        "name": "A Bit of Heaven Equestrian Center",
        "location": "Rolla, MO",
        "state": "MO",
        "latitude": 37.90826,
        "longitude": -91.74219,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "573-202-8262",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2641913",
        "name": "Ozark crossing RV Park and cabin",
        "location": "Rolla, MO",
        "state": "MO",
        "latitude": 37.9292222222222,
        "longitude": -91.8158888888889,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "573-465-3976",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8180309",
        "name": "Pearl Drive Stables",
        "location": "Saint Peters, MO",
        "state": "MO",
        "latitude": 38.8151944444444,
        "longitude": -90.6538333333333,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "636-474-9190",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4384470",
        "name": "WingNit Farms",
        "location": "Stotts City, MO",
        "state": "MO",
        "latitude": 37.07525,
        "longitude": -93.9372777777778,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "661-378-6272",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7011666",
        "name": "Tarkio Rodeo",
        "location": "Tarkio, MO",
        "state": "MO",
        "latitude": 40.4374591,
        "longitude": -95.3876828,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "660-253-0260",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5237690",
        "name": "Iron Hill Ranch",
        "location": "Union, MO",
        "state": "MO",
        "latitude": 38.385389,
        "longitude": -90.931694,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "314-814-2774",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7579414",
        "name": "EQUINE MOTEL - Box T Ranch",
        "location": "Billings, MT",
        "state": "MT",
        "latitude": 45.812063,
        "longitude": -108.400862,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "406-252-4388",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6158539",
        "name": "Hoods Western Adventures",
        "location": "Alberton, MT",
        "state": "MT",
        "latitude": 47.00041,
        "longitude": -114.59244,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "406-799-3173",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1281853",
        "name": "VanGoBoon Bozeman",
        "location": "Bozeman, MT",
        "state": "MT",
        "latitude": 45.659218,
        "longitude": -110.95133,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "406-580-7476",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6684077",
        "name": "Baker Horse Ranch",
        "location": "Bozeman, MT",
        "state": "MT",
        "latitude": 45.7083055555556,
        "longitude": -111.218694444444,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "406-579-6630",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-727254",
        "name": "Cornerstone Equine, LLC",
        "location": "Butte, MT",
        "state": "MT",
        "latitude": 45.980697,
        "longitude": -112.507501,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "406-600-2937",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6467172",
        "name": "The Paard Yard, LLC",
        "location": "Cascade, MT",
        "state": "MT",
        "latitude": 47.272715,
        "longitude": -111.700138,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "334-300-6002",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1520172",
        "name": "Mitchell Farm",
        "location": "Columbia Falls, MT",
        "state": "MT",
        "latitude": 48.2897777777778,
        "longitude": -114.183333333333,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "406-871-3792",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3842685",
        "name": "Mountain View Arena",
        "location": "Deer Lodge, MT",
        "state": "MT",
        "latitude": 46.40977,
        "longitude": -112.72042,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "406-846-3686",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2926052",
        "name": "High Prairie Akers",
        "location": "Ennis, MT",
        "state": "MT",
        "latitude": 45.211957,
        "longitude": -111.784461,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "970-215-2659",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4974720",
        "name": "Ennis Horse Motel",
        "location": "Ennis, MT",
        "state": "MT",
        "latitude": 45.20971,
        "longitude": -111.77366,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "901-652-1615",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8036627",
        "name": "Maple Mounten Stables, LLC",
        "location": "Florence, MT",
        "state": "MT",
        "latitude": 46.68585,
        "longitude": -114.08469,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "406-493-0431",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-927618",
        "name": "Hummingbird Ranch, LLC",
        "location": "Helena, MT",
        "state": "MT",
        "latitude": 46.66825,
        "longitude": -112.050305555556,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "406-502-8528",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4365819",
        "name": "Rest Awhile Ranch",
        "location": "Kalispell, MT",
        "state": "MT",
        "latitude": 48.1341666666667,
        "longitude": -114.180361111111,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "909-224-3191",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6894162",
        "name": "NFR, Nisbet Family Ranch",
        "location": "Libby, MT",
        "state": "MT",
        "latitude": 48.282,
        "longitude": -115.474638888889,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "805-338-9011",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6478320",
        "name": "Wineglass Ranch",
        "location": "Livingston, MT",
        "state": "MT",
        "latitude": 45.6245,
        "longitude": -110.589111111111,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "406-224-0658",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9039072",
        "name": "GRIZZLY ROCK RANCH",
        "location": "Manhattan, MT",
        "state": "MT",
        "latitude": 45.9413333333333,
        "longitude": -111.295,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "406-599-5690",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5972249",
        "name": "Schweigert Performance Horses, LLC",
        "location": "Miles City, MT",
        "state": "MT",
        "latitude": 46.4263333333333,
        "longitude": -105.813388888889,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "208-861-6811",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7133467",
        "name": "Grass Valley Stable",
        "location": "Missoula, MT",
        "state": "MT",
        "latitude": 46.9089722222222,
        "longitude": -114.130305555556,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "406-239-2484",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2598117",
        "name": "Kings Grace Restorative Ranch",
        "location": "Polson, MT",
        "state": "MT",
        "latitude": 47.6313888888889,
        "longitude": -114.259305555556,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "406-885-0319",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9797642",
        "name": "Rocking H Ranch",
        "location": "Ramsay, MT",
        "state": "MT",
        "latitude": 46.04473,
        "longitude": -112.75904,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "406-209-7302",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-213628",
        "name": "Red Lodge Horse Motel",
        "location": "Roberts, MT",
        "state": "MT",
        "latitude": 45.23675,
        "longitude": -109.227027777778,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "406-579-8605",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3996872",
        "name": "Trout Creek Guest House & Horse Hotel",
        "location": "Trout Creek, MT",
        "state": "MT",
        "latitude": 47.728065,
        "longitude": -115.448468,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "406-827-5240",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8257296",
        "name": "406 Arena",
        "location": "Vaughn, MT",
        "state": "MT",
        "latitude": 47.55345,
        "longitude": -111.56476,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "406-590-8682",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1617667",
        "name": "Diamond P Ranch Horse Motel",
        "location": "West Yellowstone, MT",
        "state": "MT",
        "latitude": 44.686282,
        "longitude": -111.225601,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "406-646-0606",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2309848",
        "name": "Bailey's Bed'n Bale LLC",
        "location": "Whitefish, MT",
        "state": "MT",
        "latitude": 48.4281111111111,
        "longitude": -114.291222222222,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "406-270-1603",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2410945",
        "name": "Whitefish Horse Motel",
        "location": "Whitefish, MT",
        "state": "MT",
        "latitude": 48.3389166666667,
        "longitude": -114.341333333333,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "406-260-8725",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8749806",
        "name": "Bronco Lake Stables",
        "location": "Alliance, NE",
        "state": "NE",
        "latitude": 42.0745,
        "longitude": -102.941111111111,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "270-307-8852",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8042755",
        "name": "NE - Aurora",
        "location": "Aurora, NE",
        "state": "NE",
        "latitude": 40.85722,
        "longitude": -98.01351,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "402-631-1401",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2739872",
        "name": "Triple E Equine, LLC",
        "location": "Beaver Crossing, NE",
        "state": "NE",
        "latitude": 40.795539,
        "longitude": -97.216261,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "402-643-8018",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4006182",
        "name": "HQH Stables & Campsite",
        "location": "Big Springs, NE",
        "state": "NE",
        "latitude": 41.06301,
        "longitude": -102.07402,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "308-889-3538",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1666866",
        "name": "RUJODEN Ranch Horse Motel",
        "location": "Chadron, NE",
        "state": "NE",
        "latitude": 42.756794,
        "longitude": -103.009268,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "308-432-5954",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8241824",
        "name": "Frederick Ranch",
        "location": "Doniphan, NE",
        "state": "NE",
        "latitude": 40.81271,
        "longitude": -98.264184,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "308-379-5349",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6449253",
        "name": "J Bar D Ranch",
        "location": "Geneva, NE",
        "state": "NE",
        "latitude": 40.540305,
        "longitude": -97.595972,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "402-759-3095",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1450011",
        "name": "Lazy K Arena",
        "location": "Grand Island, NE",
        "state": "NE",
        "latitude": 40.8471666666667,
        "longitude": -98.3401944444444,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "308-208-2020",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4308819",
        "name": "Lazy A Ranch",
        "location": "Hay Springs, NE",
        "state": "NE",
        "latitude": 42.687167,
        "longitude": -102.773722,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "308-430-0969",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2055219",
        "name": "Dawson County Fairgrounds",
        "location": "Lexington, NE",
        "state": "NE",
        "latitude": 40.768194,
        "longitude": -99.738972,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "308-324-3600",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8151928",
        "name": "K/B Stables",
        "location": "Lincoln, NE",
        "state": "NE",
        "latitude": 40.87286,
        "longitude": -96.58471,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "402-465-5855",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2637477",
        "name": "Diesel Rodeo Productions",
        "location": "Nebraska City, NE",
        "state": "NE",
        "latitude": 40.5894722222222,
        "longitude": -95.9343333333333,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "402-305-2488",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7111869",
        "name": "Husker Horse Hotel",
        "location": "Norfolk, NE",
        "state": "NE",
        "latitude": 42.119843,
        "longitude": -97.405929,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "402-649-2263",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2234414",
        "name": "Jump Rope Stables, LLC",
        "location": "Ogallala, NE",
        "state": "NE",
        "latitude": 41.091004,
        "longitude": -101.763502,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "308-289-1861",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5457302",
        "name": "Tumbleweed Stables",
        "location": "Sidney, NE",
        "state": "NE",
        "latitude": 41.047916,
        "longitude": -102.917807,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "308-249-1935",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8313683",
        "name": "I-80 Equine",
        "location": "Waverly, NE",
        "state": "NE",
        "latitude": 40.9123888888889,
        "longitude": -96.4898888888889,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "402-416-4771",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5724329",
        "name": "Diamond B, Inc.",
        "location": "York, NE",
        "state": "NE",
        "latitude": 40.91971,
        "longitude": -97.61621,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "402-362-5439",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8132546",
        "name": "Rustic Hart Ranch",
        "location": "Fernley, NV",
        "state": "NV",
        "latitude": 39.5570555555556,
        "longitude": -119.0525,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "530-524-1779",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-119356",
        "name": "Fly Again Ranch",
        "location": "Las Vegas, NV",
        "state": "NV",
        "latitude": 36.033583,
        "longitude": -115.162222,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "702-496-0987",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8883948",
        "name": "Steel Horse Ranch, LLC",
        "location": "Las Vegas, NV",
        "state": "NV",
        "latitude": 36.03779,
        "longitude": -115.16394,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "702-350-9563",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-566830",
        "name": "Hearts & Stars Ranch",
        "location": "Las Vegas, NV",
        "state": "NV",
        "latitude": 36.299642,
        "longitude": -115.228093,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "310-704-7101",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6648042",
        "name": "Ti-Rod Ranch Boarding Facility",
        "location": "Pahrump, NV",
        "state": "NV",
        "latitude": 36.1391944444444,
        "longitude": -115.971555555556,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "702-303-9149",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6279928",
        "name": "9 Brothers Ranch",
        "location": "Panaca, NV",
        "state": "NV",
        "latitude": 37.7887222222222,
        "longitude": -114.389194444444,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "806-577-9215",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8994063",
        "name": "N2 Mule Ranch",
        "location": "Reno, NV",
        "state": "NV",
        "latitude": 39.77921,
        "longitude": -119.80572,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "775-970-5425",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5269496",
        "name": "Franktown Meadows Equestrian Facility",
        "location": "Washoe Valley, NV",
        "state": "NV",
        "latitude": 39.2816388888889,
        "longitude": -119.840555555556,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "775-722-4261",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1568065",
        "name": "B-M Arena",
        "location": "Wells, NV",
        "state": "NV",
        "latitude": 41.09599,
        "longitude": -114.96971,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "775-752-3714",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5455624",
        "name": "Pingree Hill Farm",
        "location": "Auburn, NH",
        "state": "NH",
        "latitude": 42.944419,
        "longitude": -71.314019,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "603-703-2462",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1515273",
        "name": "The Spot!",
        "location": "Belmont, NH",
        "state": "NH",
        "latitude": 43.494092,
        "longitude": -71.443123,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3056915",
        "name": "Riley's Farm",
        "location": "Epping, NH",
        "state": "NH",
        "latitude": 43.05278,
        "longitude": -71.04099,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "603-793-9919",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2118517",
        "name": "Free Spirit Farm",
        "location": "Nottingham, NH",
        "state": "NH",
        "latitude": 43.1349166666667,
        "longitude": -71.0293888888889,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "781-799-7150",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4939979",
        "name": "Blairwood Farms, Inc.",
        "location": "Columbus, NJ",
        "state": "NJ",
        "latitude": 40.05016,
        "longitude": -74.71472,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "609-723-1155",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5424526",
        "name": "BDR Legacy Farm",
        "location": "Lambertville, NJ",
        "state": "NJ",
        "latitude": 40.3475833333333,
        "longitude": -74.9113888888889,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "215-939-1473",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1767020",
        "name": "TK Rest Stop",
        "location": "Monroe, NJ",
        "state": "NJ",
        "latitude": 40.3479722222222,
        "longitude": -74.4354166666667,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "908-907-6401",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7867548",
        "name": "Broken M Ranch",
        "location": "Albuquerque, NM",
        "state": "NM",
        "latitude": 35.01239,
        "longitude": -106.71768,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "505-877-9433",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5424626",
        "name": "Spur Stables, LLC",
        "location": "Albuquerque, NM",
        "state": "NM",
        "latitude": 35.014494,
        "longitude": -106.692671,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "505-382-2370",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2993700",
        "name": "Rio Grande Horse Hotel",
        "location": "Algodones, NM",
        "state": "NM",
        "latitude": 35.372714,
        "longitude": -106.49489,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "505-681-4242",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6983289",
        "name": "Open Heart G Farms",
        "location": "Anthony, NM",
        "state": "NM",
        "latitude": 32.02917,
        "longitude": -106.61523,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "915-920-5169",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2168587",
        "name": "Kiva RV Park and Horse Motel",
        "location": "Bosque, NM",
        "state": "NM",
        "latitude": 34.4155555555556,
        "longitude": -106.844138888889,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "505-274-6290",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5040686",
        "name": "Willow Wells Equine Ranch",
        "location": "Carlsbad, NM",
        "state": "NM",
        "latitude": 32.513999,
        "longitude": -104.22659,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "575-706-2794",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3555655",
        "name": "Rocking V Stable Equestrian Facilities",
        "location": "Clayton, NM",
        "state": "NM",
        "latitude": 36.45169,
        "longitude": -103.1841,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "575-760-8685",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3805688",
        "name": "Lazy KO Ranch",
        "location": "Deming, NM",
        "state": "NM",
        "latitude": 32.243222,
        "longitude": -107.517139,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "602-575-3340",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5528979",
        "name": "Lazy Sue Ranch",
        "location": "Deming, NM",
        "state": "NM",
        "latitude": 32.221327,
        "longitude": -107.538172,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "469-580-2451",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3645308",
        "name": "Sweet Melody Farm, LLC",
        "location": "Edgewood, NM",
        "state": "NM",
        "latitude": 35.111588,
        "longitude": -106.211307,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "541-490-8224",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1808347",
        "name": "Just Keep Swimming, Inc.",
        "location": "Edgewood, NM",
        "state": "NM",
        "latitude": 35.1351388888889,
        "longitude": -106.191611111111,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "850-276-4347",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1077290",
        "name": "Carter's Stables, LLC",
        "location": "Farmington, NM",
        "state": "NM",
        "latitude": 36.76071,
        "longitude": -108.13863,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "505-330-3066",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6060301",
        "name": "Slash M Slash Ranch",
        "location": "Grants, NM",
        "state": "NM",
        "latitude": 35.05997,
        "longitude": -107.887,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "505-290-7836",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9820780",
        "name": "Bar Key Horse Motel",
        "location": "Las Vegas, NM",
        "state": "NM",
        "latitude": 35.703768,
        "longitude": -105.396212,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "505-620-1040",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-793069",
        "name": "High Country Arena",
        "location": "Mayhill, NM",
        "state": "NM",
        "latitude": 32.911887,
        "longitude": -105.459419,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "575-687-2146",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7297757",
        "name": "Turquoise Corral",
        "location": "Quemado, NM",
        "state": "NM",
        "latitude": 34.337606,
        "longitude": -108.489877,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "505-629-3369",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7689474",
        "name": "That's Western",
        "location": "Ramah, NM",
        "state": "NM",
        "latitude": 35.13308,
        "longitude": -108.49174,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "505-495-8389",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5359277",
        "name": "Wellspring Ranch & Stables",
        "location": "Raton, NM",
        "state": "NM",
        "latitude": 36.89735,
        "longitude": -104.42128,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "575-707-8139",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9695119",
        "name": "Caballos de las Estrellas",
        "location": "Rodeo, NM",
        "state": "NM",
        "latitude": 31.9341111111111,
        "longitude": -109.044305555556,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "575-557-6465",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8338362",
        "name": "J BAR C HORSE MOTEL",
        "location": "Roswell, NM",
        "state": "NM",
        "latitude": 33.32206,
        "longitude": -104.50993,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "575-626-5294",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4266356",
        "name": "Rockin Horse Ranch",
        "location": "Stanley, NM",
        "state": "NM",
        "latitude": 35.06957,
        "longitude": -106.00594,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "505-301-3772",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1759830",
        "name": "Cassetta Critter Care",
        "location": "Tucumcari, NM",
        "state": "NM",
        "latitude": 35.1793,
        "longitude": -103.70812,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "603-731-4956",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1048687",
        "name": "Blaze-in-Saddle RV Park",
        "location": "Tucumcari, NM",
        "state": "NM",
        "latitude": 35.17175,
        "longitude": -103.693611111111,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "575-815-4085",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1771269",
        "name": "Timber Tree Ranch",
        "location": "Baldwinsville, NY",
        "state": "NY",
        "latitude": 43.1857222222222,
        "longitude": -76.36275,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "315-575-1951",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3191754",
        "name": "Wilkins Performance Horses",
        "location": "Bath, NY",
        "state": "NY",
        "latitude": 42.303,
        "longitude": -77.373278,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "607-661-9920",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4705580",
        "name": "Red Roan Ranch",
        "location": "Cambridge, NY",
        "state": "NY",
        "latitude": 43.035,
        "longitude": -73.28958,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "802-265-0702",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-485959",
        "name": "Boyd Family Ranch",
        "location": "Cattaraugus, NY",
        "state": "NY",
        "latitude": 42.3678055555556,
        "longitude": -78.8286111111111,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "716-713-1699",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3295393",
        "name": "Heritage Ridge Equestrian Center",
        "location": "Corfu, NY",
        "state": "NY",
        "latitude": 42.943766,
        "longitude": -78.440882,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "716 946-4163",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-928580",
        "name": "Leg Up Stables",
        "location": "Geneseo, NY",
        "state": "NY",
        "latitude": 42.828282,
        "longitude": -77.790495,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9431138",
        "name": "Loon Meadow Farm",
        "location": "Greenfield Center, NY",
        "state": "NY",
        "latitude": 43.153417,
        "longitude": -73.826889,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "518-893-6116",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1925681",
        "name": "Never Done Farm",
        "location": "Ilion, NY",
        "state": "NY",
        "latitude": 42.96527,
        "longitude": -75.07342,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "607-437-0892",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7824677",
        "name": "HH Riders Trail and Riding Club",
        "location": "Port Crane, NY",
        "state": "NY",
        "latitude": 42.2315555555556,
        "longitude": -75.7410833333333,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "607-244-2763",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3196102",
        "name": "Traditions Equine Transport- Hotel and Airbnb",
        "location": "Rushville, NY",
        "state": "NY",
        "latitude": 42.7756111111111,
        "longitude": -77.2743888888889,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "315-729-0946",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8964726",
        "name": "Corner Haven Farm",
        "location": "Trumansburg, NY",
        "state": "NY",
        "latitude": 42.53481,
        "longitude": -76.684503,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "607-387-9557",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5414384",
        "name": "KIWY Farm & Wellness Place, LLC",
        "location": "West Chazy, NY",
        "state": "NY",
        "latitude": 44.7793611111111,
        "longitude": -73.4925,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "518-645-0460",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9863732",
        "name": "5K Arena",
        "location": "Bladenboro, NC",
        "state": "NC",
        "latitude": 34.65825,
        "longitude": -78.80625,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "910-305-7874",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1006220",
        "name": "SFW FARM, LLC",
        "location": "Catawba, NC",
        "state": "NC",
        "latitude": 35.708032,
        "longitude": -81.075622,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "704-619-5436",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-30494",
        "name": "Fox Trail Farm",
        "location": "Dobson, NC",
        "state": "NC",
        "latitude": 36.440912,
        "longitude": -80.770266,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "954-309-0158",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5673418",
        "name": "Valentine Farm",
        "location": "Dunn, NC",
        "state": "NC",
        "latitude": 35.25682,
        "longitude": -78.5918,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "978-697-8596",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1403588",
        "name": "Bird's Nest Bunkies & Barn, LLC",
        "location": "Ellenboro, NC",
        "state": "NC",
        "latitude": 35.368556,
        "longitude": -81.743694,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "828-453-9816",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8875863",
        "name": "Cavvietta Quarter Horse & Cattle Co.",
        "location": "Elm City, NC",
        "state": "NC",
        "latitude": 35.750116,
        "longitude": -77.771487,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "919-669-8546",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9354789",
        "name": "Bed and Barn Farms, LLC",
        "location": "Forest City, NC",
        "state": "NC",
        "latitude": 35.28848,
        "longitude": -81.93262,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "828-248-4463",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1614279",
        "name": "Sugar Ridge Ranch",
        "location": "Frisco, NC",
        "state": "NC",
        "latitude": 35.2355833333333,
        "longitude": -75.6217222222222,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "252-489-0952",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4981182",
        "name": "Double Lee Farm",
        "location": "Hollister, NC",
        "state": "NC",
        "latitude": 36.207361,
        "longitude": -78.006611,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "252-257-1959",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2983787",
        "name": "Palomino Mountain Ranch",
        "location": "Lowgap, NC",
        "state": "NC",
        "latitude": 36.5100833333333,
        "longitude": -80.8539166666667,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "336-609-9730",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-149567",
        "name": "Kingfisher Park Equestrian",
        "location": "Mooresville, NC",
        "state": "NC",
        "latitude": 35.599445,
        "longitude": -80.721295,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "919-357-3949",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4649961",
        "name": "Noble Acres",
        "location": "Mooresville, NC",
        "state": "NC",
        "latitude": 35.6550277777778,
        "longitude": -80.8455555555555,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "704-458-7272",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1323493",
        "name": "Parkton Place",
        "location": "Parkton, NC",
        "state": "NC",
        "latitude": 34.929779,
        "longitude": -79.020903,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "910-494-5888",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9716782",
        "name": "Sally's Ark",
        "location": "Rowland, NC",
        "state": "NC",
        "latitude": 34.4862222222222,
        "longitude": -79.2539166666667,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "910-422-9955",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6535673",
        "name": "The Regal Roost",
        "location": "Southern Pines, NC",
        "state": "NC",
        "latitude": 35.1476388888889,
        "longitude": -79.3548333333333,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "910-603-3162",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6900118",
        "name": "Lil'Aspen Farm",
        "location": "Summerfield, NC",
        "state": "NC",
        "latitude": 36.2178055555556,
        "longitude": -79.8984722222222,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "336-362-8747",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5162080",
        "name": "Holbel Farm",
        "location": "Waynesville, NC",
        "state": "NC",
        "latitude": 35.4886388888889,
        "longitude": -82.9883611111111,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "231-350-0838",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5314349",
        "name": "JAKS Stables, LLC",
        "location": "Wendell, NC",
        "state": "NC",
        "latitude": 35.86656,
        "longitude": -78.446808,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "919-810-0654",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3237930",
        "name": "Lone Hickory Arena",
        "location": "Yadkinville, NC",
        "state": "NC",
        "latitude": 36.10079,
        "longitude": -80.706468,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "336-469-6097",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3996631",
        "name": "Coulee Ridge Ranch",
        "location": "Burlington, ND",
        "state": "ND",
        "latitude": 48.30129,
        "longitude": -101.5344,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "701-721-3754",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4614537",
        "name": "Heart River Stables",
        "location": "Mandan, ND",
        "state": "ND",
        "latitude": 46.82205,
        "longitude": -100.92966,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "701-226-0067",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-667124",
        "name": "Tower City Bed, Breakfast & Horse Motel",
        "location": "Tower City, ND",
        "state": "ND",
        "latitude": 46.92495,
        "longitude": -97.67722,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "701-490-2660",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-630214",
        "name": "Sunny Sugarcreek Stables",
        "location": "Bellbrook, OH",
        "state": "OH",
        "latitude": 39.6167777777778,
        "longitude": -84.0821666666667,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "937-203-6383",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7388276",
        "name": "Maple Lane Stables",
        "location": "Bluffton, OH",
        "state": "OH",
        "latitude": 40.83501,
        "longitude": -83.91135,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "419-788-1553",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4970852",
        "name": "Bellawood Farm",
        "location": "Canal Winchester, OH",
        "state": "OH",
        "latitude": 39.8058333333333,
        "longitude": -82.8425,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "614-578-7809",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-888012",
        "name": "Maple Crest Horse Farm, LLC",
        "location": "Castalia, OH",
        "state": "OH",
        "latitude": 41.35235,
        "longitude": -82.78597,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "419-684-9490",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8489789",
        "name": "Otter Fork Acres, LLC",
        "location": "Centerburg, OH",
        "state": "OH",
        "latitude": 40.2393611111111,
        "longitude": -82.6363888888889,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "432-250-1665",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3610845",
        "name": "CMA Endurance, LLC",
        "location": "Girard, OH",
        "state": "OH",
        "latitude": 41.190199,
        "longitude": -80.647367,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "503-930-6052",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7211478",
        "name": "Bit of Heaven Farm",
        "location": "Granville, OH",
        "state": "OH",
        "latitude": 40.026156,
        "longitude": -82.553598,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "614-746-3969",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4005074",
        "name": "Rolling Hills Farm and Stables, LLC",
        "location": "Heath, OH",
        "state": "OH",
        "latitude": 39.9924444444444,
        "longitude": -82.4765277777778,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "740-707-7834",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4728679",
        "name": "Ashley Moores Stables",
        "location": "Jefferson, OH",
        "state": "OH",
        "latitude": 41.759035,
        "longitude": -80.790007,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "440 576-8949",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3511423",
        "name": "Hathaway Hill",
        "location": "Lebanon, OH",
        "state": "OH",
        "latitude": 39.4363055555555,
        "longitude": -84.1704166666667,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "513-623-2289",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7701989",
        "name": "Shillelagh Farm",
        "location": "Madison, OH",
        "state": "OH",
        "latitude": 41.75097,
        "longitude": -81.11165,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "440-477-5086",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5154184",
        "name": "R/C Stables",
        "location": "Plain City, OH",
        "state": "OH",
        "latitude": 40.138505,
        "longitude": -83.191256,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "614-832-2147",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2603390",
        "name": "Plattsburg Equine Center, LLC",
        "location": "South Charleston, OH",
        "state": "OH",
        "latitude": 39.90831,
        "longitude": -83.66768,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "937-926-3355",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6614704",
        "name": "The Secret Ranch",
        "location": "Van Buren, OH",
        "state": "OH",
        "latitude": 41.1471666666667,
        "longitude": -83.6313055555555,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "267-500-5225",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5935579",
        "name": "Weathertree Farm",
        "location": "Vermilion, OH",
        "state": "OH",
        "latitude": 41.391302,
        "longitude": -82.33742,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "440-213-0748",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1648457",
        "name": "McGuire Auctioneers",
        "location": "Windham, OH",
        "state": "OH",
        "latitude": 41.24734,
        "longitude": -81.00476,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "330-348-1192",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6849559",
        "name": "I-44 Horse Hotel, LLC",
        "location": "Adair, OK",
        "state": "OK",
        "latitude": 36.437944,
        "longitude": -95.39209,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "918-323-1555",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7501241",
        "name": "Johnson Performance Horses",
        "location": "Ardmore, OK",
        "state": "OK",
        "latitude": 34.149,
        "longitude": -97.19656,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "580-276-0662",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2765362",
        "name": "Crossroads Equestrian Inn",
        "location": "Blackwell, OK",
        "state": "OK",
        "latitude": 36.7970833333333,
        "longitude": -97.2985,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "620-262-6892",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2592225",
        "name": "Crossroads Ranch Ministries",
        "location": "Depew, OK",
        "state": "OK",
        "latitude": 35.856006,
        "longitude": -96.508841,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "918-939-9085",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2046993",
        "name": "Black Horse Stables",
        "location": "Elk City, OK",
        "state": "OK",
        "latitude": 35.403686,
        "longitude": -99.417163,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "580-799-1690",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7761055",
        "name": "Cross Creek Horse Training, LLC",
        "location": "El Reno, OK",
        "state": "OK",
        "latitude": 35.507878,
        "longitude": -97.896438,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "405-831-4155",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4426046",
        "name": "Diamond D Ranch",
        "location": "Guthrie, OK",
        "state": "OK",
        "latitude": 35.77023,
        "longitude": -97.40755,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "405-245-4227",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8251390",
        "name": "Cowboy Town Stalls & Rv",
        "location": "Guthrie, OK",
        "state": "OK",
        "latitude": 35.88,
        "longitude": -97.4236388888889,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "405-612-8546",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2503176",
        "name": "KK's Horse Hotel",
        "location": "Guthrie, OK",
        "state": "OK",
        "latitude": 35.8070833333333,
        "longitude": -97.4074444444445,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "405-369-6619",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8423020",
        "name": "Harvey\u2019s Horse Pasture",
        "location": "Guthrie, OK",
        "state": "OK",
        "latitude": 35.7755833333333,
        "longitude": -97.5126666666667,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "405-291-0991",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7595963",
        "name": "Suncatcher Ranch, LLC",
        "location": "Guthrie, OK",
        "state": "OK",
        "latitude": 35.7822222222222,
        "longitude": -97.3361388888889,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "405-517-2979",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2288917",
        "name": "Johnson Ranch Horse Motel",
        "location": "Henryetta, OK",
        "state": "OK",
        "latitude": 35.4326666666667,
        "longitude": -96.0104166666667,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "903-926-2233",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1815912",
        "name": "Sleepy Deep Fork RV / Horse Hotel",
        "location": "Henryetta, OK",
        "state": "OK",
        "latitude": 35.4788055555556,
        "longitude": -95.8818611111111,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "918-521-5744",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8829072",
        "name": "Rock ANN Roll Ranch & Horse Motel",
        "location": "Hugo, OK",
        "state": "OK",
        "latitude": 34.011671,
        "longitude": -95.562715,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "715-205-0704",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8007501",
        "name": "New Harmony Ranch & Training Center",
        "location": "Inola, OK",
        "state": "OK",
        "latitude": 36.113454,
        "longitude": -95.494067,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "918-925-2227",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9188571",
        "name": "Jeff Damphouse Training Center",
        "location": "Jones, OK",
        "state": "OK",
        "latitude": 35.565585,
        "longitude": -97.355156,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "405-898-9293",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2203398",
        "name": "Knight's Rest Ranch",
        "location": "Luther, OK",
        "state": "OK",
        "latitude": 35.68004,
        "longitude": -97.2419,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "405-919-4056",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8115076",
        "name": "Lightning C Arena",
        "location": "McAlester, OK",
        "state": "OK",
        "latitude": 34.8899444444444,
        "longitude": -95.6130833333333,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "816-260-8174",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-604693",
        "name": "Burr Oak Farms",
        "location": "Norman, OK",
        "state": "OK",
        "latitude": 35.16054,
        "longitude": -97.46291,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "816-304-5015",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8778979",
        "name": "Felker Arena",
        "location": "Okemah, OK",
        "state": "OK",
        "latitude": 35.4308055555556,
        "longitude": -96.2995,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "580-334-7712",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8013273",
        "name": "Robinson\u2019s Equestrian Center",
        "location": "Oklahoma City, OK",
        "state": "OK",
        "latitude": 35.41858,
        "longitude": -97.33238,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "405-733-2443",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6249861",
        "name": "Circle H Stables",
        "location": "Oklahoma City, OK",
        "state": "OK",
        "latitude": 35.4065,
        "longitude": -97.67757,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "580-819-3588",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4437",
        "name": "McGuire Land & Cattle",
        "location": "Perry, OK",
        "state": "OK",
        "latitude": 36.2848333333333,
        "longitude": -97.4254444444445,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "580-572-8339",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2853629",
        "name": "Cross Country Stables",
        "location": "Red Oak, OK",
        "state": "OK",
        "latitude": 34.9979444444444,
        "longitude": -95.1217777777778,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "918-721-1718",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4377595",
        "name": "Rockin A Ranch",
        "location": "Roff, OK",
        "state": "OK",
        "latitude": 34.6803611111111,
        "longitude": -96.8561111111111,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "405-614-5084",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7049518",
        "name": "Circle R Horse Lodge",
        "location": "Sand Springs, OK",
        "state": "OK",
        "latitude": 36.133805,
        "longitude": -96.172487,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "918-636-2899",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8861270",
        "name": "Rolling Stones Stables & RV Park",
        "location": "Shawnee, OK",
        "state": "OK",
        "latitude": 35.39284,
        "longitude": -96.8484,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "405 318 3303",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8807324",
        "name": "Jett Barrel horses",
        "location": "Wayne, OK",
        "state": "OK",
        "latitude": 34.94705,
        "longitude": -97.37074,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "405-246-6189",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4395929",
        "name": "Little Creek Ranch",
        "location": "Ashland, OR",
        "state": "OR",
        "latitude": 42.16878,
        "longitude": -122.66257,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "541-324-8062",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3044578",
        "name": "Eastern Slope Ranch",
        "location": "Baker City, OR",
        "state": "OR",
        "latitude": 44.79958,
        "longitude": -117.92263,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "541-523-9155",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2522933",
        "name": "Christy Hartman at DeCourcey Ranch",
        "location": "Bend, OR",
        "state": "OR",
        "latitude": 44.03021,
        "longitude": -121.12246,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "503-866-8255",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1613173",
        "name": "The Barn at Marvin Garden",
        "location": "Bly, OR",
        "state": "OR",
        "latitude": 42.3982222222222,
        "longitude": -121.040555555556,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "541-771-8382",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5202333",
        "name": "Sleepy Horse Hollow, LLC",
        "location": "Central Point, OR",
        "state": "OR",
        "latitude": 42.378306,
        "longitude": -122.958784,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "541-821-5637",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2682723",
        "name": "Lazy H Ranch",
        "location": "Coos Bay, OR",
        "state": "OR",
        "latitude": 43.2740277777778,
        "longitude": -124.228222222222,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "619-992-5342",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4461125",
        "name": "Strawberry Hill Stables",
        "location": "Creswell, OR",
        "state": "OR",
        "latitude": 43.91797,
        "longitude": -123.014731,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "541-895-4370",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1256495",
        "name": "Hood River Saddle Club",
        "location": "Hood River, OR",
        "state": "OR",
        "latitude": 45.693136,
        "longitude": -121.569679,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "541-400-0061",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7247750",
        "name": "Territorial Farm Stay and Stable",
        "location": "Junction City, OR",
        "state": "OR",
        "latitude": 44.212556,
        "longitude": -123.286278,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "541-232-7702",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5306077",
        "name": "PPR Horse Boarding",
        "location": "La Grande, OR",
        "state": "OR",
        "latitude": 45.3409444444444,
        "longitude": -118.056277777778,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "541-910-4140",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5069654",
        "name": "Hat J Performance Horses and Boarding",
        "location": "Ontario, OR",
        "state": "OR",
        "latitude": 44.070046,
        "longitude": -117.006655,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "208-550-1839",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1131687",
        "name": "Gable Stables",
        "location": "Ontario, OR",
        "state": "OR",
        "latitude": 44.0683055555556,
        "longitude": -117.033222222222,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "208-602-1213",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9873642",
        "name": "Historic Kirchem Farm",
        "location": "Oregon City, OR",
        "state": "OR",
        "latitude": 45.37967,
        "longitude": -122.4598,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "503-539-8602",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9045585",
        "name": "Neigh-bors",
        "location": "Pendleton, OR",
        "state": "OR",
        "latitude": 45.679611,
        "longitude": -118.806333,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "541-310-0951",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9875504",
        "name": "Triple Crown Farms",
        "location": "Pleasant Hill, OR",
        "state": "OR",
        "latitude": 43.96324,
        "longitude": -122.91769,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "503-730-4380",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6065447",
        "name": "Horseman Haven Ranch Oregon",
        "location": "Prineville, OR",
        "state": "OR",
        "latitude": 44.3054166666667,
        "longitude": -120.860277777778,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "520-253-0732",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1056859",
        "name": "SKS Ranch",
        "location": "Riddle, OR",
        "state": "OR",
        "latitude": 42.9195277777778,
        "longitude": -123.338916666667,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "541-430-6562",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2008059",
        "name": "Stabling Solutions, LLC",
        "location": "Rogue River, OR",
        "state": "OR",
        "latitude": 42.5121388888889,
        "longitude": -123.167416666667,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "541-621-1405",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9652988",
        "name": "R & R Ranch",
        "location": "Sisters, OR",
        "state": "OR",
        "latitude": 44.27619,
        "longitude": -121.50541,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "541-241-4188",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5656074",
        "name": "M&R Stables",
        "location": "Turner, OR",
        "state": "OR",
        "latitude": 44.778,
        "longitude": -122.975611111111,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "503-409-9577",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9909256",
        "name": "Broken Arrow Ranch",
        "location": "Winston, OR",
        "state": "OR",
        "latitude": 43.10688,
        "longitude": -123.46852,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "541-219-2163",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8753905",
        "name": "Mane Retreat",
        "location": "Abbottstown, PA",
        "state": "PA",
        "latitude": 39.9021111111111,
        "longitude": -76.9860833333333,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "229-291-9656",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1774359",
        "name": "Blue Rock Stables",
        "location": "Annville, PA",
        "state": "PA",
        "latitude": 40.33816,
        "longitude": -76.56344,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "609-500-9711",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6295636",
        "name": "A Windy Ridge Farm",
        "location": "Bethel, PA",
        "state": "PA",
        "latitude": 40.49652,
        "longitude": -76.34252,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "717-228-8070",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-993400",
        "name": "Caliber Stables",
        "location": "Bird in Hand, PA",
        "state": "PA",
        "latitude": 40.0623055555556,
        "longitude": -76.2,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "717-847-5455",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8741070",
        "name": "Broken M, LLC",
        "location": "DuBois, PA",
        "state": "PA",
        "latitude": 41.1359722222222,
        "longitude": -78.6818888888889,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "814-661-3141",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-981469",
        "name": "Horses Keep Me Stable",
        "location": "Fairfield, PA",
        "state": "PA",
        "latitude": 39.7869722222222,
        "longitude": -77.3695555555556,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "717-515-3410",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4450881",
        "name": "MountainStead Farm",
        "location": "Fredericksburg, PA",
        "state": "PA",
        "latitude": 40.46775,
        "longitude": -76.4059166666667,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "717-926-5709",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9246230",
        "name": "Sun and Cricket Bed & Breakfast",
        "location": "Gibsonia, PA",
        "state": "PA",
        "latitude": 40.65349,
        "longitude": -79.88783,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "724-444-6300",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6601432",
        "name": "Bella View Farm, LLC",
        "location": "Grantville, PA",
        "state": "PA",
        "latitude": 40.388389,
        "longitude": -76.637686,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "717-880-8255",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7685026",
        "name": "Westmoreland Fairgrounds",
        "location": "Greensburg, PA",
        "state": "PA",
        "latitude": 40.229833,
        "longitude": -79.496139,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "724-423-5005",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3716783",
        "name": "Fair Haven Farms",
        "location": "Grove City, PA",
        "state": "PA",
        "latitude": 41.173576,
        "longitude": -80.152387,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "412-848-3501",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5173736",
        "name": "Mountain View Paradise",
        "location": "Harrisburg, PA",
        "state": "PA",
        "latitude": 40.3914166666667,
        "longitude": -76.7557222222222,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "717-368-3420",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2231894",
        "name": "219 Stables Inc.",
        "location": "Lake Aerial, PA",
        "state": "PA",
        "latitude": 41.378,
        "longitude": -75.3916111111111,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "570-877-1682",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7537764",
        "name": "Shady Spring Farm",
        "location": "McConnellsburg, PA",
        "state": "PA",
        "latitude": 39.87613,
        "longitude": -78.00401,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "571-215-3900",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8017558",
        "name": "County Seat Horse Farm, LLC",
        "location": "Milford, PA",
        "state": "PA",
        "latitude": 41.3221388888889,
        "longitude": -74.8035277777778,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "570-229-1887",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9467902",
        "name": "DeCarlo Farm",
        "location": "New Haven, PA",
        "state": "PA",
        "latitude": 39.930243,
        "longitude": -79.832571,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "724-323-6638",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6428270",
        "name": "Fox Run Equine",
        "location": "Paradise, PA",
        "state": "PA",
        "latitude": 39.984,
        "longitude": -76.0828055555556,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "717-517-6691",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1521623",
        "name": "Shadow Facs Farm",
        "location": "Waterford, PA",
        "state": "PA",
        "latitude": 41.944413,
        "longitude": -79.985653,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "814-392-5055",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2830375",
        "name": "TMR Ranch",
        "location": "Woodland, PA",
        "state": "PA",
        "latitude": 41.033205,
        "longitude": -78.350121,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "814-577-0160",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6128162",
        "name": "Heritage Breed Farm",
        "location": "York, PA",
        "state": "PA",
        "latitude": 40.053925,
        "longitude": -76.782075,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "717-324-8341",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2184645",
        "name": "Y Legacy Farms",
        "location": "Aiken, SC",
        "state": "SC",
        "latitude": 33.5371111111111,
        "longitude": -81.5934722222222,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "706-755-3720",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8214149",
        "name": "A Florence Horse Center",
        "location": "Florence, SC",
        "state": "SC",
        "latitude": 34.22824,
        "longitude": -79.84847,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "843-679-5502",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8880837",
        "name": "Turtle Cove Farm",
        "location": "Irmo, SC",
        "state": "SC",
        "latitude": 34.1088055555556,
        "longitude": -81.2313055555556,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "803-463-9119",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1994713",
        "name": "Delta's Bed & Bale",
        "location": "Liberty, SC",
        "state": "SC",
        "latitude": 34.819218,
        "longitude": -82.75163,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "843-364-5015",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5865998",
        "name": "Patient Wait Farms",
        "location": "Piedmont, SC",
        "state": "SC",
        "latitude": 34.6887777777778,
        "longitude": -82.5382222222222,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "864-947-8880",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1129426",
        "name": "WC Farm",
        "location": "Seabrook, SC",
        "state": "SC",
        "latitude": 32.5590555555556,
        "longitude": -80.7270555555556,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "843-476-8145",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3890352",
        "name": "Tucker Farm Rest N Ride",
        "location": "Seneca, SC",
        "state": "SC",
        "latitude": 34.5505,
        "longitude": -82.9653888888889,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "864-710-0963",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8606670",
        "name": "West View Farm",
        "location": "Townville, SC",
        "state": "SC",
        "latitude": 34.538706,
        "longitude": -82.910932,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "864-444-3646",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8174300",
        "name": "Equine Heart Healers Ranch",
        "location": "Ware Shoals, SC",
        "state": "SC",
        "latitude": 34.4493333333333,
        "longitude": -82.2256388888889,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "864-483-8202",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-49421",
        "name": "38 Special Horse Hotel",
        "location": "Alexandria, SD",
        "state": "SD",
        "latitude": 43.702218,
        "longitude": -97.776871,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "605-630-1899",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8447722",
        "name": "Three Arrows, LLC",
        "location": "Belle Fourche, SD",
        "state": "SD",
        "latitude": 44.649,
        "longitude": -103.859277777778,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "402-650-1940",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6666624",
        "name": "Prairie Hills Paddocks",
        "location": "Belle Fourche, SD",
        "state": "SD",
        "latitude": 44.6141111111111,
        "longitude": -103.872083333333,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "605-569-3929",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8196159",
        "name": "J & J Arena",
        "location": "Box Elder, SD",
        "state": "SD",
        "latitude": 44.139996,
        "longitude": -102.999337,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "605-381-1202",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5033634",
        "name": "Clicks Horse Boarding",
        "location": "Box Elder, SD",
        "state": "SD",
        "latitude": 44.139957,
        "longitude": -102.977471,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "605-923-7926",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3030654",
        "name": "Too'Shay Tack Horse Motel",
        "location": "Brookings, SD",
        "state": "SD",
        "latitude": 44.3095,
        "longitude": -96.74778,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "605-695-0812",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5921822",
        "name": "Hawk Stables",
        "location": "Brookings, SD",
        "state": "SD",
        "latitude": 44.327613,
        "longitude": -96.771228,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "605-695-5213",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4165173",
        "name": "Bellehaven Farm",
        "location": "Bellehaven, SC",
        "state": "SC",
        "latitude": 33.504095,
        "longitude": -81.47949,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "747-266-5333",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4201204",
        "name": "TC Horse Camp",
        "location": "Custer, SD",
        "state": "SD",
        "latitude": 43.673766,
        "longitude": -103.622733,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "605-673-3249",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6652998",
        "name": "Glen Erin Horse Camp",
        "location": "Custer, SD",
        "state": "SD",
        "latitude": 43.7089166666667,
        "longitude": -103.567694444444,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "850-464-7939",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3353901",
        "name": "Meadowlark",
        "location": "Florence, SD",
        "state": "SD",
        "latitude": 45.15289,
        "longitude": -97.37411,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "605-758-3060",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2808603",
        "name": "Ghost Canyon Horse Motel",
        "location": "Hermosa, SD",
        "state": "SD",
        "latitude": 43.8398,
        "longitude": -103.33469,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "605-393-7096",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7195585",
        "name": "Hobby Horse Overnight Stables",
        "location": "Mitchell, SD",
        "state": "SD",
        "latitude": 43.6873,
        "longitude": -98.03666,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "605-995-1581",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4922791",
        "name": "Diamond A Cattle Company",
        "location": "Pukwana, SD",
        "state": "SD",
        "latitude": 43.759243,
        "longitude": -99.08891,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "605-730-0455",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8943149",
        "name": "Happy Horse Haus, LLC",
        "location": "Rapid City, SD",
        "state": "SD",
        "latitude": 44.05312,
        "longitude": -103.13071,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "605-393-2577",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5915606",
        "name": "Buffalo Berry Arena, LLC",
        "location": "Sturgis, SD",
        "state": "SD",
        "latitude": 44.437582,
        "longitude": -103.584243,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "605-423-8109",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1229504",
        "name": "Badlands Edge RV",
        "location": "Wall, SD",
        "state": "SD",
        "latitude": 43.9368333333333,
        "longitude": -102.148611111111,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "605-200-1777",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4455017",
        "name": "Shady Creek Ranch",
        "location": "Arlington, TN",
        "state": "TN",
        "latitude": 35.287278,
        "longitude": -89.789639,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "901-828-3619",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2031504",
        "name": "THE RED BARN INN",
        "location": "Athens, TN",
        "state": "TN",
        "latitude": 35.4945,
        "longitude": -84.6110277777778,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "423-462-5332",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6442456",
        "name": "Circle E Guest Ranch and Campground",
        "location": "Belvidere, TN",
        "state": "TN",
        "latitude": 35.09573,
        "longitude": -86.09882,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "931-962-1790",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6485963",
        "name": "Renaissance Farm, LLC",
        "location": "Bulls Gap, TN",
        "state": "TN",
        "latitude": 36.298611,
        "longitude": -83.04725,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "401-965-1851",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9291280",
        "name": "Circle L Campsites and Stables",
        "location": "Castalian Springs, TN",
        "state": "TN",
        "latitude": 36.391799,
        "longitude": -86.30829,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "615-390-8150",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2166051",
        "name": "Quail Run Farm",
        "location": "Chattanooga, TN",
        "state": "TN",
        "latitude": 34.984633,
        "longitude": -85.396925,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "423-933-3123",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4994070",
        "name": "TN - Crossville",
        "location": "Crossville, TN",
        "state": "TN",
        "latitude": 35.95891,
        "longitude": -84.88291,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "309-826-3565",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3239194",
        "name": "BK Sports Arena",
        "location": "Culleoka, TN",
        "state": "TN",
        "latitude": 35.453966,
        "longitude": -86.938633,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "615-969-0723",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7675898",
        "name": "High Heifer Draft Farm",
        "location": "Estill Springs, TN",
        "state": "TN",
        "latitude": 35.3222777777778,
        "longitude": -86.0481388888889,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "931-619-0773",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7666541",
        "name": "Bierly Stables",
        "location": "Fairview, TN",
        "state": "TN",
        "latitude": 36.032361,
        "longitude": -87.143889,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "615-491-3302",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1746676",
        "name": "River Runs Thru It",
        "location": "Franklin, TN",
        "state": "TN",
        "latitude": 35.857196,
        "longitude": -86.966123,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "615-390-5074",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2643145",
        "name": "Iron Hide Ranch",
        "location": "Greenback, TN",
        "state": "TN",
        "latitude": 35.6836388888889,
        "longitude": -84.2301944444445,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "865-454-4985",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9264177",
        "name": "Bronson Abbay Acres",
        "location": "Hermitage, TN",
        "state": "TN",
        "latitude": 36.15772,
        "longitude": -86.57275,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "615-426-7833",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5814221",
        "name": "Journeys End Stables",
        "location": "Jackson, TN",
        "state": "TN",
        "latitude": 35.761333,
        "longitude": -88.6695,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "731-414-2784",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9014754",
        "name": "Southern Star Farm",
        "location": "Jackson, TN",
        "state": "TN",
        "latitude": 35.732139,
        "longitude": -88.631583,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "731-422-4512",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7793380",
        "name": "Serenity Manor Stables",
        "location": "Lebanon, TN",
        "state": "TN",
        "latitude": 36.1815,
        "longitude": -86.29625,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-194127",
        "name": "Rising Glory Farm",
        "location": "Lewisburg, TN",
        "state": "TN",
        "latitude": 35.538611,
        "longitude": -86.825056,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "615-957-7170",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5876751",
        "name": "Twisted South Stables",
        "location": "Manchester, TN",
        "state": "TN",
        "latitude": 35.5151388888889,
        "longitude": -86.1291666666667,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "501-762-4262",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3569139",
        "name": "McMillan Farms",
        "location": "Mosheim, TN",
        "state": "TN",
        "latitude": 36.1695833333333,
        "longitude": -82.9764722222222,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "423-823-2867",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-519138",
        "name": "KINGS RANCH TN",
        "location": "Murfreesboro, TN",
        "state": "TN",
        "latitude": 35.751236,
        "longitude": -86.288628,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "615-962-2447",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3697496",
        "name": "Gibson Girls Horse Hotel, LLC",
        "location": "Parrotsville, TN",
        "state": "TN",
        "latitude": 36.0014166666667,
        "longitude": -82.9691111111111,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "719-588-3203",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8792664",
        "name": "Twin Oaks Farm",
        "location": "Riceville, TN",
        "state": "TN",
        "latitude": 35.361041,
        "longitude": -84.690927,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "423-506-3890",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7487483",
        "name": "Pegasus Andalusians",
        "location": "Rossville, TN",
        "state": "TN",
        "latitude": 35.1724722222222,
        "longitude": -89.5561388888889,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "954-798-3205",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9879185",
        "name": "Thompson Training Center",
        "location": "Sweetwater, TN",
        "state": "TN",
        "latitude": 35.562395,
        "longitude": -84.456575,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "423-253-4993",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1516258",
        "name": "Ted Barnes Cutting Horses",
        "location": "Watertown, TN",
        "state": "TN",
        "latitude": 36.132306,
        "longitude": -86.125333,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "615-477-6655",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9854272",
        "name": "Phoenix Farm",
        "location": "Waverly, TN",
        "state": "TN",
        "latitude": 35.9887222222222,
        "longitude": -87.7620277777778,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "323-854-5036",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7403574",
        "name": "Lone Star Stables",
        "location": "Abilene, TX",
        "state": "TX",
        "latitude": 32.335638,
        "longitude": -99.789016,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "325-260-4778",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8099622",
        "name": "Knight Arabians",
        "location": "Abilene, TX",
        "state": "TX",
        "latitude": 32.272144,
        "longitude": -99.571456,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "210-313-4520",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6592954",
        "name": "Happy Tracks Horse Motel",
        "location": "Amarillo, TX",
        "state": "TX",
        "latitude": 35.19091,
        "longitude": -102.01027,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "806-352-9508",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9824950",
        "name": "Amarillo Lone Star Bed & Bale",
        "location": "Amarillo, TX",
        "state": "TX",
        "latitude": 35.16725,
        "longitude": -101.54257,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "806-674-8047",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9760701",
        "name": "Girl Scout Road Horse Hotel, LLC",
        "location": "Amarillo, TX",
        "state": "TX",
        "latitude": 35.195549,
        "longitude": -101.96523,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "575-799-5943",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7072023",
        "name": "2T Arena",
        "location": "Amarillo, TX",
        "state": "TX",
        "latitude": 35.0545277777778,
        "longitude": -101.9765,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "806-477-9955",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4146363",
        "name": "3CK Ranch",
        "location": "Amarillo, TX",
        "state": "TX",
        "latitude": 35.1874444444444,
        "longitude": -101.953388888889,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "806-703-6263",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6811816",
        "name": "Whitehedge Farm, LLC",
        "location": "Aubrey, TX",
        "state": "TX",
        "latitude": 33.29949,
        "longitude": -96.968579,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "940-594-9275",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-697016",
        "name": "Happy Horse Hotel",
        "location": "Austin/Bastrop, TX",
        "state": "TX",
        "latitude": 30.184167,
        "longitude": -97.438611,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "512-303-0589",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2744174",
        "name": "R&R Horse Hacienda, LLC",
        "location": "Bandera, TX",
        "state": "TX",
        "latitude": 29.810954,
        "longitude": -99.080738,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "903-267-9200",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6645106",
        "name": "Varsch Land and Cattle",
        "location": "Big Spring, TX",
        "state": "TX",
        "latitude": 32.2034166666667,
        "longitude": -101.55525,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "432-312-3489",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5421635",
        "name": "Rockin L Horse Motel",
        "location": "Big Spring, TX",
        "state": "TX",
        "latitude": 32.19025,
        "longitude": -101.485916666667,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "432-288-5503",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4111084",
        "name": "Davis Road Stables",
        "location": "Big Spring, TX",
        "state": "TX",
        "latitude": 32.3008611111111,
        "longitude": -101.470611111111,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "432-638-7122",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4692686",
        "name": "Road Rash Ranch on Rust Ranch Road",
        "location": "Blanco, TX",
        "state": "TX",
        "latitude": 30.0977777777778,
        "longitude": -98.4217777777778,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "512-589-6792",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8841376",
        "name": "Snowdonia Sport Horse Complex",
        "location": "Brookshire, TX",
        "state": "TX",
        "latitude": 29.864476,
        "longitude": -96.014589,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "281-460-9107",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5234879",
        "name": "Texas Cozy Rentals",
        "location": "Bryan, TX",
        "state": "TX",
        "latitude": 30.8288055555556,
        "longitude": -96.3622222222222,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "308-289-6791",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-739932",
        "name": "Keen Farms",
        "location": "Burleson, TX",
        "state": "TX",
        "latitude": 32.45875,
        "longitude": -97.3170555555556,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "817-689-1214",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2182023",
        "name": "Darren Miller Stables",
        "location": "Canyon, TX",
        "state": "TX",
        "latitude": 34.9216388888889,
        "longitude": -101.829527777778,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "970-520-2271",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-475599",
        "name": "Peach Creek Sporthorses, Inc,",
        "location": "Cleveland, TX",
        "state": "TX",
        "latitude": 30.3496666666667,
        "longitude": -95.2159166666667,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "832-405-6840",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9515997",
        "name": "La Chula Hacienda",
        "location": "Collinsville, TX",
        "state": "TX",
        "latitude": 33.53329,
        "longitude": -96.88682,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "541-420-7774",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5817695",
        "name": "HJK Stables",
        "location": "Corpus Christi, TX",
        "state": "TX",
        "latitude": 27.738121,
        "longitude": -97.5057,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "760-646-2019",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2471917",
        "name": "Blackjack Lane Equestrian",
        "location": "Dayton, TX",
        "state": "TX",
        "latitude": 29.882651,
        "longitude": -94.894119,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "281-515-8282",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1343626",
        "name": "Wild West Remuda",
        "location": "Decatur, TX",
        "state": "TX",
        "latitude": 33.25763,
        "longitude": -97.59358,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "940-389-4449",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7281994",
        "name": "Brazile Quarter Horses",
        "location": "Decatur, TX",
        "state": "TX",
        "latitude": 33.2564722222222,
        "longitude": -97.6123611111111,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "817-925-2574",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5345606",
        "name": "Rancho Del Sol Arena",
        "location": "De Kalb, TX",
        "state": "TX",
        "latitude": 33.51612,
        "longitude": -94.6085,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "605-890-2188",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6701157",
        "name": "Rockin H Ranch",
        "location": "Diana, TX",
        "state": "TX",
        "latitude": 32.680102,
        "longitude": -94.753362,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "813-618-4836",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6482040",
        "name": "4x4 Performance Horses",
        "location": "Dike, TX",
        "state": "TX",
        "latitude": 33.257712,
        "longitude": -95.475936,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "903-440-0118",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4628561",
        "name": "Cedar Grove Stables",
        "location": "Dripping Springs, TX",
        "state": "TX",
        "latitude": 30.15813,
        "longitude": -98.13388,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "512-825-9076",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3267630",
        "name": "Star Burst Ranch",
        "location": "Eastland, TX",
        "state": "TX",
        "latitude": 32.448889,
        "longitude": -98.817667,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "503-351-2085",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4765160",
        "name": "FAR WEST HORSE MOTEL",
        "location": "El Paso, TX",
        "state": "TX",
        "latitude": 31.92525,
        "longitude": -106.624555555556,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "915-494-9000",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-299204",
        "name": "Triple C Ranch",
        "location": "Emory, TX",
        "state": "TX",
        "latitude": 32.9116666666667,
        "longitude": -95.7453888888889,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "661-858-3903",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1649714",
        "name": "Rockin Horse RV & Stables",
        "location": "Fort Stockton, TX",
        "state": "TX",
        "latitude": 30.926083,
        "longitude": -102.8655,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "432-940-8940",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-168180",
        "name": "4Hearts Ranch",
        "location": "Fort Worth, TX",
        "state": "TX",
        "latitude": 32.5998055555556,
        "longitude": -97.5689722222222,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "970-580-0012",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3996411",
        "name": "Media Luna Ranch",
        "location": "Fredericksburg, TX",
        "state": "TX",
        "latitude": 30.224609,
        "longitude": -98.75462,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "830-313-3535",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6630230",
        "name": "Dean Properties",
        "location": "Gardendale, TX",
        "state": "TX",
        "latitude": 32.0473611111111,
        "longitude": -102.290861111111,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "432-352-8077",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8982195",
        "name": "Bobcat Crossing RV Community/Horse Motel",
        "location": "Hallsville, TX",
        "state": "TX",
        "latitude": 32.4765833333333,
        "longitude": -94.5798611111111,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "903-668-3572",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-545981",
        "name": "CK25 Horse Motel",
        "location": "Happy, TX",
        "state": "TX",
        "latitude": 34.8222777777778,
        "longitude": -101.858805555556,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "817-694-6553",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9096953",
        "name": "Hall Ranch",
        "location": "Holland, TX",
        "state": "TX",
        "latitude": 30.8693333333333,
        "longitude": -97.3524444444444,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "254-931-0410",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2391509",
        "name": "Moore Ranch and Barrel Horses",
        "location": "Johnson City, TX",
        "state": "TX",
        "latitude": 30.2769722222222,
        "longitude": -98.4033888888889,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "512-695-1799",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3048001",
        "name": "Katiesplace",
        "location": "Junction, TX",
        "state": "TX",
        "latitude": 30.410472,
        "longitude": -99.665694,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "210-912-5695",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4090447",
        "name": "Golden Curls Ranch",
        "location": "Kaufman, TX",
        "state": "TX",
        "latitude": 32.544528,
        "longitude": -96.386472,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "214-794-1776",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-270772",
        "name": "Casa Cassel II Arabians",
        "location": "Lindale, TX",
        "state": "TX",
        "latitude": 32.504639,
        "longitude": -95.351444,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "903-780-1570",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8252586",
        "name": "The Roaming Roan",
        "location": "Linden, TX",
        "state": "TX",
        "latitude": 33.054583,
        "longitude": -94.377111,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "903-634-7626",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8376562",
        "name": "Mustang 2nd Chance Foundation",
        "location": "Lorena, TX",
        "state": "TX",
        "latitude": 31.36397,
        "longitude": -97.12087,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "714-512-7768",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1060720",
        "name": "JoMar Farms",
        "location": "Midlothian, TX",
        "state": "TX",
        "latitude": 32.498333,
        "longitude": -96.912944,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "817-312-1303",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1956868",
        "name": "Double T Ranch",
        "location": "Millsap, TX",
        "state": "TX",
        "latitude": 32.698695,
        "longitude": -98.010257,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "325-207-2815",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9694749",
        "name": "Quiet Country RV Park",
        "location": "Millsap, TX",
        "state": "TX",
        "latitude": 32.7955833333333,
        "longitude": -97.9934444444444,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "817-304-7277",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5222538",
        "name": "Off 82 Horse Hotel",
        "location": "Nocona, TX",
        "state": "TX",
        "latitude": 33.7783611111111,
        "longitude": -97.8335555555555,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "972-571-6104",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8389116",
        "name": "Justin Parish Team Roping",
        "location": "Perrin, TX",
        "state": "TX",
        "latitude": 32.992635,
        "longitude": -98.068447,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "817-846-6186",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5655653",
        "name": "Mandolynn Hill Farm",
        "location": "Pilot Point, TX",
        "state": "TX",
        "latitude": 33.314667,
        "longitude": -96.936083,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "940-365-2559",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2622650",
        "name": "Rock'n N B&B",
        "location": "Plains, TX",
        "state": "TX",
        "latitude": 33.215184,
        "longitude": -102.818799,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "806-456-7885",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4038837",
        "name": "13 Points Farm Points Farm - Horse Motel",
        "location": "Port Bolivar, TX",
        "state": "TX",
        "latitude": 29.3883055555556,
        "longitude": -94.7574444444444,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "434-222-1256",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6737718",
        "name": "Not Just Horses",
        "location": "Quanah, TX",
        "state": "TX",
        "latitude": 34.288667,
        "longitude": -99.73725,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "940-839-9000",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7968121",
        "name": "Hoof Haven, LLC",
        "location": "Quanah, TX",
        "state": "TX",
        "latitude": 34.2844166666667,
        "longitude": -99.7028055555556,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "940-839-7858",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3326934",
        "name": "Haby Settlement RV Park",
        "location": "Rio Medina, TX",
        "state": "TX",
        "latitude": 29.418306,
        "longitude": -98.908639,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "210-508-8266",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9445183",
        "name": "Windy Mane Ranch",
        "location": "Roanoke, TX",
        "state": "TX",
        "latitude": 32.987902,
        "longitude": -97.265513,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "817-929-7485",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2251188",
        "name": "Circle Four T",
        "location": "Rockport, TX",
        "state": "TX",
        "latitude": 28.028996,
        "longitude": -97.08203,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "910-265-5780",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5312030",
        "name": "Last Chance Ranch and Horse Hotel",
        "location": "San Angelo, TX",
        "state": "TX",
        "latitude": 31.6031944444444,
        "longitude": -100.614472222222,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "210-232-2890",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9671614",
        "name": "San Antonio Rose Palace",
        "location": "San Antonio, TX",
        "state": "TX",
        "latitude": 29.68325,
        "longitude": -98.6754722222222,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "210-698-3300",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8356839",
        "name": "Jim a Dee Ranch",
        "location": "Sanger, TX",
        "state": "TX",
        "latitude": 33.383056,
        "longitude": -97.285944,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "502-370-7200",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7012888",
        "name": "M&M Cow Company",
        "location": "Seguin, TX",
        "state": "TX",
        "latitude": 29.5504722222222,
        "longitude": -98.0568888888889,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "830-470-4779",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3694149",
        "name": "Lone Mound Ranch",
        "location": "Shamrock, TX",
        "state": "TX",
        "latitude": 35.142615,
        "longitude": -100.343838,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "307-331-4327",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6568307",
        "name": "Breezy Acres Horse Farm",
        "location": "Sherman, TX",
        "state": "TX",
        "latitude": 33.544161,
        "longitude": -96.501181,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "512-771-2506",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7060088",
        "name": "Silver Creek Stables",
        "location": "Sinton, TX",
        "state": "TX",
        "latitude": 28.18975,
        "longitude": -97.5877222222222,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "573-528-5129",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6523148",
        "name": "Quarter Circle Spade Ranch",
        "location": "Springtown, TX",
        "state": "TX",
        "latitude": 32.9737777777778,
        "longitude": -97.7890555555555,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "817-550-2241",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2355168",
        "name": "4C'S Rodeo Ranch",
        "location": "Stephenville, TX",
        "state": "TX",
        "latitude": 32.224567,
        "longitude": -98.170076,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "817-980-9946",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-743306",
        "name": "LJ Equine",
        "location": "Stephenville, TX",
        "state": "TX",
        "latitude": 32.39975,
        "longitude": -98.1616388888889,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "940-859-6587",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7074042",
        "name": "Into the Frontier at Jackass Flats in The Big Bend",
        "location": "Terlingua, TX",
        "state": "TX",
        "latitude": 29.3213333333333,
        "longitude": -103.615472222222,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "432-371-2082",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-114731",
        "name": "VZ Stalls",
        "location": "Van, TX",
        "state": "TX",
        "latitude": 32.53677,
        "longitude": -95.69972,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "970-580-5277",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9030990",
        "name": "Wild Heart Ranch Texas",
        "location": "Van, TX",
        "state": "TX",
        "latitude": 32.49908,
        "longitude": -95.64983,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "505-908-7565",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7730882",
        "name": "Tin Top Creek Equine Facility",
        "location": "Weatherford, TX",
        "state": "TX",
        "latitude": 32.655983,
        "longitude": -97.79644,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "907-388-8649",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6397592",
        "name": "County Line Arena",
        "location": "Wills Point, TX",
        "state": "TX",
        "latitude": 32.616305,
        "longitude": -95.972707,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "903-474-5308",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5006618",
        "name": "Falconwood",
        "location": "Winnie, TX",
        "state": "TX",
        "latitude": 29.841639,
        "longitude": -94.375611,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "409-656-3822",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5199226",
        "name": "13 Points Farm - Horse Motel",
        "location": "Winnie, TX",
        "state": "TX",
        "latitude": 29.87822,
        "longitude": -94.35544,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "434-222-1256",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8946955",
        "name": "Boulder Mountain Horse Motel",
        "location": "Bicknell, UT",
        "state": "UT",
        "latitude": 38.342175,
        "longitude": -111.545564,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "801-631-1275",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-787652",
        "name": "Iron Horse Enterprises",
        "location": "Cedar City, UT",
        "state": "UT",
        "latitude": 37.6020555555556,
        "longitude": -113.194861111111,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "435-669-4697",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1010740",
        "name": "Sage Hill Ranch",
        "location": "Eagle Mountain, UT",
        "state": "UT",
        "latitude": 40.375612,
        "longitude": -111.981581,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "801-885-0259",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-526691",
        "name": "Flying M Clydesdales",
        "location": "Erda, UT",
        "state": "UT",
        "latitude": 40.602845,
        "longitude": -112.275225,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "435-228-8688",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1981747",
        "name": "Green River Stable",
        "location": "Green River, UT",
        "state": "UT",
        "latitude": 39.043917,
        "longitude": -110.14825,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "435-820-6206",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9509100",
        "name": "9S STABLES",
        "location": "Hurricane, UT",
        "state": "UT",
        "latitude": 37.1331666666667,
        "longitude": -113.309777777778,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "435-703-4112",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3764809",
        "name": "3 Step Hideaway",
        "location": "LaSal, UT",
        "state": "UT",
        "latitude": 38.093861,
        "longitude": -109.082639,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "435-249-5567",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5375408",
        "name": "Rocky Ridge Outpost",
        "location": "Manila, UT",
        "state": "UT",
        "latitude": 40.944667,
        "longitude": -109.742667,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "435-784-3600",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9883689",
        "name": "Old Spanish Trail Arena",
        "location": "Moab, UT",
        "state": "UT",
        "latitude": 38.518492,
        "longitude": -109.493302,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "435-259-6226",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3159073",
        "name": "R/M Stables",
        "location": "Morgan, UT",
        "state": "UT",
        "latitude": 41.0390833333333,
        "longitude": -111.717416666667,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "801-735-3794",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3731344",
        "name": "Wasatch View Ranch, LLC",
        "location": "Ogden, UT",
        "state": "UT",
        "latitude": 41.275219,
        "longitude": -112.051191,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "425-221-6863",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9017055",
        "name": "High Desert Stables",
        "location": "Price, UT",
        "state": "UT",
        "latitude": 39.5721944444444,
        "longitude": -110.828972222222,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "435-650-9921",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-519580",
        "name": "Horse Haven Ranch",
        "location": "St. George, UT",
        "state": "UT",
        "latitude": 37.129306,
        "longitude": -113.60575,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "435-669-9082",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4828734",
        "name": "Reinmakers Equestrian Park",
        "location": "St George, UT",
        "state": "UT",
        "latitude": 37.04264,
        "longitude": -113.61691,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "801-649-9063",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1991631",
        "name": "Oak Hills Stable",
        "location": "Salem, UT",
        "state": "UT",
        "latitude": 40.040583,
        "longitude": -111.646889,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "801-360-1491",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6840183",
        "name": "Bed and Barn",
        "location": "Saratoga Springs, UT",
        "state": "UT",
        "latitude": 40.388889,
        "longitude": -111.923193,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "801-502-3952",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7390150",
        "name": "At Liberty Ranch",
        "location": "Snowville, UT",
        "state": "UT",
        "latitude": 41.9838,
        "longitude": -112.70536,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "801-791-8829",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4791274",
        "name": "Steve McIntyre",
        "location": "South Jordan, UT",
        "state": "UT",
        "latitude": 40.543053,
        "longitude": -111.945253,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "801-560-6954",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9455991",
        "name": "Hondoo Rivers & Trails",
        "location": "Torrey, UT",
        "state": "UT",
        "latitude": 38.298956,
        "longitude": -111.4189,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "435-425-3519",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2498469",
        "name": "The Triple Crown",
        "location": "West Jordan, UT",
        "state": "UT",
        "latitude": 40.58458,
        "longitude": -111.94811,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "801-403-7423",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8450666",
        "name": "Cottonwood Stables",
        "location": "VT -\u00a0Colchester",
        "state": "",
        "latitude": 44.5069444444444,
        "longitude": -73.20175,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2305453",
        "name": "AB Vance Reining Horses",
        "location": "Danville, VT",
        "state": "VT",
        "latitude": 44.3891388888889,
        "longitude": -72.1018333333333,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "802-751-9896",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5604231",
        "name": "Fuzzy Muzzles",
        "location": "New Haven, VT",
        "state": "VT",
        "latitude": 44.1126944444444,
        "longitude": -73.1501666666667,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "702-290-7148",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9257518",
        "name": "Goose Creek Farm",
        "location": "Bristol, VA",
        "state": "VA",
        "latitude": 36.657444,
        "longitude": -82.162833,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "276-669-5457",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-837652",
        "name": "Rustling Meadows Stable @ Roslyn Forest",
        "location": "Charlottesville, VA",
        "state": "VA",
        "latitude": 38.08267,
        "longitude": -78.50016,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "434-531-2435",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4297286",
        "name": "Woodcrest Stable",
        "location": "Glasgow, VA",
        "state": "VA",
        "latitude": 37.6819444444444,
        "longitude": -79.4828055555556,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "207-679-7508",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-194136",
        "name": "Lake Prince Farm LLC",
        "location": "Gordonsville, VA",
        "state": "VA",
        "latitude": 38.1133,
        "longitude": -78.18239,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "540-246-4685",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9913058",
        "name": "RockN K, Inc.",
        "location": "Highland Springs, VA",
        "state": "VA",
        "latitude": 37.548028,
        "longitude": -77.316528,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "804-833-3459",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-877695",
        "name": "Marriott Ranch",
        "location": "Hume, VA",
        "state": "VA",
        "latitude": 38.8220555555556,
        "longitude": -78.0550555555556,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "540-631-5018",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8443855",
        "name": "RTL All Equestrian Center, LLC",
        "location": "Lawrenceville, VA",
        "state": "VA",
        "latitude": 36.7471666666667,
        "longitude": -77.7832777777778,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "607-351-9650",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-439543",
        "name": "Lazy Acres Farm",
        "location": "Lexington, VA",
        "state": "VA",
        "latitude": 37.832694,
        "longitude": -79.405306,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "540 463-1636",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7086895",
        "name": "Shenandoah Sporthorses",
        "location": "Lexington, VA",
        "state": "VA",
        "latitude": 37.755972,
        "longitude": -79.484833,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "540-319-9810",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-817469",
        "name": "Ridgeway Belgians",
        "location": "Marion, VA",
        "state": "VA",
        "latitude": 36.854376,
        "longitude": -81.517619,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "336-247-2587",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1495382",
        "name": "LoSapio Farms",
        "location": "Natural Bridge, VA",
        "state": "VA",
        "latitude": 37.668799,
        "longitude": -79.543554,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "970-231-6689",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-88488",
        "name": "TuckerBerry Farm",
        "location": "Radiant, VA",
        "state": "VA",
        "latitude": 38.31275,
        "longitude": -78.204472,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "802-299-1220",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3622977",
        "name": "Rock Creek Farm at Chateau DuVall",
        "location": "Roanoke, VA",
        "state": "VA",
        "latitude": 37.323705,
        "longitude": -79.85213,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "540-815-0928",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4713222",
        "name": "The Lazy R",
        "location": "Ruther Glen, VA",
        "state": "VA",
        "latitude": 37.991724,
        "longitude": -77.519794,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "540-841-7868",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8994754",
        "name": "Travelers Rest Farm",
        "location": "St. Stephens Church, VA",
        "state": "VA",
        "latitude": 37.85507,
        "longitude": -77.09736,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "804-432-8474",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4981363",
        "name": "Hidden Meadows Farm",
        "location": "VA -\u00a0Scotchtown",
        "state": "",
        "latitude": 37.84997,
        "longitude": -77.59197,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "804-651-8092",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3702515",
        "name": "Emmanuel Equine Facility",
        "location": "Strasburg, VA",
        "state": "VA",
        "latitude": 39.00366,
        "longitude": -78.38461,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "540-974-5794",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5292652",
        "name": "Nodrog Farm",
        "location": "Winchester, VA",
        "state": "VA",
        "latitude": 39.22175,
        "longitude": -78.20187,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "540-539-9356",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2402627",
        "name": "R&R Ranch Horse Motel",
        "location": "Benton City, WA",
        "state": "WA",
        "latitude": 46.277335,
        "longitude": -119.570379,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "509-430-1247",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2890520",
        "name": "Jt Ranch",
        "location": "Chattaroy, WA",
        "state": "WA",
        "latitude": 47.878667,
        "longitude": -117.342611,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "509-760-3930",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1249516",
        "name": "Willow Lake Equestrian Center",
        "location": "Cheney, WA",
        "state": "WA",
        "latitude": 47.5654166666667,
        "longitude": -117.61825,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "509-220-4099",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2535863",
        "name": "Dragonwood Equine Facility",
        "location": "Cle Elum, WA",
        "state": "WA",
        "latitude": 47.1820555555556,
        "longitude": -120.804944444444,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "509-260-0385",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1126666",
        "name": "Evermore Event Center",
        "location": "Deer Park, WA",
        "state": "WA",
        "latitude": 47.9283055555556,
        "longitude": -117.476972222222,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "509-991-7495",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7268100",
        "name": "Ocean Acres B&B Horse Hotel",
        "location": "Grayland, WA",
        "state": "WA",
        "latitude": 46.762363,
        "longitude": -124.083084,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "360-267-5521",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7936789",
        "name": "Beringer House Stables",
        "location": "Kelso, WA",
        "state": "WA",
        "latitude": 46.194806,
        "longitude": -122.899444,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "360-846-3449",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1559553",
        "name": "Tulipsprings, LLC",
        "location": "Kennewick, WA",
        "state": "WA",
        "latitude": 46.163056,
        "longitude": -119.081,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "509-521-1100",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9961087",
        "name": "Roosters Stables",
        "location": "Kennewick, WA",
        "state": "WA",
        "latitude": 46.1762777777778,
        "longitude": -119.060138888889,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "509-833-5516",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6405595",
        "name": "Littlerock Stables",
        "location": "Olympia, WA",
        "state": "WA",
        "latitude": 46.9658333333333,
        "longitude": -122.956583333333,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "425-772-6792",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4255980",
        "name": "Farm RV Park",
        "location": "Rochester, WA",
        "state": "WA",
        "latitude": 46.8271666666667,
        "longitude": -123.093888888889,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "360-888-0530",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4356208",
        "name": "Legacy Stables",
        "location": "Roy, WA",
        "state": "WA",
        "latitude": 46.980303,
        "longitude": -122.396869,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "253-350-1891",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1124818",
        "name": "Craterview Horse Park",
        "location": "Silver Creek, WA",
        "state": "WA",
        "latitude": 46.51921,
        "longitude": -122.57809,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "503-812-7688",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3473150",
        "name": "The Spokane Equestrian Center",
        "location": "Spokane, WA",
        "state": "WA",
        "latitude": 47.5509166666667,
        "longitude": -117.443888888889,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "509-994-6635",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3572253",
        "name": "Showtime Arena, LLC",
        "location": "Deerfield, WI",
        "state": "WI",
        "latitude": 43.060041,
        "longitude": -89.106125,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "608-764-5555",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9524123",
        "name": "Triple Z Quarter Horses",
        "location": "De Pere, WI",
        "state": "WI",
        "latitude": 44.412187,
        "longitude": -88.19062,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "920-639-2071",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4408642",
        "name": "Lone Tree Farm Horse Training",
        "location": "Eau Claire, WI",
        "state": "WI",
        "latitude": 44.8290833333333,
        "longitude": -91.4326388888889,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "715-864-7038",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6510386",
        "name": "Pakes Farm",
        "location": "Janesville, WI",
        "state": "WI",
        "latitude": 42.774083,
        "longitude": -89.144583,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "608-247-1414",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8273524",
        "name": "The Sunrise Ranch",
        "location": "Mason, WI",
        "state": "WI",
        "latitude": 46.4435833333333,
        "longitude": -90.98725,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "715-730-0857",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6669823",
        "name": "Holiday Hilltop Ranch",
        "location": "Tomah, WI",
        "state": "WI",
        "latitude": 43.933131,
        "longitude": -90.449015,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "740-390-0240",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-235511",
        "name": "Spring Willow Farm",
        "location": "Union Grove, WI",
        "state": "WI",
        "latitude": 42.62285,
        "longitude": -88.05165,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "262-878-2121",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2167042",
        "name": "Royal Mountain Stables",
        "location": "Beckley, WV",
        "state": "WV",
        "latitude": 37.801956,
        "longitude": -81.22259,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "304-787-5656",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8574992",
        "name": "Sunday Stables",
        "location": "St. Albans, WV",
        "state": "WV",
        "latitude": 38.363501,
        "longitude": -81.8497676,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "304-722-4630",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8410304",
        "name": "JC Stage Stop Resort",
        "location": "Aladdin, WY",
        "state": "WY",
        "latitude": 44.659283,
        "longitude": -104.085482,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "307-896-9134",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-768353",
        "name": "The Soaring H",
        "location": "Casper, WY",
        "state": "WY",
        "latitude": 42.804027,
        "longitude": -106.560402,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "307-277-1419",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8053828",
        "name": "Terry Bison Ranch Resorts",
        "location": "Cheyenne, WY",
        "state": "WY",
        "latitude": 41.00288,
        "longitude": -104.90536,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "307-634-4171",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3296069",
        "name": "Overnight/Short Term Horse Motel",
        "location": "Cody, WY",
        "state": "WY",
        "latitude": 44.65172,
        "longitude": -108.97413,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "307-250-6693",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5260965",
        "name": "Canyon Camp Horse Hotel",
        "location": "Cokeville, WY",
        "state": "WY",
        "latitude": 42.4021666666667,
        "longitude": -111.041472222222,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "307-248-1373",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2847386",
        "name": "Rocky Mountain Lodge",
        "location": "Dubois, WY",
        "state": "WY",
        "latitude": 43.5452777777778,
        "longitude": -109.661,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "307-455-2844",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1623410",
        "name": "Platte Ridge Equestrian Center",
        "location": "Evansville, WY",
        "state": "WY",
        "latitude": 42.8598333333333,
        "longitude": -106.058166666667,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "307-237-4889",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2546191",
        "name": "Kluz Performance Horses",
        "location": "WY- Gillette",
        "state": "",
        "latitude": 44.17825,
        "longitude": -105.636898,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "307-660-8062",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2215957",
        "name": "Valar Horse Facility",
        "location": "Jackson, WY",
        "state": "WY",
        "latitude": 43.318361,
        "longitude": -110.728944,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "307-733-2733",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3780410",
        "name": "HAMIC RANCH",
        "location": "Powell, WY",
        "state": "WY",
        "latitude": 44.9747222222222,
        "longitude": -109.110166666667,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "951-830-2698",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9010688",
        "name": "Paradise S Ranch",
        "location": "Riverton, WY",
        "state": "WY",
        "latitude": 43.0852222222222,
        "longitude": -108.466805555556,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "307-709-7928",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4207108",
        "name": "Diamonds and Twine Equine, LLC",
        "location": "Riverton, WY",
        "state": "WY",
        "latitude": 43.1319444444444,
        "longitude": -108.489722222222,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "307-354-6683",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5091200",
        "name": "7L Horse Boarding",
        "location": "Saratoga, WY",
        "state": "WY",
        "latitude": 41.488698,
        "longitude": -106.781465,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "307-760-8235",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1374709",
        "name": "Boyer YL Ranch",
        "location": "Savery, WY",
        "state": "WY",
        "latitude": 41.120627,
        "longitude": -107.368268,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "307-383-7778",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2479457",
        "name": "W/D Hossienda",
        "location": "Sheridan, WY",
        "state": "WY",
        "latitude": 44.719987,
        "longitude": -106.993097,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "307-752-2468",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1417191",
        "name": "Sings With The Eagle",
        "location": "Sheridan, WY",
        "state": "WY",
        "latitude": 44.860202,
        "longitude": -106.981591,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "307-752-9615",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3812246",
        "name": "Ten Sleep RV Park, Cabins and Horse Hotel",
        "location": "Ten Sleep, WY",
        "state": "WY",
        "latitude": 44.034167,
        "longitude": -107.451778,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "307-366-2250",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8673837",
        "name": "Rand Creek Ranch, LLC",
        "location": "Wapiti, WY",
        "state": "WY",
        "latitude": 44.462278,
        "longitude": -109.457417,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "307-587-3200",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3722132",
        "name": "Cottonwood Creek Ranch",
        "location": "Wheatland, WY",
        "state": "WY",
        "latitude": 42.2875833333333,
        "longitude": -104.9765,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "307-358-2033",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6138311",
        "name": "Spur West Arena",
        "location": "Alhambra",
        "state": "",
        "latitude": 52.322369,
        "longitude": -114.673366,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "403-357-9010",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3987999",
        "name": "Silver Sage Community Corral",
        "location": "Brooks",
        "state": "",
        "latitude": 50.542721,
        "longitude": -111.899791,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "403-362-2262",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4329607",
        "name": "Rocking Heart Ranch",
        "location": "Cardston County",
        "state": "",
        "latitude": 49.1321944444444,
        "longitude": -113.821194444444,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "403-894-1005",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3519883",
        "name": "Hamilton Lane Equestrian",
        "location": "Foothills",
        "state": "",
        "latitude": 50.8449722222222,
        "longitude": -114.371861111111,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "780-966-1227",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9526559",
        "name": "Foothills Horse Motel",
        "location": "Foorhills",
        "state": "",
        "latitude": 50.81287,
        "longitude": -113.86011,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "403-988-5313",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3060853",
        "name": "Rough Diamond Ranch Alberta",
        "location": "Rocky View County",
        "state": "",
        "latitude": 51.4362222222222,
        "longitude": -114.634805555556,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "403-615-9060",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6033675",
        "name": "ML Arena at Spring Point",
        "location": "Spring Creek",
        "state": "",
        "latitude": 49.6769166666667,
        "longitude": -113.795555555556,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "403-315-4480",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4873861",
        "name": "Hammer Hill Stables",
        "location": "Strathmore",
        "state": "",
        "latitude": 51.0378523,
        "longitude": -113.4002873,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "403-888-2102",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2178381",
        "name": "Iron Creek Community",
        "location": "Viking",
        "state": "",
        "latitude": 53.0960793,
        "longitude": -111.7764533,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "780-336-3335",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3767355",
        "name": "Honey Field Farm",
        "location": "Abbotsford",
        "state": "",
        "latitude": 49.115433,
        "longitude": -122.404413,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "778-840-9657",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1882251",
        "name": "Hylee Training",
        "location": "Prince George",
        "state": "",
        "latitude": 53.7213333333333,
        "longitude": -122.58475,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "604-785-9953",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7037750",
        "name": "L&S Farm",
        "location": "Rosedale",
        "state": "",
        "latitude": 49.18375,
        "longitude": -121.785833333333,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "604-793-3378",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4670930",
        "name": "Blessings at the Creek",
        "location": "Elm Creek",
        "state": "",
        "latitude": 49.717982,
        "longitude": -97.900324,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "204-218-6064",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8587407",
        "name": "Scott MacKenzie Show Horses",
        "location": "Navin",
        "state": "",
        "latitude": 49.8476666666667,
        "longitude": -97.0041111111111,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "204-890-7675",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-922515",
        "name": "Bar 16 Stables",
        "location": "Strathclair",
        "state": "",
        "latitude": 50.492756,
        "longitude": -100.918704,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "431-541-1351",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3208518",
        "name": "J L Farm Stable",
        "location": "Winnipeg",
        "state": "",
        "latitude": 49.778389,
        "longitude": -97.045806,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "204-981-5867",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6044262",
        "name": "Off Grid Adventure",
        "location": "NB-Canada- Anagance",
        "state": "",
        "latitude": 45.871,
        "longitude": -65.2515277777778,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "506-688-2831",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5921051",
        "name": "Balance With Hooves",
        "location": "NB-Canada-Ste-Anne-de-Madawaska",
        "state": "",
        "latitude": 47.252553,
        "longitude": -68.015053,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "506-253-7303",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6654052",
        "name": "Pleasant Valley Equestrain Center",
        "location": "Green Creek",
        "state": "",
        "latitude": 45.2456111111111,
        "longitude": -63.3703888888889,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "902-499-2191",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7968890",
        "name": "Molinaro Ranch",
        "location": "Acton",
        "state": "",
        "latitude": 43.604052,
        "longitude": -80.029989,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "519-831-4178",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5801452",
        "name": "Nooyen Farms",
        "location": "Blenheim",
        "state": "",
        "latitude": 42.417059,
        "longitude": -82.010181,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "905-355-1592",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8289",
        "name": "Perre & Nicole Bovay",
        "location": "Brighton",
        "state": "",
        "latitude": 44.0155,
        "longitude": -77.8225,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "905-355-1592",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1885554",
        "name": "Safe At Last Stables",
        "location": "Cameron",
        "state": "",
        "latitude": 44.438156,
        "longitude": -78.773345,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "647-327-6993",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4845409",
        "name": "Circle 8 Ranch",
        "location": "Drumbo",
        "state": "",
        "latitude": 43.2576388888889,
        "longitude": -80.5007777777778,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "519-404-5959",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6709482",
        "name": "Horse Country Campground",
        "location": "Forresters Falls",
        "state": "",
        "latitude": 45.665639,
        "longitude": -76.725056,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "613-867-0585",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8396231",
        "name": "Angolly\u2019s Ranch",
        "location": "Hearst",
        "state": "",
        "latitude": 49.622575,
        "longitude": -83.765596,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "705-372-8388",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-966765",
        "name": "Candy Hill Farms",
        "location": "Kakabeka Falls",
        "state": "",
        "latitude": 48.333833,
        "longitude": -89.553361,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "807-473-9579",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-1408888",
        "name": "The Island Ranch",
        "location": "Richard's Landing",
        "state": "",
        "latitude": 46.2477222222222,
        "longitude": -84.0422777777778,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "705-325-5858",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-7217044",
        "name": "Foxton Road Farm",
        "location": "Roslin",
        "state": "",
        "latitude": 44.347889,
        "longitude": -77.335417,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "613-391-4146",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-388737",
        "name": "SCR",
        "location": "Scotland",
        "state": "",
        "latitude": 43.011,
        "longitude": -80.323833,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "519-717-1226",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3128592",
        "name": "King Air and Bale",
        "location": "Warren",
        "state": "",
        "latitude": 46.4210555555556,
        "longitude": -80.3608888888889,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "705-303-5529",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-9394461",
        "name": "Waylor Farm",
        "location": "Wellington North",
        "state": "",
        "latitude": 43.918944,
        "longitude": -80.461639,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "519-994-3412",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-2193086",
        "name": "Maefield Horse Farm Lodge & Livery",
        "location": "Wyoming",
        "state": "",
        "latitude": 43.003861,
        "longitude": -82.10475,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "519-899-2167",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8719319",
        "name": "Ferme G. Guillemette Inc.",
        "location": "B\u00e9cancour",
        "state": "",
        "latitude": 46.2630555555556,
        "longitude": -72.5179166666667,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "819-371-4154",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4356788",
        "name": "Ranch du p'tit Ch\u00e2teau",
        "location": "Ste-Justine-De-Newton",
        "state": "",
        "latitude": 45.3848333333333,
        "longitude": -74.4347777777778,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "450-377-6986",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-5358259",
        "name": "\u00c9curie Heartford Stables",
        "location": "Ste Marthe",
        "state": "",
        "latitude": 45.3998055555556,
        "longitude": -74.2507777777778,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "438-889-7303",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-3118212",
        "name": "Ferme Di-nou enr",
        "location": "Saint-Val\u00e9rien-de-Milton",
        "state": "",
        "latitude": 45.592306,
        "longitude": -72.774611,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "450-793-4228",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-4888393",
        "name": "K & K Training Stables",
        "location": "Mortlach",
        "state": "",
        "latitude": 50.452639,
        "longitude": -106.096722,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "306-355-2599",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-8384207",
        "name": "Vickis EAL and Photography",
        "location": "Prud'Homme",
        "state": "",
        "latitude": 52.340151,
        "longitude": -105.890542,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "306-417-9022",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    },
    {
        "id": "layover-6991137",
        "name": "GCJ Stables, Ltd",
        "location": "Regina",
        "state": "",
        "latitude": 50.601194,
        "longitude": -104.581083,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [
            "30A"
        ],
        "accommodations": [
            "Stalls",
            "Corrals",
            "Trails"
        ],
        "maxRigLength": 60,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "306-525-6922",
        "website": "",
        "description": "Horse layover facility. Call to verify availability, amenities, and pricing before arrival.",
        "isVerified": false,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": false,
        "hasDumpStation": false,
        "hasWifi": false,
        "hasBathhouse": false,
        "pullThroughAvailable": false,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": [
            "8B5E3C",
            "D4A853"
        ],
        "source": "Layover"
    }
]

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

    # Layover listings — deduplicated by proximity against all existing camps
    print("\nMerging layover listings...")
    import math as _math
    layover_new = 0
    for camp in fetch_layovers():
        cid = camp["id"]
        if cid not in all_camps:
            lat, lng = camp["latitude"], camp["longitude"]
            dup = False
            for ex in all_camps.values():
                dlat = _math.radians(lat - ex["latitude"])
                dlng = _math.radians(lng - ex["longitude"])
                a = _math.sin(dlat/2)**2 + _math.cos(_math.radians(lat))*_math.cos(_math.radians(ex["latitude"]))*_math.sin(dlng/2)**2
                if 6371000 * 2 * _math.atan2(_math.sqrt(a), _math.sqrt(1-a)) < 500:
                    dup = True
                    break
            if not dup:
                all_camps[cid] = camp
                layover_new += 1
    print(f"  Layovers: {layover_new} new listings added")

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

    google_count   = sum(1 for c in camps_list if c.get("source") == "Google Places")
    layover_count  = sum(1 for c in camps_list if c.get("source") == "Layover")
    print(f"\nDone. {len(camps_list)} total camps written to camps.json")
    print(f"  RIDB:         {total_ridb}")
    print(f"  NPS:          {total_nps}")
    print(f"  Layovers:     {layover_count}")
    print(f"  Google Places:{google_count}")
    print(f"  Unique total: {len(camps_list)}")


if __name__ == "__main__":
    main()
