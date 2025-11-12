from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import httpx, re, json, asyncio, csv
from io import StringIO

app = FastAPI(title="VRM Property Scraper")

# Allow any frontend to call your API (adjust if you want to lock down origins)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Base site configuration
BASE_URL = "https://www.vrmproperties.com/Properties-For-Sale?currentpage="
pattern = re.compile(r"let model\s*=\s*(\{[\s\S]*?\});")

# Only keep these fields
FIELDS = [
    "assetId","assetReferenceId","addressLine1","city","state","zip","county",
    "displayPrice","squareFootage","bedrooms","bathrooms","lotSize","lotSizeSource",
    "propertyType","assetListingStatus","isVendeeFinancing","listingStartDate",
    "isNewListing","mediaGuid","mediaId"
]

def filter_fields(item):
    """Select only desired keys."""
    return {k: item.get(k) for k in FIELDS}

async def fetch_page(client, page):
    """Fetch a single listing page and extract property JSON."""
    try:
        r = await client.get(f"{BASE_URL}{page}", timeout=20)
        match = pattern.search(r.text)
        if not match:
            print(f"No JSON found on page {page}")
            return []
        cleaned = re.sub(r",(\s*[}\]])", r"\1", match.group(1))
        model = json.loads(cleaned)
        return [filter_fields(p) for p in model.get("properties", [])]
    except Exception as e:
        print(f"Error on page {page}: {e}")
        return []

@app.get("/")
def root():
    return {"message": "VRM Scraper API is live!", "endpoints": ["/scrape", "/export.csv"]}

@app.get("/scrape")
async def scrape():
    """Scrape all pages and return combined JSON."""
    async with httpx.AsyncClient() as client:
        tasks = [fetch_page(client, p) for p in range(1, 107)]  # 1–106 pages
        results = await asyncio.gather(*tasks)
    all_props = [p for page in results for p in page]
    return {"count": len(all_props), "properties": all_props}

@app.get("/export.csv")
async def export_csv():
    """Scrape and return a downloadable CSV."""
    data = await scrape()
    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=FIELDS)
    writer.writeheader()
    writer.writerows(data["properties"])
    output.seek(0)
    return StreamingResponse(
        output,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=vrm_listings.csv"}
    )

