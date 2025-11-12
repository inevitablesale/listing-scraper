from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import httpx, re, json, asyncio, os, logging, random
from datetime import datetime
from pathlib import Path
from fake_useragent import UserAgent

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

SCRAPE_ACTIVE = True
KNOWN_IDS_FILE = Path("known_ids.json")
DATA_DIR = Path("data")
LATEST_FILE = DATA_DIR / "properties.json"

logging.basicConfig(
    filename="scrape.log",
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

ua = UserAgent()

# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------
def random_headers():
    """Generate a realistic browser header."""
    ua_string = random.choice([ua.chrome, ua.firefox, ua.safari])
    return {
        "User-Agent": ua_string,
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.vrmproperties.com/Properties-For-Sale",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Connection": "keep-alive",
    }

def make_slug(address, city, state, zip_code):
    """Create SEO-friendly slug for property URL."""
    if not all([address, city, state, zip_code]):
        return None
    raw = f"{address} {city} {state} {zip_code}"
    slug = re.sub(r"[^a-z0-9]+", "-", raw.lower().strip())
    return slug.strip("-")

def load_known_ids():
    if KNOWN_IDS_FILE.exists():
        return set(json.loads(KNOWN_IDS_FILE.read_text()))
    return set()

def save_known_ids(ids):
    KNOWN_IDS_FILE.write_text(json.dumps(list(ids)))

def save_properties_to_disk(data):
    """Save full dataset and auto-clean old snapshots (keep last 5)."""
    DATA_DIR.mkdir(exist_ok=True)
    LATEST_FILE.write_text(json.dumps(data, indent=2))
    timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H-%M-%S")
    snapshot = DATA_DIR / f"properties_{timestamp}.json"
    snapshot.write_text(json.dumps(data, indent=2))
    logging.info(f"✅ Saved new snapshot: {snapshot}")

    # Cleanup old snapshots
    snapshots = sorted(DATA_DIR.glob("properties_*.json"), key=os.path.getmtime, reverse=True)
    for old in snapshots[5:]:
        try:
            old.unlink()
            logging.info(f"🧹 Deleted old snapshot: {old}")
        except Exception as e:
            logging.error(f"Cleanup error {old}: {e}")

async def parse_model(text):
    """Extract the embedded 'model' JSON from HTML."""
    match = pattern.search(text)
    if not match:
        return None
    cleaned = re.sub(r",(\s*[}\]])", r"\1", match.group(1))
    return json.loads(cleaned)

# --------------------------------------------------------------------
# Core scraper
# --------------------------------------------------------------------
async def fetch_page(client, page):
    """Fetch one search results page and extract property data."""
    global SCRAPE_ACTIVE
    if not SCRAPE_ACTIVE:
        return {"properties": [], "meta": None, "pagination": {}}

    try:
        headers = random_headers()
        r = await client.get(f"{BASE_URL}{page}", timeout=20, headers=headers)
        model = await parse_model(r.text)
        if not model:
            logging.warning(f"No JSON found on page {page}")
            return {"properties": [], "meta": None, "pagination": {}}

        props = []
        for p in model.get("properties", []):
            slug = make_slug(p.get("addressLine1"), p.get("city"), p.get("state"), p.get("zip"))
            p["propertyUrl"] = f"{DETAIL_BASE}{p.get('assetId')}/{slug}" if slug else None
            if p.get("mediaGuid"):
                p["imageUrl"] = f"https://s3.amazonaws.com/photos.vrmresales.com/{p['mediaGuid']}.jpg"
            else:
                p["imageUrl"] = None
            props.append(p)

        pagination = {
            "currentPage": model.get("currentPage"),
            "totalPages": model.get("totalPages"),
            "count": model.get("count"),
            "pageSize": model.get("pageSize"),
        }

        meta = None
        if page == 1:
            meta = {
                "searchStates": model.get("searchStates", []),
                "portfolios": model.get("portfolios", [])
            }

        return {"properties": props, "meta": meta, "pagination": pagination}

    except Exception as e:
        logging.error(f"Error fetching page {page}: {e}")
        return {"properties": [], "meta": None, "pagination": {}}

async def scrape_all_pages():
    """Scrape dynamically based on detected totalPages value."""
    global SCRAPE_ACTIVE
    SCRAPE_ACTIVE = True

    known = load_known_ids()
    new_ids = set()

    async with httpx.AsyncClient() as client:
        # Step 1: Get first page to detect total pages dynamically
        first = await fetch_page(client, 1)
        total_pages = first["pagination"].get("totalPages", 1)
        logging.info(f"🔍 Detected {total_pages} total pages.")

        # Step 2: Sequential fetching with random human-like delays
        results = []
        for p in range(2, total_pages + 1):
            if not SCRAPE_ACTIVE:
                break

            delay = random.uniform(1.0, 3.0)
            await asyncio.sleep(delay)

            page_result = await fetch_page(client, p)
            results.append(page_result)

            if p % 10 == 0:
                cooldown = random.uniform(5.0, 10.0)
                logging.info(f"🌙 Cooldown pause for {cooldown:.1f}s at page {p}.")
                await asyncio.sleep(cooldown)

    # Combine all properties and metadata
    all_props = first["properties"] + [p for page in results for p in page["properties"]]
    meta = first["meta"] or next((r["meta"] for r in results if r["meta"]), {})

    # Log new discoveries
    for p in all_props:
        pid = p.get("assetId")
        if pid and pid not in known:
            logging.info(f"🆕 New property discovered: {pid} {p.get('addressLine1')} {p.get('city')}")
            new_ids.add(pid)

    if new_ids:
        save_known_ids(known.union(new_ids))

    data = {
        "count": len(all_props),
        "new_discoveries": len(new_ids),
        "properties": all_props,
        "meta": meta,
        "pagination": first["pagination"],
        "fetched_at": datetime.utcnow().isoformat()
    }

    save_properties_to_disk(data)
    return data

# --------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------
@app.get("/")
def root():
    return {
        "message": "VRM Scraper API is live!",
        "endpoints": ["/properties", "/stored", "/latest-image-urls", "/kill"],
        "note": "Use X-API-Key header for authentication"
    }

@app.get("/properties")
async def get_properties(x_api_key: str = Header(None)):
    expected_secret = os.getenv("ZAPIER_SECRET")
    if expected_secret and x_api_key != expected_secret:
        raise HTTPException(status_code=401, detail="Unauthorized")

    data = await scrape_all_pages()
    return data

@app.get("/stored")
def get_stored(x_api_key: str = Header(None)):
    expected_secret = os.getenv("ZAPIER_SECRET")
    if expected_secret and x_api_key != expected_secret:
        raise HTTPException(status_code=401, detail="Unauthorized")

    if not LATEST_FILE.exists():
        raise HTTPException(status_code=404, detail="No stored dataset found.")
    return json.loads(LATEST_FILE.read_text())

@app.get("/latest-image-urls")
def get_latest_image_urls(x_api_key: str = Header(None)):
    expected_secret = os.getenv("ZAPIER_SECRET")
    if expected_secret and x_api_key != expected_secret:
        raise HTTPException(status_code=401, detail="Unauthorized")

    if not LATEST_FILE.exists():
        raise HTTPException(status_code=404, detail="No stored dataset found.")

    data = json.loads(LATEST_FILE.read_text())
    urls = [p["imageUrl"] for p in data.get("properties", []) if p.get("imageUrl")]
    return {"count": len(urls), "image_urls": urls}

@app.post("/kill")
def kill_scraper(x_api_key: str = Header(None)):
    global SCRAPE_ACTIVE
    expected_secret = os.getenv("ZAPIER_SECRET")
    if expected_secret and x_api_key != expected_secret:
        raise HTTPException(status_code=401, detail="Unauthorized")

    SCRAPE_ACTIVE = False
    logging.warning("🚨 Kill switch activated — scraping halted.")
    return {"message": "Scraper stopped successfully."}
