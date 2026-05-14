"""
split_turnover.py — Split turnover_{YYYY}.json into monthly files
================================================================
Splits turnover_{YYYY}.json into turnover_{YYYY}_{MM}.json files.
Each monthly file stays well under Cloudflare Pages' 25MB limit.

Usage:
  python split_turnover.py --year 2025
  python split_turnover.py --year 2026
  python split_turnover.py --year 2025 --year 2026
"""

import argparse
import json
import os
from datetime import date

def split_year(year: int):
    src = f"turnover_{year}.json"
    if not os.path.exists(src):
        print(f"ERROR: {src} not found")
        return

    with open(src, encoding="utf-8") as f:
        lib = json.load(f)

    by_date = lib.get("by_date", {})
    if not by_date:
        print(f"ERROR: {src} has no by_date")
        return

    # Group by month
    by_month = {}
    for ds, stocks in by_date.items():
        mm = ds[5:7]  # YYYY-MM-DD → MM
        by_month.setdefault(mm, {})[ds] = stocks

    print(f"turnover_{year}.json: {len(by_date)} dates → {len(by_month)} months")

    for mm, month_data in sorted(by_month.items()):
        out_path = f"turnover_{year}_{mm}.json"
        # Merge into existing monthly file if present
        if os.path.exists(out_path):
            with open(out_path, encoding="utf-8") as f:
                existing = json.load(f)
            existing_data = existing.get("by_date", {})
            existing_data.update(month_data)
            month_data = dict(sorted(existing_data.items()))
        out = {
            "meta": {
                "year":         year,
                "month":        int(mm),
                "last_updated": date.today().isoformat(),
                "total_days":   len(month_data),
                "source":       lib.get("meta", {}).get("source", "hkex"),
            },
            "by_date": month_data,
        }
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
        mb = os.path.getsize(out_path) / 1e6
        print(f"  {out_path}: {len(month_data)} days  {mb:.2f} MB")

    print(f"Done splitting turnover_{year}.json")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, action="append", required=True)
    args = ap.parse_args()
    for y in args.year:
        split_year(y)
