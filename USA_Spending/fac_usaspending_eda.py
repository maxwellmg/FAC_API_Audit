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

# %% 1. FAC Findings: one row per award -> collapse to one lookup row per (School Code, UEI) before merging
raw = pd.read_excel(FAC_FILE, sheet_name="FAC_Findings", usecols=FAC_COLS, dtype=str)
fac_rows = raw.groupby("School Code").size()              # FAC award records per school (kept for the EDA)
raw["EIN"] = norm_id(raw["EIN"])                          # carried along as a clean 9-digit string
raw["_key"] = raw[FAC_KEY].astype("string").str.split(r"[,;|/\s]+")  # a cell may list several UEIs
raw = raw.explode("_key", ignore_index=True).assign(_key=lambda d: norm_id(d["_key"])).dropna(subset=["_key"])
raw[FAC_KEY] = raw["_key"]                                # UEIS now holds the single, cleaned UEI that matches
join = lambda s: "; ".join(sorted(s.dropna().unique()))  # keep every distinct campus / UNITID / EIN value
fac = raw.groupby(["School Code", "_key"], as_index=False).agg({c: join for c in FAC_COLS if c != "School Code"})
shared = fac.loc[fac["_key"].duplicated(), "_key"].nunique()
print(f"FAC: {len(raw):,} award rows -> {len(fac):,} school/UEI rows | {fac['School Code'].nunique():,} school codes")
if shared: print(f"Note: {shared} UEI(s) belong to more than one School Code; their spending is listed under each school.")

# %% 2-3. Filter each USAspending CSV in chunks -> temp file -> stack temps per naming convention
def file_order(p):  # base name, then trailing _# as an integer, so _10 comes after _9
    m = re.match(r"(.*)_(\d+)\.csv$", p.name, re.I)
    return (m.group(1), int(m.group(2))) if m else (p.stem, 0)

for conv in CONVENTIONS:
    files = sorted(DATA_DIR.glob(f"*{conv}*.csv"), key=file_order)
    print(f"\n{conv}: {len(files)} file(s)")
    temps = []
    for f in files:
        kept = []
        for chunk in pd.read_csv(f, chunksize=CHUNK_ROWS, dtype=str, encoding_errors="replace"):
            chunk["_key"] = norm_id(chunk[SPEND_KEY])
            hit = chunk[chunk["_key"].isin(fac["_key"])]                 # keep only FAC UEIs, original row order
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
    txn = next(c for c in df.columns if c.endswith("transaction_unique_key"))
    award = next(c for c in df.columns if c.endswith("award_unique_key"))
    uniq = df.drop_duplicates(txn)        # each transaction once, even if its UEI belongs to two school codes
    awards = df.groupby(["School Code", award], as_index=False).agg(obligation=(OBL, "sum"), transactions=(OBL, "size"),
        agency=("awarding_agency_name", "first"), first_action=("action_date", "min"), last_action=("action_date", "max"))

    section(f"{name} | B. Match check: FAC school vs. USAspending recipient")
    unmatched = sorted(set(fac["School Code"].dropna()) - set(df["School Code"].dropna()))
    print(f"School codes matched: {df['School Code'].nunique():,} of {fac['School Code'].nunique():,} | unmatched: {unmatched[:30]}")
    if "recipient_name" in df: display(df.groupby(["School Name", "recipient_name"]).size().rename("rows").head(40))

    section(f"{name} | C. Dollar fields (unique transactions; award-level 'total_*' fields repeat per transaction, don't sum them)")
    display(uniq[money].describe().T.assign(sum=uniq[money].sum(), pct_missing=uniq[money].isna().mean().mul(100)))
    print(f"Award size ({OBL} summed per award):"); display(awards.drop_duplicates(award)["obligation"].describe())

    section(f"{name} | D. Summary statistics by School Code (transactions rolled up to one row per award first)")
    g = awards.groupby("School Code")
    summ = g["obligation"].agg(awards="count", total="sum", mean_per_award="mean", median_per_award="median", largest_award="max")
    summ.insert(0, "School Name", df.groupby("School Code")["School Name"].first())
    summ = summ.assign(transactions=g["transactions"].sum(), fac_award_rows=fac_rows, n_agencies=g["agency"].nunique(),
        largest_award_pct=lambda d: d["largest_award"] / d["total"] * 100, first_action=g["first_action"].min(), last_action=g["last_action"].max())
    display(summ.sort_values("total", ascending=False))
    print("Distribution across schools:"); display(summ.describe())
    print("Largest awards:"); display(awards.nlargest(15, "obligation"))

    section(f"{name} | E. Breakdowns by USAspending category (top 15 by {OBL})")
    for c in [c for c in BREAKDOWNS if c in df]:
        t = df.groupby(c, dropna=False).agg(schools=("School Code", "nunique"), awards=(award, "nunique"), transactions=(txn, "nunique"))
        t["total"] = uniq.groupby(c, dropna=False)[OBL].sum()
        t["pct_of_total"] = t["total"] / t["total"].sum() * 100
        print(f"\n--- {c} ({len(t):,} distinct values) ---"); display(t.sort_values("total", ascending=False).head(15))

    section(f"{name} | F. School Code x top 8 awarding agencies ({OBL})")
    top = uniq.groupby("awarding_agency_name")[OBL].sum().nlargest(8).index
    display(df[df["awarding_agency_name"].isin(top)].pivot_table(
        index="School Code", columns="awarding_agency_name", values=OBL, aggfunc="sum", fill_value=0))
