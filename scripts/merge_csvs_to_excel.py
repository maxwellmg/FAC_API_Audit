#!/usr/bin/env python3
"""
Merge all CSV files in a directory into a single Excel workbook with one sheet per CSV.

Usage:
  python scripts/merge_csvs_to_excel.py --csv-dir poc_results_10/csvs --out poc_results_10/combined.xlsx

Requirements:
  pip install pandas openpyxl
"""
import argparse
import os
import pandas as pd


def main():
    p = argparse.ArgumentParser(description="Merge CSVs into Excel workbook")
    p.add_argument("--csv-dir", required=True, help="Directory containing CSV files")
    p.add_argument("--out", required=True, help="Output Excel file path")
    args = p.parse_args()

    csv_dir = args.csv_dir
    out_path = args.out

    if not os.path.isdir(csv_dir):
        raise SystemExit(f"CSV directory not found: {csv_dir}")

    csv_files = [f for f in os.listdir(csv_dir) if f.lower().endswith('.csv')]
    if not csv_files:
        raise SystemExit(f"No CSV files found in {csv_dir}")

    # create parent dir for out if missing
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)

    with pd.ExcelWriter(out_path, engine='openpyxl') as writer:
        for fname in sorted(csv_files):
            path = os.path.join(csv_dir, fname)
            # use filename (without ext) as sheet name, truncated to 31 chars
            sheet = os.path.splitext(fname)[0][:31]
            try:
                df = pd.read_csv(path)
                df.to_excel(writer, sheet_name=sheet, index=False)
                print(f"Added sheet: {sheet}")
            except Exception as e:
                print(f"Failed to add {fname}: {e}")

    print(f"Wrote Excel workbook: {out_path}")


if __name__ == '__main__':
    main()
