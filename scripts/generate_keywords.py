"""
Reads data/CMW_Tender_Keywords.xlsx and merges new keywords into
config/keywords.yaml without overwriting existing tier/category tags.

Usage: python scripts/generate_keywords.py

Any keyword in the xlsx that already exists in keywords.yaml (case-insensitive)
is left untouched. New keywords are appended to the Tier 1 block with a
placeholder tag — review and re-tag them manually.
"""

import sys
from pathlib import Path

import openpyxl
import yaml

XLSX_PATH = Path("data/CMW_Tender_Keywords.xlsx")
YAML_PATH = Path("config/keywords.yaml")


def load_xlsx_keywords() -> list[str]:
    if not XLSX_PATH.exists():
        print(f"ERROR: {XLSX_PATH} not found. Commit the xlsx file to data/ first.")
        sys.exit(1)
    wb = openpyxl.load_workbook(XLSX_PATH, read_only=True, data_only=True)
    ws = wb.active
    keywords = []
    for row in ws.iter_rows(values_only=True):
        for cell in row:
            if cell and isinstance(cell, str) and cell.strip():
                keywords.append(cell.strip())
    wb.close()
    return keywords


def load_existing_yaml() -> dict:
    if not YAML_PATH.exists():
        return {"keywords": []}
    with open(YAML_PATH) as f:
        return yaml.safe_load(f) or {"keywords": []}


def main() -> None:
    xlsx_keywords = load_xlsx_keywords()
    existing_data = load_existing_yaml()
    existing_keywords = existing_data.get("keywords", [])

    existing_lower = {kw["keyword"].lower() for kw in existing_keywords}

    new_entries = []
    for kw in xlsx_keywords:
        if kw.lower() not in existing_lower:
            new_entries.append({
                "keyword": kw,
                "category": 1,   # placeholder — review and update
                "tier": 1,        # placeholder — default to Tier 1 per spec
                "notes": "AUTO-ADDED from xlsx — confirm tier and category",
            })
            print(f"  + {kw}")

    if not new_entries:
        print("No new keywords found in xlsx. keywords.yaml is up to date.")
        return

    existing_keywords.extend(new_entries)
    existing_data["keywords"] = existing_keywords

    with open(YAML_PATH, "w") as f:
        yaml.dump(existing_data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    print(f"\nAdded {len(new_entries)} new keywords to {YAML_PATH}")
    print("Review and update tier/category for each AUTO-ADDED entry.")


if __name__ == "__main__":
    main()
