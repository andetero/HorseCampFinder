#!/usr/bin/env python3
"""
HorseCamp Data Fetcher
Runs nightly via GitHub Actions.
Calls Recreation.gov (RIDB) and NPS APIs, writes results to docs/camps.json
which is served at horsecampfinder.com/camps.json for the iOS app.

Required GitHub Secrets:
  RIDB_API_KEY  — from ridb.recreation.gov/profile
  NPS_API_KEY   — from developer.nps.gov/signup
"""

import os, json, time, re, requests
from datetime import datetime, timezone

RIDB_KEY = os.environ.get("RIDB_API_KEY", "")
NPS_KEY  = os.environ.get("NPS_API_KEY", "")

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

                camps[fid] = {
                    "id":                  f"ridb-{fid}",
                    "name":                f.get("FacilityName", "Unknown Camp"),
                    "location":            f"{city}, {fstate}".strip(", "),
                    "state":               fstate,
                    "latitude":            lat,
                    "longitude":           lng,
                    "pricePerNight":       25.0,
                    "horseFeePerNight":    0.0,
                    "hookups":             list(dict.fromkeys(hookups)),
                    "accommodations":      list(dict.fromkeys(accommodations)),
                    "maxRigLength":        60,
                    "stallCount":          8 if "stall" in blob_lower else 0,
                    "paddockCount":        6 if ("corral" in blob_lower or "paddock" in blob_lower) else 0,
                    "phone":               f.get("FacilityPhone", ""),
                    "website":             f.get("FacilityReservationURL", "") or f"https://www.recreation.gov/camping/campgrounds/{fid}",
                    "description":         desc[:400],
                    "isVerified":          True,
                    "seasonStart":         5,
                    "seasonEnd":           10,
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
            "pricePerNight":       fee or 20.0,
            "horseFeePerNight":    0.0,
            "hookups":             hookups,
            "accommodations":      list(dict.fromkeys(accommodations)),
            "maxRigLength":        55,
            "stallCount":          6 if amenities.get("stableNearby") == "Yes" else 0,
            "paddockCount":        4 if amenities.get("corralOrPaddockOnsite") == "Yes" else 0,
            "phone":               phone,
            "website":             c.get("url", f"https://www.nps.gov/{c.get('parkCode', '')}/"),
            "description":         desc[:400],
            "isVerified":          True,
            "seasonStart":         5,
            "seasonEnd":           10,
            "hasWashRack":         False,
            "hasDumpStation":      amenities.get("dumpStation") == "Yes",
            "hasWifi":             amenities.get("internetConnectivity") == "Yes",
            "hasBathhouse":        "shower" in (amenities.get("showers", "") or "").lower(),
            "pullThroughAvailable": False,
            "rating":              0.0,
            "reviewCount":         0,
            "imageColors":         ["4A7FA5", "5C7A4E"],
            "source":              "NPS",
        })

    return camps


# ── MAIN ───────────────────────────────────────────────────────────────
def main():
    print(f"HorseCamp data fetch starting — {datetime.now(timezone.utc).isoformat()}")
    print(f"RIDB key present: {'Yes' if RIDB_KEY else 'NO — set RIDB_API_KEY secret'}")
    print(f"NPS key present:  {'Yes' if NPS_KEY  else 'NO — set NPS_API_KEY secret'}")

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

    camps_list = sorted(all_camps.values(), key=lambda c: c["state"])

    output = {
        "generated":  datetime.now(timezone.utc).isoformat(),
        "count":      len(camps_list),
        "sources":    ["Recreation.gov RIDB", "NPS API"],
        "camps":      camps_list,
    }

    # Write to docs/ so GitHub Pages serves it
    with open("camps.json", "w") as f:
        json.dump(output, f, indent=2)

    print(f"\nDone. {len(camps_list)} total camps written to docs/camps.json")
    print(f"  RIDB total: {total_ridb}")
    print(f"  NPS total:  {total_nps}")
    print(f"  Unique:     {len(camps_list)}")


if __name__ == "__main__":
    main()
