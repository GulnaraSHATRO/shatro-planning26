import requests
import json
import os
import time
import smtplib
import re
import urllib3
from bs4 import BeautifulSoup
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from urllib.parse import urljoin

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ============================================================
# CONFIGURATION
# ------------------------------------------------------------
# Credentials are read from environment variables when they exist
# (that's how the cloud/GitHub Actions run supplies them), and fall
# back to the values below when running on your own laptop.
# ============================================================
def _cfg(name, default=""):
    return os.environ.get(name, default)

MONDAY_API_TOKEN = _cfg("MONDAY_API_TOKEN", "eyJhbGciOiJIUzI1NiJ9.eyJ0aWQiOjY2OTgxMjk5MSwiYWFpIjoxMSwidWlkIjoxMDM1MjkxNTIsImlhZCI6IjIwMjYtMDYtMTFUMTI6Mzg6NTMuMDAwWiIsInBlciI6Im1lOndyaXRlIiwiYWN0aWQiOjM1MDg1OTY3LCJyZ24iOiJldWMxIn0.S0C15wMkX-ze9ZnwuFjF6ECBI3Qpkvke0opp0Xipez0")
MONDAY_BOARD_ID  = _cfg("MONDAY_BOARD_ID", "5098358178")
# How many days back to scan on each run. The Planning London Datahub
# publishes some applications a few days late, so a 7-day window makes sure
# nothing is missed. Duplicates are filtered out against the board, so a
# wider window never creates repeats — it only closes gaps.
DAYS_BACK        = int(_cfg("DAYS_BACK", "7"))

# Live portal lookups for direct application links. Set False to skip them
# (faster runs, but boroughs without a direct link from the data source will
# get a generic search link instead).
RESOLVE_DIRECT_LINKS = True

EMAIL_TO       = _cfg("EMAIL_TO", "g.makhmutovajanukonis@shatro.uk")
EMAIL_FROM     = _cfg("EMAIL_FROM", "g.makhmutovajanukonis@shatro.uk")
EMAIL_PASSWORD = _cfg("EMAIL_PASSWORD", "jawatxjvymvmjttb")
EMAIL_SMTP     = "smtp.gmail.com"
EMAIL_PORT     = 587
# Send a short "ran OK, 0 new today" email on days when nothing is added,
# so a silent inbox never leaves you guessing whether the job ran.
SEND_HEARTBEAT = True

PLANWIRE_KEY = _cfg("PLANWIRE_KEY", "90ed4c18abf046dbe06aee49153f6e23f17c98aba30a1036")
PLANWIRE_URL = "https://api.planwire.io/v1/applications"

PLD_URL     = "https://planningdata.london.gov.uk/api-guest/applications/_search"
PLD_HEADERS = {"X-API-AllowRequest": "be2rmRnt&", "Content-Type": "application/json"}

# Monday.com column IDs (confirmed from live board)
COL_DESC     = "long_text_mm47psdt"   # Works Description
COL_ARCH     = "text_mm47ff45"        # Architect / Company
COL_WEBSITE  = "link_mm47fp34"        # Architect Website
COL_LINK     = "link_mm47ry6m"        # Planning Portal Link
COL_DOCS     = "link_mm4711zt"        # Drawings Available
COL_DATE     = "date_mm47sp3"         # Application Date
COL_STATUS   = "color_mm5gbk4j"       # Status (real column — the old "status" id didn't exist on this board)
COL_APP_TYPE = "dropdown_mm472bpj"    # Application Type
COL_ADDRESS  = "location_mm47w9vb"    # Project Address
COL_SECTOR   = "dropdown_mm5g4xwf"    # Sector (Residential / Commercial / Mixed Use) — new column

PLANWIRE_COUNCIL = {
    "Westminster":            "westminster",
    "Camden":                 "camden",
    "Southwark":              "southwark",
    "Wandsworth":             "wandsworth",
    "Hammersmith and Fulham": "hammersmith-and-fulham",
    "Islington":              "islington",
    "Tower Hamlets":          "tower-hamlets",
    "City of London":         "city-of-london",
    "Richmond upon Thames":   "richmond-upon-thames",
    "Merton":                 "merton",
    "Lambeth":                "lambeth",
    "Barnet":                 "barnet",
    "Haringey":               "haringey",
    "Kensington and Chelsea": "kensington-and-chelsea",
}

BOROUGH_GROUPS = {
    "Kensington and Chelsea":  "group_mm47vpd7",
    "Westminster":             "group_mm47bsaw",
    "Camden":                  "group_mm47pr85",
    "Wandsworth":              "group_mm47jv7d",
    "Hammersmith and Fulham":  "group_mm479v1v",
    "Islington":               "group_mm47dnr7",
    "Tower Hamlets":           "group_mm47cmps",
    "City of London":          "group_mm47cmps",
    "Richmond upon Thames":    "group_mm47ska1",
    "Merton":                  "group_mm47ska1",
    "Southwark":               "group_mm47qrzg",
    "Lambeth":                 "group_mm47qrzg",
    "Barnet":                  "group_mm476axx",
    "Haringey":                "group_mm476axx",
}

TARGET_BOROUGHS = list(BOROUGH_GROUPS.keys())

# ------------------------------------------------------------
# FILTERING RULES  —  edit these lists to tune what gets through
# ------------------------------------------------------------
# 1) POSITIVE: an application must mention at least one of these
#    (keeps it to commercial + residential building work)
KEYWORDS = [
    "dwelling", "house", "flat", "apartment", "residential",
    "bedroom", "maisonette", "studio", "penthouse", "villa",
    "new build", "renovation", "refurbishment", "extension",
    "loft conversion", "basement", "listed building",
    "office", "commercial", "restaurant", "hotel", "retail",
    "hospitality", "fit-out", "fitout", "boutique", "shop",
    "cafe", "bar", "club", "gym", "spa", "clinic",
    "showroom", "gallery", "mixed use", "change of use",
    "co-working", "workspace",
    "redevelopment", "demolition", "erection of",
]

# 2) EXCLUDE BY APPLICATION TYPE: amendments, conditions, certs, ads, trees
#    (matched against the application_type field, lowercase substring)
EXCLUDE_TYPES = [
    "amendment", "non material", "non-material",
    "approval of details", "reserved by condition",
    "variation of condition", "removal of condition",
    "discharge of condition", "discharge of conditions",
    "certificate of lawful", "certificate of lawfulness",
    "advertisement", "tree",
]

# 3) HARD EXCLUDE (description): always rejected, no exceptions.
#    Amendments, retrospective, and pure non-construction noise.
HARD_EXCLUDE = [
    "retrospective",
    "non-material amendment", "non material amendment",
    "minor material amendment", "material amendment",
    "amendment to", "amendments to",
    "variation of condition", "removal of condition",
    "discharge of condition", "approval of details",
    "tree works", "works to trees", "fell ", "felling",
    "pruning", "crown reduction", "arboricultural",
    "advertisement", "illuminated sign", "fascia sign",
    "signage", "hoarding",
    "telecommunication", "telecoms", "antenna", "5g mast",
    "solar panel", "solar panels", "photovoltaic", "pv panel",
    "satellite dish",
    "dropped kerb", "vehicle crossover", "crossover",
    "ev charging", "electric vehicle charging",
    "cycle store", "bin store", "refuse store", "hardstanding",
    # --- added: minor householder-scale signals that previously slipped
    # through untouched (dormers, hip-to-gable, garage conversions, etc.)
    "dormer", "hip to gable", "hip-to-gable", "outrigger",
    "juliette balcony", "juliet balcony",
    "garage into", "garage conversion", "conversion of the existing garage",
]

# 4) MAINTENANCE / MINOR WORKS (description): rejected UNLESS a
#    SUBSTANTIAL signal (see below) is also present — so these terms
#    appearing inside a real redevelopment don't kill it.
MAINTENANCE_TERMS = [
    "landscaping", "landscape", "public realm",
    "porch",
    "re-roof", "reroof", "re-roofing", "roof covering",
    "roof recovering", "roof replacement", "replacement roof",
    "roof repair", "roof upgrade", "new roof",
    "rooflight", "roof light", "skylight",
    "repointing", "rendering", "render only",
    "guttering", "rainwater goods",
    "window", "door",
    "boundary wall", "boundary fence", "fencing", "fence",
    "decking", "garden shed", "outbuilding", "garden room",
    "redecoration", "painting and decorating",
    # --- added: ordinary single/two-storey house extensions. These are
    # the "change the roof / change the gate" scale of job — real
    # redevelopment (basement, new dwelling, multi-unit, change of use)
    # still overrides via SUBSTANTIAL_SIGNALS below.
    "single storey rear extension", "single storey side extension",
    "single-storey rear extension", "single-storey side extension",
    "single storey front extension", "two storey side extension",
    "two storey rear extension", "first floor extension",
    "first floor side extension", "first floor rear extension",
    "storey extension", "rear extension", "side extension",
    "front extension", "wraparound extension",
]

# 5) SUBSTANTIAL signals: presence of any of these overrides the
#    maintenance list above (it's a real project, not a small job).
#    Kept deliberately specific so generic verbs like "erection of"
#    on a tiny porch job don't trigger an override.
SUBSTANTIAL_SIGNALS = [
    "basement", "subterranean",
    "redevelopment", "new build", "newbuild", "rebuild",
    "new dwelling", "new house", "replacement dwelling", "replacement house",
    "new homes", "apartment block", "block of flats",
    "residential development", "residential units", "residential scheme",
    "change of use", "fit-out", "fitout",
    "refurbishment", "mixed use", "mixed-use",
    "block of flats", "apartment block", "residential development",
    "residential scheme", "new homes", "new dwelling", "new house",
    "replacement dwelling", "replacement house", "demolition",
    "self-contained flats", "self-contained apartments",
    "self-contained units", "into flats", "into apartments",
]

# 6) HIGH-END POSTCODE DISTRICTS (outward codes). The W1*, SW1*, WC*
#    and EC* families are handled by rule in is_high_end() below.
HIGH_END_POSTCODES = {
    # Chelsea / Kensington / South Kensington / Earls Court
    "SW3", "SW5", "SW7", "SW10",
    # Fulham / Battersea / Barnes / Wimbledon
    "SW6", "SW11", "SW13", "SW19",
    # Bayswater / Kensington / Maida Vale / Notting Hill / Holland Park
    "W2", "W8", "W9", "W11", "W14",
    # Hampstead / St John's Wood / Regent's Park / Primrose Hill
    "NW1", "NW3", "NW8",
    # Islington / Canonbury / Highgate / Muswell Hill
    "N1", "N6", "N10",
    # Richmond / Richmond Hill
    "TW9", "TW10",
    # Hampstead Garden Suburb / Totteridge
    "NW11", "N20",
    # Southwark riverside / Dulwich Village
    "SE1", "SE21",
    # Wapping / Canary Wharf
    "E1W", "E14",
}

# 7) REFERENCE SUFFIX EXCLUSIONS — London planning refs end in a code
#    that reflects the council's OWN classification (e.g. 26/1234/HSE).
#    This is far more reliable than guessing from free-text descriptions:
#    HSE ("Householder") is by definition extension/alteration to a single
#    house — exactly the "change the roof / change the gate" scale — no
#    matter how the description is worded. Same logic for prior-approval
#    and certificate-of-lawfulness codes, which are confirmations of
#    already-permitted minor works, not new construction opportunities.
EXCLUDE_REF_SUFFIXES = {
    "HSE",                      # Householder (house extensions/alterations)
    "PNH", "PNE",                # Prior Notification — Householder ext.
    "PA1", "PA2", "PA3", "PA4", "PA5", "PA6", "PA7", "PA8",  # Prior Approval (PD)
    "191", "192",                # Certificate of Lawfulness (existing/proposed)
    "CLE", "CLP", "CLUD", "LDC", "LDCE", "LDCP",  # same, other councils' codes
    "ADV",                       # Advertisement consent
    "TPO",                       # Tree works
    "NMA",                       # Non-material amendment
    "S73", "S73A", "VAR",        # Variation/removal of condition
    "DISCH", "COND",             # Discharge of condition
}

def ref_suffix(ref):
    """Return the trailing classification code of a planning reference,
    e.g. '26/2298/192' -> '192', '26/2603/HSE' -> 'HSE'."""
    if not ref:
        return ""
    return ref.strip().split("/")[-1].upper()

# ============================================================
# HELPERS
# ============================================================
def get_group(borough):
    for key, gid in BOROUGH_GROUPS.items():
        if key.lower() in borough.lower():
            return gid
    return "group_mm47vpd7"

def _has_term(text, terms):
    """Return the first matching term, or None. Multi-word/hyphenated terms
    use plain substring; single words use word boundaries so 'fence' doesn't
    match 'defence', 'gate' doesn't match 'investigate', etc."""
    for t in terms:
        if (" " in t) or ("-" in t):
            if t in text:
                return t
        else:
            suffix = "" if t.endswith("s") else "s?"
            if re.search(r"\b" + re.escape(t) + suffix + r"\b", text):
                return t
    return None

def outward_code(app):
    """Best-effort outward postcode (e.g. 'SW3', 'W1K'). Prefer the API
    postcode field, fall back to a regex over the address."""
    pc = (app.get("postcode") or "").upper().strip()
    if not pc:
        m = re.search(r"\b([A-Z]{1,2}[0-9][0-9A-Z]?)\s*[0-9][A-Z]{2}\b",
                      (app.get("address") or "").upper())
        if m:
            pc = m.group(1)
    return pc.split()[0] if pc else ""

def is_high_end(app):
    pc = outward_code(app)
    if not pc:
        return False  # no postcode -> can't confirm prime area -> reject
    if pc in HIGH_END_POSTCODES:
        return True
    # W1* family (Mayfair, Marylebone, Fitzrovia, Soho): W1 + a letter
    if pc.startswith("W1") and len(pc) >= 3 and pc[2].isalpha():
        return True
    # SW1* family (Belgravia, Knightsbridge, Westminster, Pimlico): SW1 + a letter
    if pc.startswith("SW1") and len(pc) >= 4 and pc[3].isalpha():
        return True
    # Commercial core: Covent Garden/Holborn/Bloomsbury (WC) + the City (EC)
    if pc.startswith("WC") or pc.startswith("EC"):
        return True
    return False

COMMERCIAL_KEYWORDS = [
    "office", "commercial", "retail", "restaurant", "hotel",
    "hospitality", "fit-out", "fitout", "fit out", "shop",
    "cafe", "bar", "club", "gym", "spa", "clinic",
    "showroom", "gallery", "co-working", "workspace", "leisure",
]
RESIDENTIAL_KEYWORDS = [
    "dwelling", "house", "flat", "apartment", "residential",
    "bedroom", "maisonette", "studio", "penthouse", "villa",
]

def classify_sector(desc):
    desc = (desc or "").lower()
    is_comm = bool(_has_term(desc, COMMERCIAL_KEYWORDS))
    is_res = bool(_has_term(desc, RESIDENTIAL_KEYWORDS))
    if is_comm and is_res:
        return "Mixed Use"
    if is_comm:
        return "Commercial"
    return "Residential"

def is_relevant(app):
    """Decide whether an application is worth Shatro's attention.
    Returns (keep: bool, reason: str)."""
    desc = (app.get("description") or "").lower()
    app_type = (app.get("app_type") or "").lower()

    # 0) drop by the council's OWN reference-suffix classification —
    #    the most reliable signal, see EXCLUDE_REF_SUFFIXES above.
    suf = ref_suffix(app.get("ref", ""))
    if suf in EXCLUDE_REF_SUFFIXES:
        return False, f"refsuffix:{suf}"

    # 1) drop amendments / conditions / certs / ads / trees by type
    t = _has_term(app_type, EXCLUDE_TYPES)
    if t:
        return False, f"type:{t}"

    # 2) hard exclusions (amendments, retrospective, pure noise)
    h = _has_term(desc, HARD_EXCLUDE)
    if h:
        return False, f"excluded:{h}"

    # 3) must be commercial or residential building work
    if not _has_term(desc, KEYWORDS):
        return False, "no-keyword"

    # 4) maintenance/minor works — drop unless a substantial signal is present
    m = _has_term(desc, MAINTENANCE_TERMS)
    if m and not _has_term(desc, SUBSTANTIAL_SIGNALS):
        return False, f"maintenance:{m}"

    # 5) high-end area only
    if not is_high_end(app):
        return False, f"area:{outward_code(app) or 'none'}"

    return True, "ok"

def parse_date(s):
    if not s:
        return datetime.now().strftime("%Y-%m-%d")
    for fmt in ["%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"]:
        try:
            return datetime.strptime(str(s)[:10], fmt).strftime("%Y-%m-%d")
        except:
            continue
    return datetime.now().strftime("%Y-%m-%d")

def council_url(borough, ref):
    """Build best available direct link to planning application on council portal."""
    r = ref.replace("/", "%2F")
    urls = {
        # IDOX portals — search page with reference pre-filled
        "Westminster":            f"https://idoxpa.westminster.gov.uk/online-applications/search.do?action=simple&searchType=Application&searchCriteria.reference={r}",
        "Southwark":              f"https://planning.southwark.gov.uk/online-applications/search.do?action=simple&searchType=Application&searchCriteria.reference={r}",
        "Tower Hamlets":          f"https://development.towerhamlets.gov.uk/online-applications/search.do?action=simple&searchType=Application&searchCriteria.reference={r}",
        "City of London":         f"https://www.planning2.cityoflondon.gov.uk/online-applications/search.do?action=simple&searchType=Application&searchCriteria.reference={r}",
        "Lambeth":                f"https://planning.lambeth.gov.uk/online-applications/search.do?action=simple&searchType=Application&searchCriteria.reference={r}",
        "Kensington and Chelsea": f"https://www.rbkc.gov.uk/planning/searches/default.aspx?action=simple&searchType=Application&searchCriteria.reference={r}",
        "Hammersmith and Fulham": f"https://www.lbhf.gov.uk/planning/planning-applications/search-planning-applications?reference={r}",
        "Richmond upon Thames":   f"https://www2.richmond.gov.uk/lbrplanning/Planning_CaseNo.aspx?strCASENO={r}",
        "Merton":                 f"https://planning.merton.gov.uk/Northgate/PlanningExplorer/GeneralSearch.aspx?searchType=Application&ref={r}",
        # Camden — same search page, documents are shown inline
        "Camden":                 f"https://accountforms.camden.gov.uk/planning-search/?search={ref}",
        "Wandsworth":             f"http://planning.wandsworth.gov.uk/Northgate/PlanningExplorer/DisplayAppDetails.aspx?AppNo={ref}",
        # Haringey — Salesforce portal (URL comes from API when available)
        "Haringey":               f"https://publicregister.haringey.gov.uk/pr/s/planning-search?ref={r}",
        # Barnet — publicaccess portal (simpleSearchString actually executes;
        # searchCriteria.reference is ignored on this install)
        "Barnet":                 f"https://publicaccess.barnet.gov.uk/online-applications/simpleSearchResults.do?action=firstPage&searchType=Application&searchCriteria.simpleSearch=true&searchCriteria.simpleSearchString={r}",
        # Islington — Agile Applications portal
        "Islington":              f"https://planning.agileapplications.co.uk/islington/search-applications/results?criteria=%7B%22query%22:%22{ref.replace('/', '%2F')}%22%7D&page=1",
    }
    return urls.get(borough, f"https://idoxpa.westminster.gov.uk/online-applications/search.do?action=simple&searchType=Application&searchCriteria.reference={r}")

# ============================================================
# PLANWIRE LOOKUP — gets direct portal URL + agent details
# ============================================================
# Cache of PlanWire data per borough to avoid repeated API calls
_planwire_cache = {}

def build_planwire_cache(borough, session):
    """Fetch all recent apps from PlanWire for a borough and cache by reference.
    PlanWire sorts by applicationDate desc by default, so most recent apps
    come first — but our apps may be a few days old by the time PlanWire
    ingests them, so we paginate further for high-volume boroughs."""
    council_id = PLANWIRE_COUNCIL.get(borough, "")
    if not council_id or borough in _planwire_cache:
        return
    
    _planwire_cache[borough] = {}
    
    page = 1
    max_pages = 9  # up to 900 applications per borough
    while page <= max_pages:
        try:
            r = session.get(
                PLANWIRE_URL,
                params={"council": council_id, "limit": 100, "page": page},
                headers={"X-API-Key": PLANWIRE_KEY},
                timeout=20,
                verify=False
            )
            if r.status_code != 200:
                break
            data = r.json()
            apps = data.get("data", [])
            if not apps:
                break
            for app in apps:
                ref = app.get("reference", "").replace(" ", "").replace("/", "").upper()
                if ref:
                    _planwire_cache[borough][ref] = {
                        "portal_link": app.get("url", "") or "",
                        "docs_link":   (app.get("url", "") or "").replace("activeTab=summary", "activeTab=documents"),
                        "architect":   app.get("agentName", "") or "",
                    }
            total_pages = data.get("meta", {}).get("pages", 1)
            if page >= total_pages or len(apps) < 100:
                break
            page += 1
        except Exception:
            break
    print(f"  PlanWire cache: {len(_planwire_cache.get(borough, {}))} apps for {borough}")

def planwire_lookup(ref, borough, postcode, session):
    """Look up application in PlanWire cache."""
    if borough not in _planwire_cache:
        build_planwire_cache(borough, session)
    
    clean_ref = ref.replace(" ", "").replace("/", "").upper()
    result = _planwire_cache.get(borough, {}).get(clean_ref, {})
    return result

# ============================================================
# STEP 1: FETCH APPLICATIONS
# ============================================================
def _make_docs_link(url):
    """Convert a planning portal summary URL to a documents tab URL."""
    if not url:
        return ""
    # IDOX portals
    if "activeTab=summary" in url:
        return url.replace("activeTab=summary", "activeTab=documents")
    if "activeTab=details" in url:
        return url.replace("activeTab=details", "activeTab=documents")
    # Haringey Salesforce portal
    if "publicregister.haringey.gov.uk" in url and "tabset" not in url:
        return url + "?tabset-3892f=3"
    # Wandsworth Northgate portal
    if "planning.wandsworth.gov.uk" in url:
        sep = "&" if "?" in url else "?"
        return url + sep + "tab=documents"
    # Camden, Islington, Barnet — documents shown inline, same URL
    return url
def fetch_all():
    apps = []
    date_from = (datetime.now() - timedelta(days=DAYS_BACK)).strftime("%d/%m/%Y")
    print(f"\nSTEP 1 — Fetching last {DAYS_BACK} days from Planning London Datahub...")

    for borough in TARGET_BOROUGHS:
        try:
            body = {
                "query": {"bool": {"must": [
                    {"term": {"lpa_name.raw": borough}},
                    {"range": {"valid_date": {"gte": date_from}}}
                ]}},
                "_source": ["*"],
                "size": 100
            }
            r = requests.post(PLD_URL, headers=PLD_HEADERS, json=body, timeout=20)
            hits = r.json().get("hits", {}).get("hits", []) if r.status_code == 200 else []
            print(f"  {borough}: {len(hits)}")

            for hit in hits:
                src = hit.get("_source", {})
                ref = src.get("lpa_app_no", "")
                if not ref:
                    continue
                addr = " ".join(filter(None, [
                    src.get("site_number", ""),
                    src.get("street_name", ""),
                    src.get("locality", ""),
                    src.get("site_name", ""),
                    src.get("postcode", ""),
                ])).strip()
                borough_name = src.get("lpa_name", borough)
                apps.append({
                    "name":        f"{ref} — {addr[:60]}",
                    "ref":         ref,
                    "address":     addr,
                    "postcode":    (src.get("postcode", "") or "").strip(),
                    "description": src.get("description", "") or "",
                    "date":        parse_date(src.get("valid_date", "")),
                    "borough":     borough_name,
                    "app_type":    src.get("application_type", "") or "",
                    "portal_link": council_url(borough_name, ref) if borough_name == "Camden" else (src.get("url_planning_app") or council_url(borough_name, ref)),
                    "docs_link":   council_url(borough_name, ref) if borough_name == "Camden" else _make_docs_link(src.get("url_planning_app") or council_url(borough_name, ref)),
                    "architect":   "",
                    "arch_co":     "",
                    "arch_email":  "",
                    "arch_phone":  "",
                    "arch_url":    "",
                })
        except Exception as e:
            print(f"  {borough}: Error — {e}")
        time.sleep(0.2)

    return apps

# ============================================================
# IDOX "Public Access" direct-link resolver
# ------------------------------------------------------------
# On many IDOX portals a reference-based search URL just lands on the
# search form (Barnet ignores searchCriteria.reference). So we run a live
# search — by reference first, then by full postcode — read the real
# keyVal off the results, and build the proper applicationDetails link,
# i.e. exactly the link you get by searching the postcode and clicking
# the matching result.
# ============================================================
IDOX_BASES = {
    "Barnet":                 "https://publicaccess.barnet.gov.uk",
    "Westminster":            "https://idoxpa.westminster.gov.uk",
    "Southwark":              "https://planning.southwark.gov.uk",
    "Tower Hamlets":          "https://development.towerhamlets.gov.uk",
    "City of London":         "https://www.planning2.cityoflondon.gov.uk",
    "Lambeth":                "https://planning.lambeth.gov.uk",
    "Kensington and Chelsea": "https://idoxpa.rbkc.gov.uk",
}

def _keyval_from(text):
    m = re.search(r"keyVal=([A-Za-z0-9]+)", text or "")
    return m.group(1) if m else ""

def _match_ref_anchor(anchors, ref):
    """From a list of <a> tags, return the one carrying our reference.
    Checks each anchor's OWN text first (so links sharing a parent block
    don't borrow a sibling's reference), then the parent block, then
    falls back to the first anchor."""
    if not anchors:
        return None
    norm = (ref or "").replace(" ", "").replace("/", "").upper()
    if norm:
        for a in anchors:  # pass 1: the anchor's own text
            t = a.get_text(" ", strip=True).upper().replace(" ", "").replace("/", "")
            if norm in t:
                return a
        for a in anchors:  # pass 2: the immediate parent block
            p = a.find_parent()
            if p:
                t = p.get_text(" ", strip=True).upper().replace(" ", "").replace("/", "")
                if norm in t:
                    return a
    return anchors[0]

def idox_resolve(base, ref, postcode, session):
    """Return {'keyval','portal_link','docs_link','contacts_link'} or {}.
    Reference search usually returns one result and redirects straight to
    the application; postcode search returns a list, so we pick the row
    that carries our reference."""
    norm_ref = (ref or "").replace(" ", "").replace("/", "").upper()
    terms = [t for t in (ref, postcode) if t]
    endpoints = [
        f"{base}/online-applications/simpleSearchResults.do",
        f"{base}/online-applications/search.do",
    ]
    for term in terms:
        for ep in endpoints:
            params = {
                "action": "firstPage" if "simpleSearchResults" in ep else "simple",
                "searchType": "Application",
                "searchCriteria.caseType": "Application",
                "searchCriteria.simpleSearch": "true",
                "searchCriteria.simpleSearchString": term,
            }
            try:
                resp = session.get(ep, params=params, timeout=15,
                                   verify=False, allow_redirects=True)
                if resp.status_code != 200:
                    continue
                # single result -> redirected straight to the application page
                kv = _keyval_from(resp.url)
                if not kv:
                    soup = BeautifulSoup(resp.text, "html.parser")
                    links = [a for a in soup.select("a[href*='applicationDetails.do']")
                             if "keyVal=" in a.get("href", "")]
                    if not links:
                        continue
                    chosen = _match_ref_anchor(links, ref)
                    kv = _keyval_from(chosen.get("href")) if chosen else ""
                if kv:
                    detail = f"{base}/online-applications/applicationDetails.do?keyVal={kv}"
                    return {
                        "keyval":        kv,
                        "portal_link":   detail + "&activeTab=summary",
                        "docs_link":     detail + "&activeTab=documents",
                        "contacts_link": detail + "&activeTab=contacts",
                    }
            except Exception:
                continue
    return {}

def _parse_contacts_soup(soup):
    """Pull agent/applicant details out of an IDOX contacts page."""
    res = {}
    for row in soup.find_all("tr"):
        cells = row.find_all(["th", "td"])
        if len(cells) < 2:
            continue
        key = cells[0].get_text(strip=True).lower()
        val = " ".join(cells[1].get_text(separator=" ", strip=True).split())
        if not val or len(val) > 150:
            continue
        if "agent name" in key or "agent's name" in key:
            res["architect"] = val
        elif "agent company" in key or "agent organisation" in key:
            res["arch_co"] = val
        elif "agent email" in key:
            res["arch_email"] = val
        elif "agent phone" in key or "agent tel" in key:
            res["arch_phone"] = val
        elif "applicant name" in key and not res.get("architect"):
            res["architect"] = val
        elif "applicant company" in key and not res.get("arch_co"):
            res["arch_co"] = val
    if not res.get("arch_email"):
        m = re.search(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}", soup.get_text())
        if m and "gov.uk" not in m.group():
            res["arch_email"] = m.group()
    if res.get("arch_co"):
        res["arch_url"] = f"https://www.google.com/search?q={requests.utils.quote(res['arch_co'] + ' architect London')}"
    return res

def idox_contacts(contacts_url, session):
    """Fetch + parse an IDOX contacts tab by its real (keyVal) URL."""
    try:
        resp = session.get(contacts_url, timeout=15, verify=False)
        if resp.status_code != 200:
            return {}
        return _parse_contacts_soup(BeautifulSoup(resp.text, "html.parser"))
    except Exception:
        return {}

# ============================================================
# NORTHGATE Planning Explorer resolver (Wandsworth, Richmond, Merton)
# ------------------------------------------------------------
# Wandsworth and Richmond accept the reference directly via a shim page.
# Merton needs a form search (ASP.NET WebForms), which is best-effort.
# ============================================================
NORTHGATE = {
    "Wandsworth": {
        "direct": "https://planning.wandsworth.gov.uk/Northgate/PlanningExplorer/DisplayAppDetails.aspx?AppNo={enc}",
    },
    "Richmond upon Thames": {
        "direct": "https://www2.richmond.gov.uk/lbrplanning/Planning_CaseNo.aspx?strCASENO={enc}",
    },
    "Merton": {
        "base":   "https://planning.merton.gov.uk/Northgate/PlanningExplorer",
        "search": "https://planning.merton.gov.uk/Northgate/PlanningExplorer/GeneralSearch.aspx",
    },
}

def _page_has_ref(text, ref):
    norm = (ref or "").replace(" ", "").replace("/", "").upper()
    body = (text or "").upper().replace(" ", "").replace("/", "")
    return bool(norm) and norm in body

def _aspnet_fields(soup):
    f = {}
    for name in ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION",
                 "__EVENTTARGET", "__EVENTARGUMENT"):
        el = soup.find("input", {"name": name})
        f[name] = el.get("value", "") if el else ""
    return f

def _northgate_detail_link(base, html, ref):
    soup = BeautifulSoup(html or "", "html.parser")
    cands = [a for a in soup.find_all("a", href=True)
             if "StdDetails.aspx" in a["href"] or "DisplayAppDetails.aspx" in a["href"]]
    a = _match_ref_anchor(cands, ref)
    return urljoin(base.rstrip("/") + "/", a["href"]) if a else None

def northgate_resolve(borough, ref, session):
    cfg = NORTHGATE.get(borough)
    if not cfg or not ref:
        return {}
    enc = requests.utils.quote(ref, safe="")

    # 1) Direct shim (Wandsworth / Richmond resolve the reference themselves)
    if cfg.get("direct"):
        url = cfg["direct"].format(enc=enc)
        try:
            r = session.get(url, timeout=15, verify=False)
            if r.status_code == 200 and _page_has_ref(r.text, ref):
                final = r.url or url
                return {"portal_link": final, "docs_link": final}
        except Exception:
            pass

    # 2) Merton — ASP.NET form search (best-effort)
    if cfg.get("search"):
        try:
            g = session.get(cfg["search"], timeout=15, verify=False)
            soup = BeautifulSoup(g.text, "html.parser")
            data = _aspnet_fields(soup)
            field = next((n for n in (
                "ctl00$ContentPlaceHolder1$txtApplicationNumber",
                "txtApplicationNumber",
                "ctl00$MainContent$txtApplicationNumber",
            ) if soup.find("input", {"name": n})), None)
            if field:
                data[field] = ref
                btn = next((n for n in (
                    "ctl00$ContentPlaceHolder1$csbtnSearch",
                    "ctl00$ContentPlaceHolder1$btnSearch",
                    "btnSearch",
                ) if soup.find(["input", "a"], {"name": n})), None)
                if btn:
                    data[btn] = "Search"
                rs = session.post(cfg["search"], data=data, timeout=20,
                                  verify=False, allow_redirects=True)
                link = _northgate_detail_link(cfg["base"], rs.text, ref)
                if link:
                    return {"portal_link": link, "docs_link": link}
        except Exception:
            pass
    return {}

# ============================================================
# AGILE Applications resolver (Islington)
# ============================================================
def agile_resolve(authority, ref, postcode, session):
    base = f"https://planning.agileapplications.co.uk/{authority}"
    norm = (ref or "").replace(" ", "").replace("/", "").upper()
    for term in [t for t in (ref, postcode) if t]:
        crit = requests.utils.quote(json.dumps({"query": term}))
        url = f"{base}/search-applications/results?criteria={crit}&page=1"
        try:
            r = session.get(url, timeout=15, verify=False)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            cands = [a for a in soup.find_all("a", href=True)
                     if "/planning-application" in a["href"]
                     or "/applications/" in a["href"]]
            if not cands:
                continue
            chosen = _match_ref_anchor(cands, ref)
            full = urljoin(base + "/", chosen["href"])
            return {"portal_link": full, "docs_link": full}
        except Exception:
            continue
    return {}

# ============================================================
# STEP 2: ARCHITECT LOOKUP (IDOX CONTACTS TAB)
# ============================================================
def get_architect(ref, borough, session):
    if not ref or len(ref) < 4:
        return {}
    r_enc = requests.utils.quote(ref)
    CONTACTS = {
        "Westminster":            f"https://idoxpa.westminster.gov.uk/online-applications/applicationDetails.do?activeTab=contacts&keyVal=Westminster%2F{r_enc}",
        "Camden":                 f"https://camdocs.camden.gov.uk/online-applications/applicationDetails.do?activeTab=contacts&keyVal=Camden%2F{r_enc}",
        "Southwark":              f"https://planning.southwark.gov.uk/online-applications/applicationDetails.do?activeTab=contacts&keyVal=SOUTHWARK%2F{r_enc}",
        "Tower Hamlets":          f"https://development.towerhamlets.gov.uk/online-applications/applicationDetails.do?activeTab=contacts&keyVal=TOWERHAMLETS%2F{r_enc}",
        "City of London":         f"https://www.planning2.cityoflondon.gov.uk/online-applications/applicationDetails.do?activeTab=contacts&keyVal=CITYOFLONDON%2F{r_enc}",
        "Lambeth":                f"https://planning.lambeth.gov.uk/online-applications/applicationDetails.do?activeTab=contacts&keyVal=LAMBETH%2F{r_enc}",
        "Kensington and Chelsea": f"https://idoxpa.rbkc.gov.uk/online-applications/applicationDetails.do?activeTab=contacts&keyVal=RBKC%2F{r_enc}",
        "Wandsworth":             f"https://planning.wandsworth.gov.uk/WAM/showCaseFile.do?action=show&appType=Planning&appNumber={r_enc}",
    }
    url = CONTACTS.get(borough, "")
    if not url:
        return {}
    try:
        resp = session.get(url, timeout=15, verify=False)
        if resp.status_code != 200:
            return {}
        return _parse_contacts_soup(BeautifulSoup(resp.text, "html.parser"))
    except Exception:
        return {}

def enrich(apps, session):
    print(f"\nSTEP 2 — Enriching with PlanWire + IDOX ({len(apps)} applications)...")
    found_url = 0
    found_arch = 0

    for i, app in enumerate(apps):
        ref      = app.get("ref", "")
        borough  = app.get("borough", "")
        address  = app.get("address", "")

        # Full postcode (e.g. 'N20 0XE') — prefer the API field, else parse
        full_pc = (app.get("postcode") or "").strip()
        if not full_pc and address:
            m = re.search(r'\b([A-Z]{1,2}[0-9][0-9A-Z]?\s?[0-9][A-Z]{2})\b', address.upper())
            if m:
                full_pc = m.group(1).strip()
        # Outward code only (used by PlanWire matching below)
        postcode = full_pc.split()[0] if full_pc else (address.strip().split()[-1] if address else "")

        # Step 1: Try PlanWire to get direct portal URL + agent
        # Skip for Camden - PlanWire returns the same broken NECSWS format as the
        # primary API; our council_url() fallback (accountforms.camden.gov.uk) works instead
        pw = planwire_lookup(ref, borough, postcode, session) if borough != "Camden" else {}
        if pw:
            if pw.get("portal_link"):
                app["portal_link"] = pw["portal_link"]
                app["docs_link"]   = pw.get("docs_link", _make_docs_link(pw["portal_link"]))
                found_url += 1
            if pw.get("architect"):
                app["architect"] = pw["architect"]
                found_arch += 1

        # Step 1b: If we still don't have a direct link, resolve it live on
        # the borough's portal. Always falls back to the existing link.
        if RESOLVE_DIRECT_LINKS and "keyVal=" not in (app.get("portal_link") or ""):
            resolved = {}
            if borough in IDOX_BASES:
                idx = idox_resolve(IDOX_BASES[borough], ref, full_pc, session)
                if idx.get("portal_link"):
                    resolved = idx
                    # architect straight from the real contacts tab
                    if not app.get("architect") and not app.get("arch_co") and idx.get("contacts_link"):
                        details = idox_contacts(idx["contacts_link"], session)
                        if details and any(details.values()):
                            app.update({k: v for k, v in details.items()
                                        if v and not app.get(k)})
                            if details.get("architect") or details.get("arch_co"):
                                found_arch += 1
            elif borough in NORTHGATE:
                resolved = northgate_resolve(borough, ref, session)
            elif borough == "Islington":
                resolved = agile_resolve("islington", ref, full_pc, session)
            # (Haringey runs on a JavaScript Salesforce portal that a plain
            #  HTTP request can't read; it keeps its search deep-link and
            #  relies on PlanWire for the direct URL once ingested.)

            if resolved.get("portal_link"):
                app["portal_link"] = resolved["portal_link"]
                app["docs_link"]   = resolved.get("docs_link", resolved["portal_link"])
                found_url += 1

        # Step 2: If no architect yet, try IDOX contacts tab
        if not app.get("architect") and not app.get("arch_co"):
            details = get_architect(ref, borough, session)
            if details and any(details.values()):
                app.update(details)
                if details.get("architect") or details.get("arch_co"):
                    found_arch += 1

        # Build Google search for architect if name found
        arch_name = app.get("architect") or app.get("arch_co")
        if arch_name and not app.get("arch_url"):
            app["arch_url"] = f"https://www.google.com/search?q={requests.utils.quote(arch_name + ' architect London')}"

        # Status line
        pl = app.get("portal_link", "")
        direct_markers = ("keyVal=", "agileapplications", "publicregister",
                          "StdDetails.aspx", "DisplayAppDetails.aspx",
                          "Planning_CaseNo.aspx", "/planning-application")
        parts = []
        if pl and any(mk in pl for mk in direct_markers):
            parts.append("direct URL")
        if arch_name:
            parts.append(f"agent: {arch_name[:30]}")
        if parts:
            print(f"  [{i+1}/{len(apps)}] {' | '.join(parts)} — {ref}")
        else:
            print(f"  [{i+1}/{len(apps)}] Not found — {ref}")

        time.sleep(0.3)

    print(f"  Direct URLs: {found_url}/{len(apps)} | Architects: {found_arch}/{len(apps)}")
    return apps

# ============================================================
# STEP 3: ADD TO MONDAY.COM
# ============================================================
def monday_api(query, variables):
    try:
        r = requests.post(
            "https://api.monday.com/v2",
            json={"query": query, "variables": variables},
            headers={"Authorization": MONDAY_API_TOKEN, "Content-Type": "application/json", "API-Version": "2024-01"},
            timeout=15
        )
        data = r.json()
        if r.status_code != 200:
            print(f"  API error: HTTP {r.status_code} — {r.text[:200]}")
        return data if data is not None else {}
    except Exception as e:
        print(f"  API exception: {e}")
        return {}

def get_existing():
    q = """query ($b: ID!) { boards(ids: [$b]) { items_page(limit: 500) { items { name } } } }"""
    res = monday_api(q, {"b": MONDAY_BOARD_ID})
    if res is None:
        return set()
    items = res.get("data", {}).get("boards", [{}])[0].get("items_page", {}).get("items", [])
    return set(i["name"][:50] for i in items)

def add_item(app):
    arch_name = app.get("architect") or app.get("arch_co") or ""
    arch_url  = app.get("arch_url") or ""
    if not arch_url and arch_name:
        arch_url = f"https://www.google.com/search?q={requests.utils.quote(arch_name + ' architect London')}"

    desc = f"[{app['app_type']}] " if app.get("app_type") else ""
    desc += app.get("description", "")
    if app.get("address"):
        desc += f"\n\nAddress: {app['address']}"
    extras = []
    if app.get("architect"):  extras.append(f"Architect: {app['architect']}")
    if app.get("arch_co"):    extras.append(f"Company: {app['arch_co']}")
    if app.get("arch_email"): extras.append(f"Email: {app['arch_email']}")
    if app.get("arch_phone"): extras.append(f"Phone: {app['arch_phone']}")
    if extras:
        desc += "\n\n" + "\n".join(extras)

    col = {
        COL_DESC:   {"text": desc[:2000]},
        COL_ARCH:   arch_name,
        COL_DATE:   {"date": app.get("date", datetime.now().strftime("%Y-%m-%d"))},
        COL_LINK:   {"url": app.get("portal_link", ""), "text": "View Application"},
        COL_STATUS: {"label": "New"},
        COL_SECTOR: {"label": classify_sector(app.get("description", ""))},
    }
    if app.get("app_type"):
        col[COL_APP_TYPE] = {"label": app["app_type"][:75]}
    # Address column - use simple text format to avoid API errors
    # location column requires specific format, skip for now
    if arch_url:
        col[COL_WEBSITE] = {"url": arch_url, "text": arch_name or "Search"}
    if app.get("docs_link"):
        col[COL_DOCS] = {"url": app["docs_link"], "text": "View Documents"}

    mut = """
    mutation ($b: ID!, $g: String!, $n: String!, $cv: JSON!) {
        create_item(board_id: $b, group_id: $g, item_name: $n, column_values: $cv,
                     create_labels_if_missing: true) { id }
    }
    """
    res = monday_api(mut, {
        "b": MONDAY_BOARD_ID,
        "g": get_group(app.get("borough", "")),
        "n": app["name"][:255],
        "cv": json.dumps(col),
    })
    if res is None:
        print(f"  Error: No response from Monday.com")
        return None
    create_item_data = (res.get("data") or {}).get("create_item") or {}
    item_id = create_item_data.get("id") if isinstance(create_item_data, dict) else None
    if item_id:
        print(f"  Added: {app['name'][:70]}")
        return item_id
    errors = res.get("errors", [])
    for err in errors:
        if "DAILY_LIMIT_EXCEEDED" in str(err.get("extensions", {}).get("code", "")):
            retry = err.get("extensions", {}).get("retry_in_seconds", 0)
            print(f"\n  Monday.com daily limit hit. Resets in ~{retry//3600} hours.")
            return "LIMIT"
    if errors:
        print(f"  Failed: {errors[:200]}")
    elif not item_id:
        print(f"  Failed silently — response: {str(res)[:200]}")
    return None

# ============================================================
# STEP 4: EMAIL
# ============================================================
def send_email(apps):
    try:
        lines = [
            "Good morning Gulnara,", "",
            f"Daily planning scan — {len(apps)} new relevant applications.", "",
            "=" * 60, "",
        ]
        for i, app in enumerate(apps[:30], 1):
            arch = app.get("architect") or app.get("arch_co") or "Not listed"
            lines += [
                f"{i}. {app.get('address', app['name'])}",
                f"   Ref:          {app['ref']}",
                f"   Borough:      {app.get('borough', '')}",
                f"   Type:         {app.get('app_type', '')}",
                f"   Description:  {app.get('description', '')[:150]}",
                f"   Date:         {app.get('date', '')}",
                f"   Architect:    {arch}",
            ]
            if app.get("arch_co") and app["arch_co"] != arch:
                lines.append(f"   Company:      {app['arch_co']}")
            if app.get("arch_email"):
                lines.append(f"   Email:        {app['arch_email']}")
            if app.get("arch_phone"):
                lines.append(f"   Phone:        {app['arch_phone']}")
            lines += [f"   Portal:       {app.get('portal_link', '')}", ""]
        if len(apps) > 30:
            lines.append(f"... and {len(apps)-30} more.")
        lines += ["", "=" * 60, f"Board: https://shatro.monday.com/boards/{MONDAY_BOARD_ID}", "", "Shatro Planning Scraper"]

        msg = MIMEMultipart()
        msg["From"] = EMAIL_FROM
        msg["To"] = EMAIL_TO
        msg["Subject"] = f"Shatro — {len(apps)} New Planning Applications — {datetime.now().strftime('%d %b %Y')}"
        msg.attach(MIMEText("\n".join(lines), "plain"))
        with smtplib.SMTP(EMAIL_SMTP, EMAIL_PORT) as s:
            s.starttls()
            s.login(EMAIL_FROM, EMAIL_PASSWORD)
            s.send_message(msg)
        print(f"  Email sent to {EMAIL_TO}")
    except Exception as e:
        print(f"  Email error: {e}")

def send_heartbeat(scanned, relevant, added, note=""):
    """Short daily 'the job ran' email, sent on days with no new additions."""
    if not SEND_HEARTBEAT:
        return
    try:
        body = [
            "Good morning Gulnara,", "",
            f"Daily planning scan ran OK — {added} new applications added today.",
            "",
            f"  Scanned (last {DAYS_BACK} days):  {scanned}",
            f"  Passed filter:            {relevant}",
            f"  Added to board:           {added}",
        ]
        if note:
            body += ["", note]
        body += ["", f"Board: https://shatro.monday.com/boards/{MONDAY_BOARD_ID}",
                 "", "Shatro Planning Scraper"]
        msg = MIMEMultipart()
        msg["From"] = EMAIL_FROM
        msg["To"] = EMAIL_TO
        msg["Subject"] = (f"Shatro — Daily scan OK, {added} new — "
                          f"{datetime.now().strftime('%d %b %Y')}")
        msg.attach(MIMEText("\n".join(body), "plain"))
        with smtplib.SMTP(EMAIL_SMTP, EMAIL_PORT) as s:
            s.starttls()
            s.login(EMAIL_FROM, EMAIL_PASSWORD)
            s.send_message(msg)
        print(f"  Heartbeat email sent to {EMAIL_TO}")
    except Exception as e:
        print(f"  Heartbeat email error: {e}")

# ============================================================
# DIGEST MODE — run separately (e.g. 10:00) to email whatever the
# 09:00 scrape run added to the board, read straight from Monday
# rather than re-scraping. Window defaults to 20h to comfortably
# cover the gap between the two scheduled runs.
# ============================================================
DIGEST_WINDOW_HOURS = int(_cfg("DIGEST_WINDOW_HOURS", "20"))

def get_recent_items(hours):
    q = """
    query ($b: ID!) {
      boards(ids: [$b]) {
        items_page(limit: 200) {
          items {
            name
            created_at
            group { title }
            column_values(ids: [
              "%s", "%s", "%s", "%s", "%s", "%s"
            ]) { id text }
          }
        }
      }
    }
    """ % (COL_DESC, COL_ARCH, COL_LINK, COL_DATE, COL_APP_TYPE, COL_SECTOR)
    res = monday_api(q, {"b": MONDAY_BOARD_ID})
    items = ((res or {}).get("data", {}).get("boards", [{}])[0]
             .get("items_page", {}).get("items", []))
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    recent = []
    for it in items:
        try:
            created = datetime.strptime(it["created_at"][:19], "%Y-%m-%dT%H:%M:%S")
        except Exception:
            continue
        if created < cutoff:
            continue
        cv = {c["id"]: c.get("text", "") for c in it.get("column_values", [])}
        recent.append({
            "name":        it["name"],
            "borough":     (it.get("group") or {}).get("title", ""),
            "description": cv.get(COL_DESC, ""),
            "architect":   cv.get(COL_ARCH, ""),
            "portal_link": cv.get(COL_LINK, ""),
            "date":        cv.get(COL_DATE, ""),
            "app_type":    cv.get(COL_APP_TYPE, ""),
            "sector":      cv.get(COL_SECTOR, ""),
        })
    return recent

def run_digest():
    print("=" * 60)
    print(f"SHATRO Planning Digest — {datetime.now().strftime('%d %b %Y %H:%M')}")
    print(f"Window: last {DIGEST_WINDOW_HOURS}h | Board: {MONDAY_BOARD_ID}")
    print("=" * 60)
    items = get_recent_items(DIGEST_WINDOW_HOURS)
    print(f"Found {len(items)} item(s) added in the last {DIGEST_WINDOW_HOURS}h.")
    if not items:
        send_heartbeat(0, 0, 0, "No new items added since the last digest.")
        return
    send_email(items)

# ============================================================
# MAIN
# ============================================================
def run():
    print("=" * 60)
    print(f"SHATRO Planning Scraper — {datetime.now().strftime('%d %b %Y %H:%M')}")
    print(f"Looking back: {DAYS_BACK} days | Board: {MONDAY_BOARD_ID}")
    print("=" * 60)

    # Step 1: Fetch
    apps = fetch_all()
    print(f"\nTotal: {len(apps)}")
    if not apps:
        print("No applications found.")
        send_heartbeat(0, 0, 0, "Data source returned no applications.")
        return

    # Filter
    filtered, rejects = [], {}
    for a in apps:
        keep, reason = is_relevant(a)
        if keep:
            filtered.append(a)
        else:
            tag = reason.split(":")[0]
            rejects[tag] = rejects.get(tag, 0) + 1
    print(f"Relevant (high-end residential + commercial): {len(filtered)}")
    if rejects:
        summary = ", ".join(f"{k}={v}" for k, v in sorted(rejects.items()))
        print(f"  Dropped {sum(rejects.values())} — {summary}")
    if not filtered:
        print("No relevant applications.")
        send_heartbeat(len(apps), 0, 0, "Nothing passed the filter today.")
        return

    # Deduplicate
    seen, unique = set(), []
    for a in filtered:
        k = a["name"][:50]
        if k not in seen:
            seen.add(k)
            unique.append(a)
    print(f"Unique: {len(unique)}")

    # Check duplicates on board
    print("\nChecking Monday.com...")
    existing = get_existing()
    new_apps = [a for a in unique if a["name"][:50] not in existing]
    print(f"New to add: {len(new_apps)}")
    if not new_apps:
        print("Board is up to date.")
        send_heartbeat(len(apps), len(unique), 0,
                       "Board already up to date — no new applications today.")
        return

    # Step 2: Architect lookup
    session = requests.Session()
    session.verify = False
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    new_apps = enrich(new_apps, session)

    # Step 3: Add to Monday.com
    print(f"\nSTEP 3 — Adding {len(new_apps)} to Monday.com...")
    added, added_apps, stop = 0, [], False
    for app in new_apps:
        if stop:
            break
        result = add_item(app)
        if result == "LIMIT":
            stop = True
        elif result:
            added += 1
            added_apps.append(app)
        time.sleep(0.3)

    print(f"\n{'='*60}")
    print(f"Complete — Added: {added}")
    print(f"Board: https://shatro.monday.com/boards/{MONDAY_BOARD_ID}")
    print(f"{'='*60}")

    # Step 4: Email
    if added_apps:
        print(f"\nSTEP 4 — Sending email...")
        send_email(added_apps)
    else:
        print("\nNo applications added — sending heartbeat.")
        send_heartbeat(len(apps), len(unique), 0,
                       "Items were found but none added (daily limit or add error).")

def _london_hour():
    """Current hour in Europe/London local time (handles BST/GMT automatically)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/London")).hour
    except Exception:
        return datetime.utcnow().hour  # fallback: treat as UTC

if __name__ == "__main__":
    import sys
    mode = "auto"
    for arg in sys.argv[1:]:
        if arg.startswith("--mode="):
            mode = arg.split("=", 1)[1]

    if mode == "auto":
        # Manual runs (workflow_dispatch, or running the script directly)
        # should always actually do something — the hour-gate is only for
        # the unattended hourly cron trigger.
        if _cfg("GITHUB_EVENT_NAME", "") == "workflow_dispatch":
            print("Manually triggered (workflow_dispatch) — running scrape directly, ignoring time gate.")
            mode = "scrape"
        else:
            # Workflow runs hourly; the script decides what to do based on
            # Europe/London local time, so 9am/10am stay correct across BST/GMT.
            hour = _london_hour()
            if hour == 9:
                mode = "scrape"
            elif hour == 10:
                mode = "digest"
            else:
                print(f"London local hour is {hour}:00 — nothing scheduled, exiting.")
                sys.exit(0)

    if mode == "digest":
        run_digest()
    else:
        run()
