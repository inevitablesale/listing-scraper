from fastapi import FastAPI, Header, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
import httpx, re, json, asyncio, os, logging, random, time, sys
from datetime import datetime
from pathlib import Path
from fake_useragent import UserAgent, settings

# --------------------------------------------------------------------
# App setup
# --------------------------------------------------------------------
app = FastAPI(title="Property Scraper API")

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
pattern = re.compile(r"let\s+model\s*=\s*(\{.*?\});", re.DOTALL)

SCRAPE_ACTIVE = True
KNOWN_IDS_FILE = Path("known_ids.json")
DATA_DIR = Path("data")
LATEST_FILE = DATA_DIR / "properties.json"

# --------------------------------------------------------------------
# Logging setup: file + stdout (so Render shows logs live)
# --------------------------------------------------------------------
DATA_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler("scrape.log"),
        logging.StreamHandler(sys.stdout)
    ]
)

# --------------------------------------------------------------------
# Scrape progress tracker
# --------------------------------------------------------------------
PROGRESS = {
    "running": False,
    "page": 0,
    "total": 0,
    "started_at": None,
    "finished_at": None,
    "duration_seconds": None,
    "message": "Idle"
}

# --------------------------------------------------------------------
# Safe user-agent initialization
# --------------------------------------------------------------------
try:
    ua = UserAgent()
except Exception:
    settings.HTTP_TIMEOUT = 2.0
    ua = UserAgent(use_cache_server=False)

# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------
def make_slug(address, city, state, zip_code):
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
    DATA_DIR.mkdir(exist_ok=True)
    LATEST_FILE.write_text(json.dumps(data, indent=2))
    timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H-%M-%S")
    snapshot = DATA_DIR / f"properties_{timestamp}.json"
    snapshot.write_text(json.dumps(data, indent=2))
    logging.info(f"✅ Saved new snapshot: {snapshot}")

    snapshots = sorted(DATA_DIR.glob("properties_*.json"), key=os.path.getmtime, reverse=True)
    for old in snapshots[5:]:
        try:
            old.unlink()
            logging.info(f"🧹 Deleted old snapshot: {old}")
        except Exception as e:
            logging.error(f"Cleanup error {old}: {e}")

async def parse_model(text):
    match = pattern.search(text)
    if not match:
        return None
    cleaned = re.sub(r",(\s*[}\]])", r"\1", match.group(1))
    try:
        return json.loads(cleaned)
    except Exception as e:
        logging.error(f"JSON decode error: {e}")
        return None

# --------------------------------------------------------------------
# Core scraper
# --------------------------------------------------------------------
async def fetch_page(client, page, session_headers):
    """Fetch one search results page and extract property data."""
    global SCRAPE_ACTIVE
    if not SCRAPE_ACTIVE:
        return {"properties": [], "meta": None, "pagination": {}}

    try:
        r = await client.get(f"{BASE_URL}{page}", timeout=20, headers=session_headers)
        model = await parse_model(r.text)
        if not model:
            logging.warning(f"No JSON found on page {page}")
            return {"properties": [], "meta": None, "pagination": {}}

        props = []
        for p in model.get("properties", []):
            slug = make_slug(p.get("addressLine1"), p.get("city"), p.get("state"), p.get("zip"))
            p["propertyUrl"] = f"{DETAIL_BASE}{p.get('assetId')}/{slug}" if slug else None
            p["imageUrl"] = (
                f"https://s3.amazonaws.com/photos.vrmresales.com/{p['mediaGuid']}.jpg"
                if p.get("mediaGuid") else None
            )
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
                "portfolios": model.get("portfolios", []),
            }

        return {"properties": props, "meta": meta, "pagination": pagination}

    except Exception as e:
        logging.error(f"Error fetching page {page}: {e}")
        return {"properties": [], "meta": None, "pagination": {}}


async def scrape_all_pages():
    """Scrape dynamically based on detected totalPages value, with human pacing and persistence."""
    global SCRAPE_ACTIVE
    SCRAPE_ACTIVE = True

    known = load_known_ids()
    new_ids = set()
    start_time = time.time()

    PROGRESS.update({
        "running": True,
        "page": 0,
        "total": 0,
        "started_at": datetime.utcnow().isoformat(),
        "finished_at": None,
        "message": "Starting scrape..."
    })

    # Pick a session-level User-Agent
    session_ua = random.choice([ua.chrome, ua.firefox, ua.safari])
    session_headers = {
        "User-Agent": session_ua,
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.vrmproperties.com/Properties-For-Sale",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Connection": "keep-alive",
    }

    logging.info(f"🧠 Using session User-Agent: {session_ua}")

    async with httpx.AsyncClient(follow_redirects=True, headers=session_headers) as client:
        first = await fetch_page(client, 1, session_headers)
        total_pages = first["pagination"].get("totalPages", 1)
        PROGRESS["total"] = total_pages
        logging.info(f"🔍 Detected {total_pages} total pages.")

        results = []
        for p in range(2, total_pages + 1):
            if not SCRAPE_ACTIVE:
                break

            elapsed = time.time() - start_time
            PROGRESS.update({"page": p, "message": f"Scraping page {p}/{total_pages}"})
            logging.info(f"🧭 Scraping page {p}/{total_pages} (elapsed: {elapsed:0.1f}s)")

            # Human-like delay with micro jitter
            delay = random.uniform(1.5, 3.5) + random.random() * 0.2
            await asyncio.sleep(delay)

            page_result = await fetch_page(client, p, session_headers)
            results.append(page_result)

            # Longer cooldown every 10 pages
            if p % 10 == 0:
                cooldown = random.uniform(5.0, 12.0) + random.random()
                logging.info(f"🌙 Cooldown pause for {cooldown:.1f}s at page {p}.")
                await asyncio.sleep(cooldown)

    all_props = first["properties"] + [p for page in results for p in page["properties"]]
    meta = first["meta"] or next((r["meta"] for r in results if r["meta"]), {})

    for p in all_props:
        pid = p.get("assetId")
        if pid and pid not in known:
            logging.info(f"🆕 New property discovered: {pid} {p.get('addressLine1')} {p.get('city')}")
            new_ids.add(pid)

    if new_ids:
        save_known_ids(known.union(new_ids))

    end_time = time.time()
    duration = end_time - start_time

    data = {
        "count": len(all_props),
        "new_discoveries": len(new_ids),
        "properties": all_props,
        "meta": meta,
        "pagination": first["pagination"],
        "fetched_at": datetime.utcnow().isoformat(),
        "scrape_started_at": datetime.utcfromtimestamp(start_time).isoformat(),
        "scrape_finished_at": datetime.utcfromtimestamp(end_time).isoformat(),
        "duration_seconds": round(duration, 2),
    }

    save_properties_to_disk(data)
    logging.info(f"✅ Full scrape complete — {len(all_props)} properties fetched in {duration:.1f}s.")
    PROGRESS.update({
        "running": False,
        "finished_at": datetime.utcnow().isoformat(),
        "duration_seconds": round(duration, 2),
        "message": f"Completed {len(all_props)} properties in {duration:.1f}s"
    })
    return data

# --------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------
@app.get("/")
def root():
    return {
        "message": "Property Scraper API is live!",
        "endpoints": [
            "POST /properties (start async scrape)",
            "GET /progress (check status)",
            "GET /stored",
            "GET /latest-image-urls",
            "POST /kill",
            "GET /health"
        ],
    }

@app.post("/properties")
async def start_properties_scrape(background_tasks: BackgroundTasks, x_api_key: str = Header(None)):
    expected_secret = os.getenv("ZAPIER_SECRET")
    if expected_secret and x_api_key != expected_secret:
        raise HTTPException(status_code=401, detail="Unauthorized")

    if PROGRESS.get("running"):
        return {"message": "Scrape already running", "progress": PROGRESS}

    logging.info("🚀 Background scrape triggered via /properties")
    background_tasks.add_task(scrape_all_pages)
    return {"message": "Scrape started", "progress": PROGRESS}

@app.get("/progress")
def get_progress():
    return PROGRESS

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
    PROGRESS.update({"running": False, "message": "Scrape manually stopped"})
    logging.warning("🚨 Kill switch activated — scraping halted.")
    return {"message": "Scraper stopped successfully."}

@app.get("/health")
def health_check():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}
