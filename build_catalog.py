"""
Run this ONCE (or periodically, to pick up new books) before starting the
bot. Downloads turath's full library dump and builds the local SQLite
catalog used for /bookinfo and /getscan autocomplete.

    python build_catalog.py                 # builds turath_catalog.db if missing
    python build_catalog.py --force          # rebuilds even if it exists
    python build_catalog.py --out mydb.db    # custom output path
"""

import argparse

from catalog import build_catalog

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="turath_catalog.db")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    print("Downloading turath's book library and building catalog -- this may take a while...")
    path = build_catalog(args.out, force=args.force)
    print(f"Catalog ready at: {path}")
