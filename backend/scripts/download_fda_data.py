import os
import urllib.request
import zipfile
import sys
from pathlib import Path

# Setup paths
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
FDA_DIR = DATA_DIR / "fda_delimited"
ZIP_URL = "https://accessgudid.nlm.nih.gov/release_files/download/AccessGUDID_Delimited_Full_Release_20260302.zip"
ZIP_FILE_PATH = DATA_DIR / "fda_delimited_20260302.zip"

def report_progress(block_num, block_size, total_size):
    downloaded = block_num * block_size
    percent = downloaded / total_size * 100
    # Print progress to the console on the same line
    sys.stdout.write(f"\rDownloading: {percent:.1f}% ({downloaded / (1024 * 1024):.1f} MB / {total_size / (1024 * 1024):.1f} MB)")
    sys.stdout.flush()

def main():
    print(f"Ensuring data directories exist...")
    DATA_DIR.mkdir(exist_ok=True)
    FDA_DIR.mkdir(exist_ok=True)

    print(f"Downloading FDA Delimited Release...")
    if not ZIP_FILE_PATH.exists():
        urllib.request.urlretrieve(ZIP_URL, ZIP_FILE_PATH, reporthook=report_progress)
        print(f"\nDownload complete: {ZIP_FILE_PATH}")
    else:
        print(f"\nFile already exists: {ZIP_FILE_PATH}")

    print("Unzipping the database files...")
    with zipfile.ZipFile(ZIP_FILE_PATH, 'r') as zip_ref:
        zip_ref.extractall(FDA_DIR)
    
    print("Extraction complete. Here are the files we have:")
    for file in FDA_DIR.glob("*.txt"):
        print(f"- {file.name} ({(file.stat().st_size / (1024 * 1024)):.2f} MB)")

if __name__ == "__main__":
    main()
