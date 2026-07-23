"""
Shatro EPC Scanner
==================
Queries the UK non-domestic EPC register (epc.opendatacommunities.org) for
office buildings (property type "B1 Offices...") across target London
postcode districts, keeps only certificates rated D/E/F/G (below C) with
a small-to-medium floor area, and pushes new matches to the
"London Offices — EPC Ratings" Monday.com board.

SETUP REQUIRED (one-off, free):
  1. Register at https://epc.opendatacommunities.org/login
  2. Go to your account page and copy your API key
  3. Set two GitHub Actions secrets (or env vars if running locally):
       EPC_API_EMAIL = the email you registered with
       EPC_API_KEY   = the API key from your account page
  The API uses HTTP Basic Auth: username = email, password = API key.

This is intentionally a separate script from the planning scraper — it
runs on its own schedule (e.g. weekly, since EPC data changes far less
often than planning applications) rather than daily.
"""
import requests
import os
import time
import base64
from datetime import datetime

def _cfg(name, default=""):
    return os.environ.get(name, default)

EPC_API_EMAIL = _cfg("EPC_API_EMAIL", "")
EPC_API_KEY   = _cfg("EPC_API_KEY", "")
EPC_BASE      = "https://epc.opendatacommunities.org/api/v1/non-domestic/search"

MONDAY_API_TOKEN = _cfg("MONDAY_API_TOKEN", "")
MONDAY_BOARD_ID  = _cfg("EPC_BOARD_ID", "5100826961")

# Column IDs on the "London Offices — EPC Ratings" board
COL_POSTCODE   = "text_mm5grp39"
COL_BOROUGH    = "text_mm5gge51"
COL_RATING     = "dropdown_mm5gwzgp"
COL_SCORE      = "numeric_mm5gfqqm"
COL_VALID      = "date_mm5gtt2p"
COL_FLOORAREA  = "numeric_mm5gm3tz"
COL_CHARACTER  = "text_mm5g3d0q"
COL_AGENT      = "text_mm5ghj6t"
COL_AGENTCONT  = "text_mm5gqybr"
COL_SOURCE     = "link_mm5gcwzf"
COL_ADDED      = "date_mm5g67dn"
COL_NOTES      = "long_text_mm5gg6hk"

# Postcode districts to scan — same "boutique/characterful" areas
# identified in research: Soho/Fitzrovia, Shoreditch, Clerkenwell,
# Covent Garden. Extend this list as you identify more target areas.
TARGET_POSTCODE_DISTRICTS = [
    "W1D", "W1F", "W1T", "W1W",      # Soho / Fitzrovia
    "EC2A", "E1", "E2",               # Shoreditch
    "EC1V", "EC1M", "EC1R",           # Clerkenwell / Farringdon
    "WC2H", "WC2E",                   # Covent Garden
]

# Only keep buildings in this floor-area band — small/medium, matching
# the "boutique" brief rather than large institutional office blocks.
MIN_FLOOR_AREA = 200     # sqm
MAX_FLOOR_AREA = 4000    # sqm

BELOW_C_RATINGS = {"D", "E", "F", "G"}

def _auth_header():
    token = base64.b64encode(f"{EPC_API_EMAIL}:{EPC_API_KEY}".encode()).decode()
    return {"Authorization": f"Basic {token}", "Accept": "application/json"}

def fetch_district(postcode_district):
    """Fetch non-domestic EPC certificates for a postcode district.
    Paginates via the search-after cursor the API returns."""
    results = []
    search_after = None
    for _ in range(20):  # safety cap on pagination
        params = {"postcode": postcode_district, "size": 100}
        if search_after:
            params["search-after"] = search_after
        try:
            r = requests.get(EPC_BASE, headers=_auth_header(), params=params, timeout=20)
        except Exception as e:
            print(f"  {postcode_district}: request error — {e}")
            break
        if r.status_code == 401:
            print("  AUTH ERROR — check EPC_API_EMAIL / EPC_API_KEY secrets.")
            break
        if r.status_code != 200:
            print(f"  {postcode_district}: HTTP {r.status_code} — body: {r.text[:300]}")
            break
        try:
            data = r.json()
        except Exception as e:
            print(f"  {postcode_district}: response wasn't valid JSON ({e})")
            print(f"    content-type: {r.headers.get('content-type')}")
            print(f"    body preview: {r.text[:300]!r}")
            break
        rows = data.get("rows", [])
        if not rows:
            break
        results.extend(rows)
        search_after = r.headers.get("X-Next-Search-After")
        if not search_after or len(rows) < 100:
            break
        time.sleep(0.3)
    return results

def is_office(row):
    ptype = (row.get("property-type") or "").lower()
    return "office" in ptype or "b1" in ptype

def passes_filters(row):
    if not is_office(row):
        return False
    rating = (row.get("asset-rating-band") or row.get("current-energy-rating") or "").upper()
    if rating not in BELOW_C_RATINGS:
        return False
    try:
        area = float(row.get("floor-area") or 0)
    except ValueError:
        area = 0
    if area and not (MIN_FLOOR_AREA <= area <= MAX_FLOOR_AREA):
        return False
    return True

def fetch_all():
    print("=" * 60)
    print(f"SHATRO EPC Scanner — {datetime.now().strftime('%d %b %Y %H:%M')}")
    print("=" * 60)
    if not EPC_API_EMAIL or not EPC_API_KEY:
        print("Missing EPC_API_EMAIL / EPC_API_KEY — register free at "
              "https://epc.opendatacommunities.org/login and set these as secrets.")
        return []
    all_rows = []
    for district in TARGET_POSTCODE_DISTRICTS:
        rows = fetch_district(district)
        kept = [r for r in rows if passes_filters(r)]
        print(f"  {district}: {len(rows)} certificates, {len(kept)} match (office, below C, {MIN_FLOOR_AREA}-{MAX_FLOOR_AREA} sqm)")
        all_rows.extend(kept)
    return all_rows

def monday_api(query, variables):
    try:
        r = requests.post(
            "https://api.monday.com/v2",
            json={"query": query, "variables": variables},
            headers={"Authorization": MONDAY_API_TOKEN, "Content-Type": "application/json",
                     "API-Version": "2024-01"},
            timeout=15,
        )
        return r.json()
    except Exception as e:
        print(f"  Monday API error: {e}")
        return {}

def get_existing_addresses():
    q = """query ($b: ID!) { boards(ids: [$b]) { items_page(limit: 500) { items { name } } } }"""
    res = monday_api(q, {"b": MONDAY_BOARD_ID})
    items = ((res or {}).get("data", {}).get("boards", [{}])[0]
             .get("items_page", {}).get("items", []))
    return set(i["name"] for i in items)

def add_to_board(row):
    address = row.get("address", "") or row.get("address1", "") or "Unknown address"
    postcode = row.get("postcode", "")
    rating = (row.get("asset-rating-band") or row.get("current-energy-rating") or "").upper()
    score = row.get("asset-rating") or row.get("current-energy-efficiency") or ""
    valid_until = row.get("inspection-date") or ""
    area = row.get("floor-area") or ""
    lmk = row.get("lmk-key", "")
    cert_url = f"https://find-energy-certificate.service.gov.uk/energy-certificate/{lmk}" if lmk else ""

    col = {
        COL_POSTCODE: postcode,
        COL_BOROUGH:  row.get("local-authority-label", ""),
        COL_RATING:   {"label": rating} if rating else None,
        COL_SCORE:    str(score) if score else None,
        COL_FLOORAREA: str(area) if area else None,
        COL_ADDED:    {"date": datetime.now().strftime("%Y-%m-%d")},
        COL_NOTES:    {"text": "Auto-added by shatro_epc_scan.py — verify managing agent before outreach."},
    }
    if cert_url:
        col[COL_SOURCE] = {"url": cert_url, "text": "View EPC Certificate"}
    col = {k: v for k, v in col.items() if v is not None}

    mut = """
    mutation ($b: ID!, $n: String!, $cv: JSON!) {
        create_item(board_id: $b, item_name: $n, column_values: $cv,
                     create_labels_if_missing: true) { id }
    }
    """
    res = monday_api(mut, {"b": MONDAY_BOARD_ID, "n": address[:255], "cv": __import__("json").dumps(col)})
    item = ((res or {}).get("data") or {}).get("create_item") or {}
    return item.get("id")

def run():
    rows = fetch_all()
    print(f"\nTotal matches across all districts: {len(rows)}")
    if not rows:
        return
    existing = get_existing_addresses()
    added = 0
    for row in rows:
        address = row.get("address", "") or "Unknown address"
        if address[:255] in existing:
            continue
        if add_to_board(row):
            added += 1
            print(f"  Added: {address}")
        time.sleep(0.3)
    print(f"\nDone — {added} new building(s) added to the EPC board.")
    print("NOTE: managing agent / landlord is NOT auto-populated — the EPC "
          "register doesn't carry this. Fill it in manually per building "
          "before outreach (Land Registry / Companies House / agent listing).")

if __name__ == "__main__":
    import traceback
    try:
        print(f"EPC_API_EMAIL set: {bool(EPC_API_EMAIL)} | EPC_API_KEY set: {bool(EPC_API_KEY)} | MONDAY_API_TOKEN set: {bool(MONDAY_API_TOKEN)}")
        run()
    except Exception:
        print("=== UNHANDLED EXCEPTION ===")
        traceback.print_exc()
        raise
