"""
Run this ONCE to download the Amiri font next to page_renderer.py.
(page_renderer.py now looks for it at an absolute path relative to its own
location, so this must be run from -- or extracted into -- the same folder
as page_renderer.py.)

    python setup_font.py
"""

import zipfile
from io import BytesIO
from pathlib import Path

import requests

URL = "https://github.com/aliftype/amiri/releases/download/1.001/Amiri-1.001.zip"

if __name__ == "__main__":
    target_dir = Path(__file__).resolve().parent / "amiri_font"
    expected_file = target_dir / "Amiri-1.001" / "Amiri-Regular.ttf"

    if expected_file.exists():
        print(f"Font already present at {expected_file}")
    else:
        print("Downloading Amiri font...")
        resp = requests.get(URL, timeout=60)
        resp.raise_for_status()
        with zipfile.ZipFile(BytesIO(resp.content)) as zf:
            zf.extractall(target_dir)
        if expected_file.exists():
            print(f"Done. Font installed at {expected_file}")
        else:
            print("Extraction finished but the expected file wasn't found -- "
                  "check the extracted folder structure under:", target_dir)
