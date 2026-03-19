import os
import sys
import time
from pathlib import Path
import requests
from dotenv import load_dotenv
from supabase import create_client, Client
from duckduckgo_search import DDGS

# Load environment variables
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / '.env')

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("❌ Missing Supabase credentials in backend/.env")
    sys.exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
IFU_DIR = BASE_DIR / "data" / "ifus"
IFU_DIR.mkdir(parents=True, exist_ok=True)

# Scraper configuration
TARGET_COMPANY = "stryker" # Case insensitive match
BATCH_LIMIT = 5 # Try 5 to start with

def download_pdf(url: str, output_path: Path) -> bool:
    try:
        # Use a realistic User-Agent to avoid immediate blocking
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        response = requests.get(url, headers=headers, timeout=15, stream=True)
        response.raise_for_status()
        
        # Check if it's actually a PDF
        content_type = response.headers.get('Content-Type', '').lower()
        if 'application/pdf' not in content_type and not url.lower().endswith('.pdf'):
            print(f"  ⚠️ Warning: URL doesn't look like a PDF ({content_type})")
            
        with open(output_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        return True
    except Exception as e:
        print(f"  ❌ Failed to download {url}: {e}")
        return False

def search_for_ifu(company: str, brand: str, catalog: str) -> str:
    """Uses DuckDuckGo to find a PDF IFU for the given details."""
    # Clean up inputs
    company = company.split(',')[0].strip() if company else ""
    brand = brand.strip() if brand else ""
    
    # Query variations, starting specific
    queries = [
        f"{company} {catalog} \"Instructions for use\" filetype:pdf",
        f"{company} {brand} {catalog} IFU filetype:pdf",
        f"{company} {catalog} manual filetype:pdf"
    ]
    
    with DDGS() as ddgs:
        for q in queries:
            print(f"  🔍 Querying: {q}")
            try:
                # DDGS rate limits heavily, sleep is essential
                time.sleep(2)
                results = list(ddgs.text(q, max_results=3))
                for res in results:
                    url = res.get('href', '')
                    if url.lower().endswith('.pdf') or 'pdf' in url.lower():
                        return url
            except Exception as e:
                print(f"  ⚠️ DDG Error: {e}")
    return None

def main():
    print(f"🚀 Targeting eIFUs for company matching: {TARGET_COMPANY}")
    
    # Query Supabase for devices belonging to the target company
    # that have a catalog number
    response = supabase.table("devices")\
        .select("primary_di, company_name, brand_name, catalog_number")\
        .ilike("company_name", f"%{TARGET_COMPANY}%")\
        .neq("catalog_number", "")\
        .limit(BATCH_LIMIT)\
        .execute()
        
    devices = response.data
    if not devices:
        print("❌ No devices found for that company.")
        return

    print(f"📦 Found {len(devices)} devices. Starting scrape...\n")
    
    success_count = 0
    
    for idx, device in enumerate(devices):
        primary_di = device['primary_di']
        company = device['company_name']
        brand = device['brand_name']
        catalog = device['catalog_number']
        
        print(f"[{idx+1}/{len(devices)}] {company} | {brand} | SKU: {catalog}")
        
        pdf_path = IFU_DIR / f"{primary_di}_{catalog.replace('/', '_')}.pdf"
        if pdf_path.exists():
            print(f"  ✅ Already downloaded. Skipping.")
            continue
            
        pdf_url = search_for_ifu(company, brand, catalog)
        
        if pdf_url:
            print(f"  📄 Found PDF: {pdf_url}")
            print(f"  ⬇️ Downloading...")
            if download_pdf(pdf_url, pdf_path):
                print(f"  ✅ Saved to {pdf_path.name}")
                success_count += 1
        else:
            print(f"  ❌ No PDF found.")
            
        print("-" * 40)
        
    print(f"\n🎉 Finished scraping batch. Downloaded {success_count}/{len(devices)} IFUs.")

if __name__ == "__main__":
    main()
