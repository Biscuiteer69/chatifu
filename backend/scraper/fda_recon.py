import requests
import json
import os

def fetch_fda_targets(company_name="Stryker", limit=20):
    print(f"🕵️‍♂️ Hitting the FDA AccessGUDID API for {company_name} devices...")
    
    # We query the FDA API for a specific manufacturer to get their catalog/reference numbers.
    url = f"https://accessgudid.nlm.nih.gov/api/v2/devices/search.json?search=companyName:{company_name}"
    
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        results = data.get("search-result", {}).get("device", [])
        
        targets = []
        for item in results[:limit]:
            # The catalog/reference number is what doctors actually type into the eIFU portals
            catalog = item.get("catalogNumber", "")
            brand = item.get("brandName", "Unknown")
            device_desc = item.get("deviceDescription", "No description")
            
            if catalog:
                targets.append({
                    "catalog_number": catalog,
                    "brand": brand,
                    "description": device_desc
                })
        
        # Save our hit list
        output_path = os.path.join(os.path.dirname(__file__), 'data', f'{company_name.lower()}_targets.json')
        with open(output_path, "w") as f:
            json.dump(targets, f, indent=2)
            
        print(f"✅ Extracted {len(targets)} verified product codes. Hit list saved to {output_path}")
        return targets
        
    except Exception as e:
        print(f"❌ Failed to extract FDA data: {e}")
        return []

if __name__ == "__main__":
    targets = fetch_fda_targets("Stryker", 10)
    for t in targets:
        print(f"- {t['catalog_number']} ({t['brand']})")