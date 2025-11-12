from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import httpx, re, json, asyncio, os, logging
from datetime import datetime
from pathlib import Path

# --------------------------------------------------------------------
# App setup
# --------------------------------------------------------------------
app = FastAPI(title="VRM Property Scraper")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --------------------------------------------------------------------
# Constants & Globals
# --------------------------------------------------------------------
BASE_URL = "https://www.vrmproperties.com/Properties-For-Sale?currentpage="
DETAIL_BASE = "https://www.vrmproperties.com/Property-For-Sale/"
pattern = re.compile(r"let model\s*=\s*(\{[\s\S]*?\});")
TOTAL_PAGES = 106

SCRAPE_ACTIVE = True
KNOWN_IDS_FILE = Path("known_ids.json")

logging.basicConfig(
    filename="scrape.log",
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------
def make_slug(address, city, state, zip_code):
    """Create SEO-friendly slug for property URL."""
    if not all([address, city, state, zip_code]):
        return None
    raw = f"{address} {city} {state} {zip_code}"
    slug = re.sub(r"[^a-z0-9]+", "-", raw.lower().strip())
    return slug.strip("-")

def load_known_ids():
    """Load known property IDs for logging new discoveries."""
    if KNOWN_IDS_FILE.exists():
        return set(json.loads(KNOWN_IDS_FILE.read_text()))
    return set()

def save_known_ids(ids):
    KNOWN_IDS_FILE.write_text(json.dumps(list(ids)))

# --------------------------------------------------------------------
# Core scraper
# --------------------------------------------------------------------
async def fetch_page(client, page):
    """Fetch one VRM listing page and extract property + metadata JSON."""
    global SCRAPE_ACTIVE
    if not SCRAPE_ACTIVE:
        return {"properties": [], "meta": None}

    await asyncio.sleep(0.5)
    headers = {
        "User-Agent": "VRM-Scraper/1.0 (+https://yourdomain.com/contact)",
        "From": "admin@yourdomain.com"
    }

    try:
        r = await client.get(f"{BASE_URL}{page}", timeout=20, headers=headers)
        match = pattern.search(r.text)
        if not match:
            logging.warning(f"No JSON found on page {page}")
            return {"properties": [], "meta": None}

        # Parse the inline model JSON block
        cleaned = re.sub(r",(\s*[}\]])", r"\1", match.group(1))
        model = json.loads(cleaned)

        # Preserve full structure and add propertyUrl field
        properties = []
        for p in model.get("properties", []):
            slug = make_slug(p.get("addressLine1"), p.get("city"), p.get("state"), p.get("zip"))
            p["propertyUrl"] = f"{DETAIL_BASE}{p.get('assetId')}/{slug}" if slug else None
            properties.append(p)

        # Grab metadata from the first page only
        meta = None
        if page == 1:
            meta = {
                "searchStates": model.get("searchStates", []),
                "portfolios": model.get("portfolios", [])
            }

        return {"properties": properties, "meta": meta}

    except Exception as e:
        logging.error(f"Error fetching page {page}: {e}")
        return {"properties": [], "meta": None}

async def scrape_all_pages():
    """Main scrape logic with concurrency + logging new properties."""
    global SCRAPE_ACTIVE
    SCRAPE_ACTIVE = True

    known = load_known_ids()
    new_ids = set()

    async with httpx.AsyncClient() as client:
        sem = asyncio.Semaphore(3)
        async def worker(p):
            async with sem:
                return await fetch_page(client, p)
        results = await asyncio.gather(*[worker(p) for p in range(1, TOTAL_PAGES + 1)])

    all_props = [p for page in results for p in page["properties"]]
    meta = next((r["meta"] for r in results if r["meta"]), {})

    for p in all_props:
        pid = p.get("assetId")
        if pid and pid not in known:
            logging.info(f"🆕 New property discovered: {pid} {p.get('addressLine1')} {p.get('city')}")
            new_ids.add(pid)

    if new_ids:
        save_known_ids(known.union(new_ids))

    return {
        "count": len(all_props),
        "new_discoveries": len(new_ids),
        "properties": all_props,
        "meta": meta,
        "fetched_at": datetime.utcnow().isoformat()
    }

# --------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------
@app.get("/")
def root():
    return {
        "message": "VRM Scraper API is live!",
        "endpoints": ["/properties", "/kill"],
        "note": "Use X-API-Key header for authentication"
    }

@app.get("/properties")
async def get_properties(x_api_key: str = Header(None)):
    """Fetch all listings, preserving full JSON structure (nulls included)."""
    expected_secret = os.getenv("ZAPIER_SECRET")
    if expected_secret and x_api_key != expected_secret:
        raise HTTPException(status_code=401, detail="Unauthorized")

    data = await scrape_all_pages()
    return data  # returned as-is; FastAPI keeps all nulls

@app.post("/kill")
def kill_scraper(x_api_key: str = Header(None)):
    """Kill switch to stop scraping."""
    global SCRAPE_ACTIVE
    expected_secret = os.getenv("ZAPIER_SECRET")
    if expected_secret and x_api_key != expected_secret:
        raise HTTPException(status_code=401, detail="Unauthorized")

    SCRAPE_ACTIVE = False
    logging.warning("🚨 Kill switch activated — scraping halted.")
    return {"message": "Scraper stopped successfully."}
