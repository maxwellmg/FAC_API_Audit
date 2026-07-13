#!/usr/bin/env python3
"""
Split each sheet in an Excel workbook to separate CSV files in the same directory.

Usage:
  python scripts/split_excel_sheets_to_csv.py "FAC API Data Dictionary.xlsx"

Requirements:
  pip install pandas openpyxl

The script will create files named like:
  FAC API Data Dictionary - SheetName.csv
"""
import sys
import os

def main():
    try:
        import pandas as pd
    except ImportError:
        print("Please install required packages: pip install pandas openpyxl")
        sys.exit(2)

    if len(sys.argv) < 2:
        print("Usage: python split_excel_sheets_to_csv.py <excel_file.xlsx>")
        sys.exit(1)

    excel_path = sys.argv[1]
    if not os.path.isfile(excel_path):
        print(f"File not found: {excel_path}")
        sys.exit(1)

    try:
        xls = pd.ExcelFile(excel_path)
    except Exception as e:
        print(f"Error reading Excel file: {e}")
        sys.exit(1)

    dirpath = os.path.dirname(os.path.abspath(excel_path))
    base = os.path.splitext(os.path.basename(excel_path))[0]

    # create target folder named like "<base> CSVs" (e.g. "FAC API Data Dictionary CSVs")
    folder_name = f"{base} CSVs"
    target_dir = os.path.join(dirpath, folder_name)
    os.makedirs(target_dir, exist_ok=True)

    for sheet in xls.sheet_names:
        try:
            df = xls.parse(sheet)
        except Exception as e:
            print(f"Skipping sheet '{sheet}' due to parse error: {e}")
            continue

        safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in sheet).strip()

        # original style: "<base> - <sheet>". We want to remove everything up to
        # and including the first " - ", so resulting filename is just the sheet part.
        name_part = f"{base} - {safe}"
        if " - " in name_part:
            outname = name_part.split(" - ", 1)[1] + ".csv"
        else:
            outname = name_part + ".csv"

        outpath = os.path.join(target_dir, outname)
        try:
            df.to_csv(outpath, index=False)
            print(f"Wrote {outpath}")
        except Exception as e:
            print(f"Failed writing {outpath}: {e}")

        # remove any old CSV created in the workbook directory with the original naming
        oldpath = os.path.join(dirpath, f"{base} - {safe}.csv")
        try:
            if os.path.exists(oldpath) and os.path.abspath(oldpath) != os.path.abspath(outpath):
                os.remove(oldpath)
                print(f"Removed old file {oldpath}")
        except Exception:
            pass

if __name__ == "__main__":
    main()
