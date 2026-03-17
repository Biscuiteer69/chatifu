import os
import csv
import sys
from dotenv import load_dotenv
from supabase import create_client, Client
from pathlib import Path

# Load environment variables
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / '.env')

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("❌ Missing Supabase URL or Key in backend/.env")
    sys.exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
DEVICE_FILE = BASE_DIR / "data" / "fda_delimited" / "device.txt"

BATCH_SIZE = 1000

def main():
    if not DEVICE_FILE.exists():
        print(f"❌ Could not find {DEVICE_FILE}. Did the extraction fail?")
        sys.exit(1)

    print("🔌 Connected to Supabase. Starting bulk upload...")
    
    batch = []
    total_inserted = 0
    
    with open(DEVICE_FILE, 'r', encoding='utf-8') as f:
        # device.txt is pipe delimited
        reader = csv.DictReader(f, delimiter='|')
        
        for i, row in enumerate(reader):
            # Extract just the essential columns we care about for search
            # (To save database space and speed up upload)
            device_data = {
                "primary_di": row.get("PrimaryDI"),
                "brand_name": row.get("brandName"),
                "company_name": row.get("companyName"),
                "model_number": row.get("versionModelNumber"),
                "catalog_number": row.get("catalogNumber"),
                "description": row.get("deviceDescription")
            }
            batch.append(device_data)
            
            # When we hit the batch size, push to Supabase
            if len(batch) >= BATCH_SIZE:
                try:
                    # Upsert batch (ignores duplicates by primary_di)
                    supabase.table("devices").upsert(batch, on_conflict="primary_di").execute()
                    total_inserted += len(batch)
                    
                    # Print progress on the same line
                    sys.stdout.write(f"\rUploaded: {total_inserted:,} devices...")
                    sys.stdout.flush()
                except Exception as e:
                    print(f"\n❌ Error uploading batch {total_inserted}: {e}")
                    # Don't exit, just skip this broken batch and keep going
                    pass
                
                # Clear the batch
                batch = []
        
        # Insert any remaining records
        if batch:
            try:
                supabase.table("devices").insert(batch).execute()
                total_inserted += len(batch)
                print(f"\rUploaded: {total_inserted:,} devices... DONE!")
            except Exception as e:
                print(f"\n❌ Error uploading final batch: {e}")

if __name__ == "__main__":
    main()