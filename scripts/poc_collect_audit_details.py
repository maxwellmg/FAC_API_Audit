#!/usr/bin/env python3
"""
Proof-of-concept: fetch all available API data for a list of audits.

Usage examples:
  # provide comma-separated report ids
  python scripts/poc_collect_audit_details.py --ids 12345,67890

  # read first N report ids from the generated General.csv
  python scripts/poc_collect_audit_details.py --from-csv "FAC API Data Dictionary CSVs/General.csv" --id-column report_id --count 5

Requirements:
  pip install requests pandas

Output:
  - poc_results/<report_id>.json  (all endpoints returned)
  - poc_results/csvs/<endpoint>.csv (combined rows across audits)
"""
import argparse
import os
import json
import requests
from collections import defaultdict
from typing import List

BASE_URL = "https://api.fac.gov"

# Prefer environment variable, otherwise read from a local ignored file '.fac_api_key'
API_KEY = os.environ.get("FAC_API_KEY")
if not API_KEY:
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    key_path = os.path.join(repo_root, ".fac_api_key")
    if os.path.exists(key_path):
        try:
            with open(key_path, "r", encoding="utf-8") as fh:
                API_KEY = fh.read().strip()
        except Exception:
            API_KEY = None
    else:
        API_KEY = None

# Endpoints to try (common table names from the data dictionary)
ENDPOINTS = [
    "general",
    "federal_awards",
    "findings",
    "findings_text",
    "corrective_action_plans",
    "passthrough",
    "secondary_auditors",
    "additional_ueis",
    "additional_eins",
    "notes_to_sefa",
    "resubmission",
]


def read_ids_from_csv(path: str, id_column: str, count: int) -> List[str]:
    import csv

    ids = []
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for r in reader:
            if id_column not in r:
                raise SystemExit(f"Column '{id_column}' not found in {path}")
            ids.append(str(r[id_column]))
            if count and len(ids) >= count:
                break
    return ids


def fetch_endpoint_for_id(session: requests.Session, endpoint: str, report_id: str):
    # try to fetch all fields for rows matching report_id
    url = f"{BASE_URL}/{endpoint}?report_id=eq.{report_id}&select=*"
    try:
        r = session.get(url, timeout=15)
    except requests.RequestException as e:
        return {"error": str(e), "status_code": None}

    if r.status_code == 200:
        try:
            return r.json()
        except Exception:
            return {"error": "invalid json", "status_code": r.status_code}
    else:
        return {"error": r.text, "status_code": r.status_code}


def save_json(path: str, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)


def write_csvs_from_combined(target_dir: str, combined: dict):
    import pandas as pd

    csv_dir = os.path.join(target_dir, "csvs")
    os.makedirs(csv_dir, exist_ok=True)

    for endpoint, rows in combined.items():
        if not rows:
            continue
        # rows may be a list of dicts or contain error objects - filter
        records = [r for r in rows if isinstance(r, dict)]
        if not records:
            continue
        try:
            df = pd.DataFrame.from_records(records)
            out = os.path.join(csv_dir, f"{endpoint}.csv")
            df.to_csv(out, index=False)
            print(f"Wrote combined CSV: {out}")
        except Exception as e:
            print(f"Failed to write CSV for {endpoint}: {e}")


def main():
    p = argparse.ArgumentParser(description="POC: collect FAC API data for audits")
    p.add_argument("--ids", help="Comma-separated report ids to fetch")
    p.add_argument("--from-csv", help="CSV path to read report ids from")
    p.add_argument("--id-column", default="report_id", help="Column name in CSV containing the report id")
    p.add_argument("--count", type=int, default=5, help="Number of ids to read from CSV when using --from-csv")
    p.add_argument("--out", default="poc_results", help="Output folder")
    p.add_argument("--endpoints", help="Comma-separated endpoints to query (overrides defaults)")
    args = p.parse_args()

    if args.endpoints:
        endpoints = [e.strip() for e in args.endpoints.split(",") if e.strip()]
    else:
        endpoints = ENDPOINTS

    if args.ids:
        ids = [s.strip() for s in args.ids.split(",") if s.strip()]
    elif args.from_csv:
        ids = read_ids_from_csv(args.from_csv, args.id_column, args.count)
    else:
        raise SystemExit("Provide --ids or --from-csv to select audits")

    os.makedirs(args.out, exist_ok=True)

    headers = {"X-Api-Key": API_KEY} if API_KEY else {}

    session = requests.Session()
    session.headers.update(headers)

    combined = defaultdict(list)

    for report_id in ids:
        print(f"\nFetching data for report_id={report_id} ...")
        audit_data = {}
        for endpoint in endpoints:
            print(f"  -> trying endpoint: {endpoint}")
            data = fetch_endpoint_for_id(session, endpoint, report_id)
            audit_data[endpoint] = data
            # if successful list of dicts, extend combined
            if isinstance(data, list):
                combined[endpoint].extend(data)
        # save per-audit JSON
        out_json = os.path.join(args.out, f"{report_id}.json")
        save_json(out_json, audit_data)
        print(f"Saved: {out_json}")

    # write combined CSVs for endpoints with any rows
    write_csvs_from_combined(args.out, combined)


if __name__ == "__main__":
    main()
