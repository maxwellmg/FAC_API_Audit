# %% [markdown]
# # FAC Findings x USAspending (2026): filter, stack, and EDA
# Set `BASE_DIR` (and `SPEND_KEY` if needed) in the first cell, then run all cells.

# %% 0. Setup
import gc, re
from pathlib import Path
import pandas as pd
from IPython.display import display

BASE_DIR = Path(r"/path/to/base_dir")                    # <-- set this
FAC_FILE = BASE_DIR / "fac_findings.xlsx"                 # adjust name/extension if different
FAC_COLS = ["School Code", "School Name", "Campus Name", "Campus Code", "UNITID", "EIN", "UEIS"]
FAC_KEY, SPEND_KEY = "UEIS", "recipient_uei"              # match FAC UEIS to USAspending recipient_uei
CONVENTIONS = ["All_Assistance_Delta", "All_Contracts_Delta", "All_Assistance_Full", "All_Contracts_Full"]
CHUNK_ROWS = 250_000                                      # rows held in memory at once while filtering
DATA_DIR = BASE_DIR / "USA_Spending_Data" / "2026"
OUT_DIR = DATA_DIR / "Merged_Data"
OUT_DIR.mkdir(exist_ok=True)
pd.set_option("display.max_rows", 500, "display.max_columns", 60, "display.float_format", "{:,.2f}".format)

def norm_id(s):
    """IDs -> clean strings: '12-3456789', '123456789.0', 23456789 -> '123456789' / '023456789'."""
    s = s.astype("string").str.strip().str.upper().str.replace(r"\.0$", "", regex=True).str.replace(r"[^0-9A-Z]", "", regex=True)
    s = s.mask(s.str.fullmatch(r"\d{1,8}").fillna(False), s.str.zfill(9))  # restore leading zeros lost as numbers
    return s.replace("", pd.NA)

def section(title): print(f"\n{'=' * 100}\n{title}\n{'=' * 100}")

# %% 1. FAC Findings: the six ID columns, one row per school/campus
fac = pd.read_excel(FAC_FILE, sheet_name="FAC_Findings", usecols=FAC_COLS, dtype=str)
fac["EIN"] = norm_id(fac["EIN"])                          # carried along as a clean 9-digit string
fac["_key"] = fac[FAC_KEY].astype("string").str.split(r"[,;|/\s]+")  # a cell may list several UEIs
fac = fac.explode("_key", ignore_index=True).assign(_key=lambda d: norm_id(d["_key"]))  # one row per UEI
fac[FAC_KEY] = fac["_key"]                                # UEIS now holds the single, cleaned UEI that matches
fac = fac.dropna(subset=["_key"]).drop_duplicates()       # findings sheets repeat a school once per finding
shared = fac["_key"].duplicated().sum()
print(f"FAC: {len(fac):,} rows | {fac['_key'].nunique():,} unique UEIs | {fac['School Code'].nunique():,} school codes")
if shared: print(f"Note: {shared} extra rows share a UEI with another row; matching spending rows repeat for each (de-duplicated in EDA).")

# %% 2-3. Filter each USAspending CSV in chunks -> temp file -> stack temps per naming convention
def file_order(p):  # base name, then trailing _# as an integer, so _10 comes after _9
    m = re.match(r"(.*)_(\d+)\.csv$", p.name, re.I)
    return (m.group(1), int(m.group(2))) if m else (p.stem, 0)

header = pd.read_csv(next(DATA_DIR.glob("*.csv")), nrows=0).columns
if SPEND_KEY not in header:
    ids = [c for c in header if re.search(r"(^|_)(ein|tin|uei|duns)(_|$)", c, re.I)]
    raise KeyError(f"'{SPEND_KEY}' is not a USAspending column. Recipient ID columns available: {ids}")

for conv in CONVENTIONS:
    files = sorted(DATA_DIR.glob(f"*{conv}*.csv"), key=file_order)
    print(f"\n{conv}: {len(files)} file(s)")
    temps = []
    for f in files:
        kept = []
        for chunk in pd.read_csv(f, chunksize=CHUNK_ROWS, dtype=str, encoding_errors="replace"):
            chunk["_key"] = norm_id(chunk[SPEND_KEY])
            hit = chunk[chunk["_key"].isin(fac["_key"])]                 # keep only FAC EINs, original row order
            hit = hit.merge(fac, on="_key", how="left", suffixes=("_usaspending", ""))
            if len(hit): kept.append(hit)
        print(f"  {f.name}: kept {sum(map(len, kept)):,} rows")
        if kept:
            out = pd.concat(kept, ignore_index=True).drop(columns="_key")
            out = out[FAC_COLS + [c for c in out.columns if c not in FAC_COLS]]  # FAC columns first
            temps.append(OUT_DIR / f"_tmp_{f.stem}.csv")
            out.to_csv(temps[-1], index=False)
        kept = chunk = hit = out = None; gc.collect()             # release the large file before the next one
    if temps:
        stacked = pd.concat([pd.read_csv(t, dtype=str) for t in temps], ignore_index=True)
        stacked.to_csv(OUT_DIR / f"{conv}_merged.csv", index=False)
        for t in temps: t.unlink()
        print(f"  -> {conv}_merged.csv: {len(stacked):,} rows"); stacked = None; gc.collect()

# %% 4. Exploratory data analysis, one block of sections per merged dataset
OBL = "federal_action_obligation"   # transaction-level dollars: the column that is safe to sum
MONEY, NOT_MONEY = re.compile(r"obligat|amount|value|outlay|cost", re.I), re.compile(r"code|name|desc|_id|key|date|ind$", re.I)
BREAKDOWNS = ["correction_delete_ind", "action_date_fiscal_year", "awarding_agency_name", "awarding_sub_agency_name",
              "funding_agency_name", "assistance_type_description", "assistance_listing_title", "cfda_title", "award_type",
              "type_of_contract_pricing", "naics_description", "product_or_service_code_description",
              "primary_place_of_performance_state_name", "recipient_state_name", "business_types_description"]

for f in sorted(OUT_DIR.glob("*_merged.csv")):
    df, name = pd.read_csv(f, dtype=str), f.stem.replace("_merged", "")
    section(f"{name} | A. Field profile: every USAspending field ({len(df):,} rows x {df.shape[1]} columns)")
    display(df.describe().T.assign(pct_missing=df.isna().mean().mul(100)))
    money = [c for c in df.columns if MONEY.search(c) and not NOT_MONEY.search(c)]
    df[money] = df[money].apply(pd.to_numeric, errors="coerce")
    txn = next((c for c in df.columns if c.endswith("transaction_unique_key")), None)
    award = next((c for c in df.columns if c.endswith("award_unique_key")), None)
    by_school = df.drop_duplicates(["School Code", txn]) if txn else df   # campuses sharing an EIN
    uniq = df.drop_duplicates(txn) if txn else df                         # each transaction once

    section(f"{name} | B. Match check: FAC school vs. USAspending recipient")
    unmatched = sorted(set(fac["School Code"].dropna()) - set(df["School Code"].dropna()))
    print(f"School codes matched: {df['School Code'].nunique():,} of {fac['School Code'].nunique():,} | unmatched: {unmatched[:30]}")
    if "recipient_name" in df: display(df.groupby(["School Name", "recipient_name"]).size().rename("rows").head(40))

    section(f"{name} | C. Dollar fields (unique transactions; award-level 'total_*' fields repeat per transaction, don't sum them)")
    display(uniq[money].describe().T.assign(sum=uniq[money].sum(), pct_missing=uniq[money].isna().mean().mul(100)))

    section(f"{name} | D. Summary statistics by School Code ({OBL})")
    g = by_school.groupby("School Code")
    summ = g[OBL].agg(["count", "sum", "mean", "median", "min", "max"]).rename(columns={"count": "transactions", "sum": "total"})
    summ.insert(0, "School Name", g["School Name"].first())
    if award: summ["unique_awards"] = g[award].nunique()
    if "awarding_agency_name" in df: summ["n_agencies"] = g["awarding_agency_name"].nunique()
    if "action_date" in df: summ["first_action"], summ["last_action"] = g["action_date"].min(), g["action_date"].max()
    display(summ.sort_values("total", ascending=False))
    print("Distribution across schools:"); display(summ.describe())

    section(f"{name} | E. Breakdowns by USAspending category (top 15 by {OBL})")
    for c in [c for c in BREAKDOWNS if c in df]:
        t = uniq.groupby(c, dropna=False)[OBL].agg(transactions="count", total="sum").sort_values("total", ascending=False)
        t["pct_of_total"] = t["total"] / t["total"].sum() * 100
        print(f"\n--- {c} ({len(t):,} distinct values) ---"); display(t.head(15))

    if "awarding_agency_name" in df:
        section(f"{name} | F. School Code x top 8 awarding agencies ({OBL})")
        top = uniq.groupby("awarding_agency_name")[OBL].sum().nlargest(8).index
        display(by_school[by_school["awarding_agency_name"].isin(top)].pivot_table(
            index="School Code", columns="awarding_agency_name", values=OBL, aggfunc="sum", fill_value=0))
