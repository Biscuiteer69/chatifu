import requests
import json
import os
import time

# -------------------------------------------------------------------------------------
# STRYKER eIFU SCRAPER
# -------------------------------------------------------------------------------------

BASE_URL = "https://api-public.qarad.eifu.online/api/v1/business-units/0/product-types/1/products"

def search_stryker_api(query, country="US"):
    print(f"🎯 Querying Stryker eIFU API for: {query}")
    
    # We hijack the exact parameters from the Chrome Developer Network trace
    params = {
        "audience": "HCP",
        "page": 0,
        "size": 5 # Limit to top 5 results per query for now
    }
    
    # These headers trick the API into thinking we are a real Chrome browser
    headers = {
        "accept": "*/*",
        "accept-language": "en",
        "content-type": "application/json; charset=utf-8",
        "origin": "https://labeling.stryker.com",
        "referer": "https://labeling.stryker.com/",
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
    }
    
    # The JSON body defines the actual search
    payload = {
        "currentDate": "2026-03-15",
        "attributes": [
            {"name": "cross-field-search", "value": query}
        ],
        "country": country
    }
    
    try:
        response = requests.post(
            BASE_URL,
            params=params,
            headers=headers,
            json=payload,
            timeout=15
        )
        response.raise_for_status()
        
        data = response.json()
        print(f"✅ Found {data.get('totalElements', 0)} total matching devices.")
        
        return data.get("content", [])
        
    except Exception as e:
        print(f"❌ API Request failed: {e}")
        return []

if __name__ == "__main__":
    # Test the API wrapper
    results = search_stryker_api("drill")
    
    if results:
        print("\n--- TOP RESULTS ---")
        for idx, item in enumerate(results):
            # The API returns a lot of metadata; we want the product names and document links
            name = item.get("name", "Unknown Product")
            gtin = item.get("gtin", "N/A")
            print(f"{idx+1}. {name} (GTIN: {gtin})")
            
            # The documents array holds the IFUs
            docs = item.get("documents", [])
            for doc in docs:
                doc_type = doc.get("documentType", {}).get("name", "Unknown")
                doc_lang = doc.get("language", {}).get("isoCode", "en")
                
                # We want English Instructions for Use
                if doc_lang.lower() == "en" and "instructions for use" in doc_type.lower():
                    print(f"   -> Found English IFU: {doc.get('title')}")
                    # The URL requires a base domain to fetch
                    file_uuid = doc.get("fileUuid")
                    if file_uuid:
                        download_link = f"https://api-public.qarad.eifu.online/api/v1/business-units/0/product-types/1/products/{item.get('id')}/documents/{doc.get('id')}/file?language=en&country=US&audience=HCP"
                        print(f"   -> Download Link: {download_link}")
    else:
        print("No results found.")