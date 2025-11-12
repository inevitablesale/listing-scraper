# 🏠 Property Scraper API

A lightweight **FastAPI** web service for scraping and storing paginated property listings from a public real estate website.

It dynamically detects pagination, stores all property data as structured JSON, and exposes multiple routes for testing, syncing, and analysis.

---

## 🚀 Features

- **Dynamic Pagination Detection** – Automatically determines total listing pages.  
- **FastAPI + HTTPX** – Asynchronous requests for efficient, concurrent scraping.  
- **Randomized User Agents** – Simulates natural browser traffic for safer scraping.  
- **Snapshot Storage** – Saves each run to `/data/properties_<timestamp>.json` and keeps the latest as `/data/properties.json`.  
- **Automatic Cleanup** – Keeps only the 5 most recent snapshots.  
- **Secure Access** – Protects routes with an `X-API-Key` header using `ZAPIER_SECRET`.  
- **Diagnostics & Debugging Tools** – Includes `/test-page` and `/debug-html` for troubleshooting.  
- **Graceful Kill Switch** – Stop scraping instantly using `/kill`.

