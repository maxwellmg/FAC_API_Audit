#!/usr/bin/env python3
"""
FAC (Federal Audit Clearinghouse) EDA / ML-preprocessing pipeline.

Pulls Single Audit data for a chosen entity type (default: institutions of
higher education, entity_type=higher-ed) from https://api.fac.gov, then
reshapes it from FAC's normalized, multi-row-per-audit endpoints into two
flat, ML-ready tables:

  1. A panel table   - one row per (auditee, audit_year) audit report.
  2. A school table  - one row per auditee (auditee_uei), aggregating every
                        available audit year. This is the grain that lines
                        up with a "one row per school" master table.

See README_FAC_EDA.md (one directory up) for the full set of decisions this
script encodes: field selection, aggregation logic, and known limitations
(e.g. FAC has no UNITID/OPEID, so joining to a College Scorecard-style school
table requires a downstream name/state fuzzy match).

Usage:
  # Step 1: pull raw data from the API
  python scripts/fac_eda_pipeline.py pull --entity-type higher-ed --out-dir data/raw

  # Step 2: build the panel + school-level tables and an EDA summary
  python scripts/fac_eda_pipeline.py process --raw-dir data/raw --out-dir data/processed

  # Both steps back-to-back
  python scripts/fac_eda_pipeline.py all --out-dir data

Requirements:
  pip install requests pandas numpy
"""
import argparse
import os
import time
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
import requests

BASE_URL = "https://api.fac.gov"

# Child endpoints keyed by report_id, and the raw filename each is pulled to.
# (general is handled separately since it's the driver query.)
CHILD_ENDPOINTS = [
    "federal_awards",
    "findings",
    "findings_text",
    "corrective_action_plans",
    "passthrough",
    "notes_to_sefa",
    "secondary_auditors",
    "additional_eins",
    "additional_ueis",
]

# Server-observed hard cap on rows per request (confirmed via Content-Range on
# api.fac.gov: requesting limit=100000 still returns at most 20000 rows).
SERVER_PAGE_CAP = 20000


# --------------------------------------------------------------------------
# Shared HTTP helpers
# --------------------------------------------------------------------------

def get_api_key() -> Optional[str]:
    key = os.environ.get("FAC_API_KEY")
    if key:
        return key
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    key_path = os.path.join(repo_root, ".fac_api_key")
    if os.path.exists(key_path):
        with open(key_path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    return None


def make_session() -> requests.Session:
    key = get_api_key()
    if not key:
        raise SystemExit(
            "No FAC API key found. Set FAC_API_KEY env var or create .fac_api_key in the repo root."
        )
    session = requests.Session()
    session.headers.update({"X-Api-Key": key})
    return session


def paginated_get(session: requests.Session, endpoint: str, filters: Dict[str, str],
                   select: Optional[List[str]] = None, page_size: int = 10000,
                   max_rows: Optional[int] = None, max_retries: int = 5) -> List[dict]:
    """Fetch all rows matching `filters` from a PostgREST endpoint, paging via
    limit/offset and Content-Range, with retry/backoff on transient errors."""
    params = dict(filters)
    if select:
        params["select"] = ",".join(select)
    page_size = min(page_size, SERVER_PAGE_CAP)

    rows: List[dict] = []
    offset = 0
    while True:
        params["limit"] = page_size
        params["offset"] = offset
        url = f"{BASE_URL}/{endpoint}"

        attempt = 0
        while True:
            try:
                resp = session.get(url, params=params, timeout=60)
            except requests.RequestException as exc:
                attempt += 1
                if attempt > max_retries:
                    raise
                time.sleep(min(2 ** attempt, 30))
                continue

            if resp.status_code in (200, 206):
                break
            if resp.status_code in (429, 500, 502, 503, 504):
                attempt += 1
                if attempt > max_retries:
                    resp.raise_for_status()
                time.sleep(min(2 ** attempt, 30))
                continue
            resp.raise_for_status()

        page = resp.json()
        rows.extend(page)
        if max_rows and len(rows) >= max_rows:
            return rows[:max_rows]
        if len(page) < page_size:
            break
        offset += page_size
    return rows


def batched(seq: List[str], size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


# --------------------------------------------------------------------------
# PULL
# --------------------------------------------------------------------------

def pull(args: argparse.Namespace) -> None:
    os.makedirs(args.out_dir, exist_ok=True)
    session = make_session()

    general_path = os.path.join(args.out_dir, "general.csv")
    if os.path.exists(general_path) and not args.refresh:
        print(f"[pull] {general_path} already exists, reusing (use --refresh to re-pull).")
        general_df = pd.read_csv(general_path, dtype=str)
    else:
        filters = {"entity_type": f"eq.{args.entity_type}"}
        if args.audit_year_min:
            filters["audit_year"] = f"gte.{args.audit_year_min}"
        if args.audit_year_max:
            # PostgREST needs a second audit_year key; requests can't send two
            # identical keys via a dict, so combine via `and=`.
            filters.pop("audit_year", None)
            conds = []
            if args.audit_year_min:
                conds.append(f"audit_year.gte.{args.audit_year_min}")
            conds.append(f"audit_year.lte.{args.audit_year_max}")
            filters["and"] = f"({','.join(conds)})"

        print(f"[pull] Fetching /general where entity_type=eq.{args.entity_type} ...")
        rows = paginated_get(session, "general", filters, page_size=args.page_size,
                              max_rows=args.max_reports)
        general_df = pd.DataFrame(rows)
        general_df.to_csv(general_path, index=False)
        print(f"[pull] Wrote {general_path} ({len(general_df)} rows, "
              f"{general_df['auditee_uei'].nunique() if len(general_df) else 0} distinct auditees).")

    if general_df.empty:
        print("[pull] No general rows found for the given filters; nothing more to pull.")
        return

    report_ids = general_df["report_id"].dropna().astype(str).unique().tolist()
    print(f"[pull] {len(report_ids)} report_ids to fetch child-endpoint data for.")

    for endpoint in CHILD_ENDPOINTS:
        out_path = os.path.join(args.out_dir, f"{endpoint}.csv")
        if os.path.exists(out_path) and not args.refresh:
            print(f"[pull] {out_path} already exists, skipping (use --refresh to re-pull).")
            continue

        print(f"[pull] Fetching /{endpoint} for {len(report_ids)} report_ids "
              f"in batches of {args.batch_size} ...")
        all_rows: List[dict] = []
        batches = list(batched(report_ids, args.batch_size))
        for i, batch in enumerate(batches, 1):
            id_list = ",".join(batch)
            filters = {"report_id": f"in.({id_list})"}
            rows = paginated_get(session, endpoint, filters, page_size=args.page_size)
            all_rows.extend(rows)
            if i % 10 == 0 or i == len(batches):
                print(f"    ... batch {i}/{len(batches)}, {len(all_rows)} rows so far")
            if args.sleep:
                time.sleep(args.sleep)

        df = pd.DataFrame(all_rows)
        df.to_csv(out_path, index=False)
        print(f"[pull] Wrote {out_path} ({len(df)} rows).")

    print("[pull] Done.")


# --------------------------------------------------------------------------
# PROCESS  (feature engineering)
# --------------------------------------------------------------------------

YN_COLS_GENERAL = [
    "is_sp_framework_required", "is_going_concern_included",
    "is_internal_control_deficiency_disclosed",
    "is_internal_control_material_weakness_disclosed",
    "is_material_noncompliance_disclosed", "is_low_risk_auditee",
    "agencies_with_prior_findings", "is_aicpa_audit_guide_included",
    "is_additional_ueis", "is_multiple_eins", "is_secondary_auditors",
]

DATE_COLS_GENERAL = [
    "date_created", "ready_for_certification_date", "auditor_certified_date",
    "auditee_certified_date", "submitted_date", "fac_accepted_date",
    "fy_end_date", "fy_start_date",
]

NUMERIC_COLS_GENERAL = ["dollar_threshold", "total_amount_expended", "number_months"]


def yn_to_bool(series: pd.Series) -> pd.Series:
    return series.map({"Y": True, "N": False})


def read_raw(raw_dir: str, name: str) -> pd.DataFrame:
    path = os.path.join(raw_dir, f"{name}.csv")
    if not os.path.exists(path):
        print(f"[process] WARNING: {path} not found, treating {name} as empty.")
        return pd.DataFrame()
    try:
        return pd.read_csv(path, dtype=str)
    except pd.errors.EmptyDataError:
        # A report set with zero matching rows for this endpoint is written
        # as a header-less empty file by `pull`.
        return pd.DataFrame()


def load_general(raw_dir: str) -> pd.DataFrame:
    df = read_raw(raw_dir, "general")
    if df.empty:
        return df

    for col in YN_COLS_GENERAL:
        if col in df.columns:
            df[col] = yn_to_bool(df[col])
    for col in DATE_COLS_GENERAL:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")
    for col in NUMERIC_COLS_GENERAL:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "audit_year" in df.columns:
        df["audit_year"] = pd.to_numeric(df["audit_year"], errors="coerce").astype("Int64")
    if "resubmission_version" in df.columns:
        df["resubmission_version"] = pd.to_numeric(df["resubmission_version"], errors="coerce")
        df["was_resubmitted"] = df["resubmission_version"].fillna(0) > 0

    # FAC's pre-~2022 records, migrated from the legacy Census Bureau system,
    # carry the literal placeholder "GSA_MIGRATION" instead of a real UEI
    # (confirmed live: true for ~59% of higher-ed rows, concentrated in
    # audit_year <= 2021). auditee_ein is populated for 100% of rows across
    # the whole date range, so it - not auditee_uei - is used as the
    # school-level grouping key. See README_FAC_EDA.md.
    if "auditee_uei" in df.columns:
        df["auditee_uei_is_placeholder"] = df["auditee_uei"] == "GSA_MIGRATION"
        df["auditee_uei"] = df["auditee_uei"].where(~df["auditee_uei_is_placeholder"])

    # Filing timeliness: Uniform Guidance (2 CFR 200.512) sets the audit
    # submission deadline at the earlier of 30 days after receiving the
    # auditor's report or 9 months (~273 days) after the fiscal year end.
    # We approximate against the 9-month statutory ceiling since we don't
    # have the auditor's-report-received date.
    df["days_fyend_to_submitted"] = (df["submitted_date"] - df["fy_end_date"]).dt.days
    df["days_fyend_to_accepted"] = (df["fac_accepted_date"] - df["fy_end_date"]).dt.days
    df["is_late_submission"] = df["days_fyend_to_submitted"] > 273

    return df


def agg_federal_awards(raw_dir: str) -> pd.DataFrame:
    df = read_raw(raw_dir, "federal_awards")
    if df.empty:
        return pd.DataFrame(columns=["report_id"])

    for col in ["amount_expended", "cluster_total", "federal_program_total",
                "loan_balance", "findings_count", "passthrough_amount"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in ["is_major", "is_loan", "is_direct", "is_passthrough_award"]:
        if col in df.columns:
            df[col] = yn_to_bool(df[col])

    g = df.groupby("report_id")
    out = pd.DataFrame({
        "fa_n_awards": g.size(),
        "fa_total_amount_expended": g["amount_expended"].sum(min_count=1),
        "fa_n_clusters": g["cluster_name"].nunique(),
        "fa_n_agencies": g["federal_agency_prefix"].nunique(),
        "fa_n_major_programs": g["is_major"].sum(min_count=1),
        "fa_n_loan_programs": g["is_loan"].sum(min_count=1),
        "fa_total_loan_balance": g["loan_balance"].sum(min_count=1),
        "fa_n_direct_awards": g["is_direct"].sum(min_count=1),
        "fa_n_passthrough_awards": g["is_passthrough_award"].sum(min_count=1),
        "fa_total_passthrough_amount": g["passthrough_amount"].sum(min_count=1),
        "fa_sum_findings_count": g["findings_count"].sum(min_count=1),
    })
    out["fa_pct_major_programs"] = out["fa_n_major_programs"] / out["fa_n_awards"]
    out["fa_pct_direct_awards"] = out["fa_n_direct_awards"] / out["fa_n_awards"]
    out["fa_pct_passthrough_awards"] = out["fa_n_passthrough_awards"] / out["fa_n_awards"]

    if "audit_report_type" in df.columns:
        type_counts = df.pivot_table(index="report_id", columns="audit_report_type",
                                      aggfunc="size", fill_value=0)
        type_counts = type_counts.rename(columns={
            "U": "fa_n_unqualified", "Q": "fa_n_qualified",
            "A": "fa_n_adverse", "D": "fa_n_disclaimer",
        })
        out = out.join(type_counts, how="left")

    return out.reset_index()


def agg_findings(raw_dir: str) -> pd.DataFrame:
    df = read_raw(raw_dir, "findings")
    if df.empty:
        return pd.DataFrame(columns=["report_id"])

    bool_cols = ["is_material_weakness", "is_modified_opinion", "is_other_findings",
                 "is_other_matters", "is_questioned_costs", "is_repeat_finding",
                 "is_significant_deficiency"]
    for col in bool_cols:
        if col in df.columns:
            df[col] = yn_to_bool(df[col])

    # A finding can be listed against more than one award_reference; de-dupe
    # to one row per (report_id, reference_number) so counts reflect distinct
    # findings rather than finding x award combinations.
    dedup = df.drop_duplicates(subset=["report_id", "reference_number"])

    g = dedup.groupby("report_id")
    out = pd.DataFrame({
        "fnd_n_findings": g.size(),
        "fnd_n_material_weakness": g["is_material_weakness"].sum(min_count=1),
        "fnd_n_significant_deficiency": g["is_significant_deficiency"].sum(min_count=1),
        "fnd_n_modified_opinion": g["is_modified_opinion"].sum(min_count=1),
        "fnd_n_questioned_costs": g["is_questioned_costs"].sum(min_count=1),
        "fnd_n_repeat_findings": g["is_repeat_finding"].sum(min_count=1),
        "fnd_n_other_matters": g["is_other_matters"].sum(min_count=1),
        "fnd_n_other_findings": g["is_other_findings"].sum(min_count=1),
    })
    if "type_requirement" in dedup.columns:
        out["fnd_n_distinct_type_requirements"] = dedup.groupby("report_id")["type_requirement"].nunique()
    out["fnd_has_findings"] = out["fnd_n_findings"] > 0
    return out.reset_index()


def agg_findings_text(raw_dir: str) -> pd.DataFrame:
    df = read_raw(raw_dir, "findings_text")
    if df.empty:
        return pd.DataFrame(columns=["report_id"])
    if "contains_chart_or_table" in df.columns:
        df["contains_chart_or_table"] = yn_to_bool(df["contains_chart_or_table"])
    df["_text_len"] = df.get("finding_text", pd.Series(dtype=str)).fillna("").str.len()
    g = df.groupby("report_id")
    out = pd.DataFrame({
        "fndtxt_n_with_text": g.size(),
        "fndtxt_avg_text_length": g["_text_len"].mean(),
    })
    if "contains_chart_or_table" in df.columns:
        out["fndtxt_n_with_chart_or_table"] = g["contains_chart_or_table"].sum(min_count=1)
    return out.reset_index()


def agg_corrective_action_plans(raw_dir: str) -> pd.DataFrame:
    df = read_raw(raw_dir, "corrective_action_plans")
    if df.empty:
        return pd.DataFrame(columns=["report_id"])
    g = df.groupby("report_id")
    out = pd.DataFrame({"cap_n_plans": g.size()})
    return out.reset_index()


def agg_passthrough(raw_dir: str) -> pd.DataFrame:
    df = read_raw(raw_dir, "passthrough")
    if df.empty:
        return pd.DataFrame(columns=["report_id"])
    g = df.groupby("report_id")
    out = pd.DataFrame({
        "pt_n_relationships": g.size(),
        "pt_n_distinct_entities": g["passthrough_name"].nunique(),
    })
    return out.reset_index()


def agg_notes_to_sefa(raw_dir: str) -> pd.DataFrame:
    df = read_raw(raw_dir, "notes_to_sefa")
    if df.empty:
        return pd.DataFrame(columns=["report_id"])
    if "is_minimis_rate_used" in df.columns:
        df["is_minimis_rate_used"] = yn_to_bool(df["is_minimis_rate_used"])
    if "contains_chart_or_table" in df.columns:
        df["contains_chart_or_table"] = yn_to_bool(df["contains_chart_or_table"])
    g = df.groupby("report_id")
    out = pd.DataFrame({"sefa_n_notes": g.size()})
    if "is_minimis_rate_used" in df.columns:
        out["sefa_is_minimis_rate_used"] = g["is_minimis_rate_used"].max()
    if "contains_chart_or_table" in df.columns:
        out["sefa_n_with_chart_or_table"] = g["contains_chart_or_table"].sum(min_count=1)
    return out.reset_index()


def agg_secondary_auditors(raw_dir: str) -> pd.DataFrame:
    df = read_raw(raw_dir, "secondary_auditors")
    if df.empty:
        return pd.DataFrame(columns=["report_id"])
    g = df.groupby("report_id")
    out = pd.DataFrame({"sa_n_secondary_auditors": g.size()})
    return out.reset_index()


def agg_additional_ids(raw_dir: str) -> pd.DataFrame:
    ein_df = read_raw(raw_dir, "additional_eins")
    uei_df = read_raw(raw_dir, "additional_ueis")
    parts = []
    if not ein_df.empty:
        parts.append(ein_df.groupby("report_id").size().rename("org_n_additional_eins"))
    if not uei_df.empty:
        parts.append(uei_df.groupby("report_id").size().rename("org_n_additional_ueis"))
    if not parts:
        return pd.DataFrame(columns=["report_id"])
    out = pd.concat(parts, axis=1)
    return out.reset_index()


def build_panel(raw_dir: str) -> pd.DataFrame:
    general = load_general(raw_dir)
    if general.empty:
        raise SystemExit(f"No general.csv data found in {raw_dir}; run the `pull` step first.")

    panel = general
    for agg_fn in [agg_federal_awards, agg_findings, agg_findings_text,
                   agg_corrective_action_plans, agg_passthrough, agg_notes_to_sefa,
                   agg_secondary_auditors, agg_additional_ids]:
        panel = panel.merge(agg_fn(raw_dir), on="report_id", how="left")

    # Zero-fill count-style columns where an audit had no rows in that
    # endpoint at all (NaN there means "zero", not "unknown").
    count_prefixes = ("fa_n_", "fnd_n_", "fndtxt_n_", "cap_n_", "pt_n_",
                       "sefa_n_", "sa_n_", "org_n_")
    for col in panel.columns:
        if col.startswith(count_prefixes):
            panel[col] = panel[col].fillna(0)
    for col in ["fnd_has_findings", "sefa_is_minimis_rate_used"]:
        if col in panel.columns:
            panel[col] = panel[col].fillna(False)

    return panel


# auditee_ein (not auditee_uei) is the school-level grouping key: it is
# populated for 100% of higher-ed rows across all audit years, whereas
# auditee_uei is a "GSA_MIGRATION" placeholder for pre-2022 records. See the
# note in load_general() and README_FAC_EDA.md.
SCHOOL_ID_COL = "auditee_ein"

# Numeric feature columns worth carrying both a "latest" snapshot and
# historical aggregates for, at the school level.
HIST_NUMERIC_COLS = [
    "total_amount_expended", "dollar_threshold", "number_months",
    "days_fyend_to_submitted", "days_fyend_to_accepted",
    "fa_n_awards", "fa_total_amount_expended", "fa_n_clusters", "fa_n_agencies",
    "fa_n_major_programs", "fa_pct_major_programs", "fa_n_loan_programs",
    "fa_total_loan_balance", "fa_pct_direct_awards", "fa_pct_passthrough_awards",
    "fa_total_passthrough_amount", "fa_sum_findings_count",
    "fnd_n_findings", "fnd_n_material_weakness", "fnd_n_significant_deficiency",
    "fnd_n_modified_opinion", "fnd_n_questioned_costs", "fnd_n_repeat_findings",
    "fnd_n_distinct_type_requirements",
    "cap_n_plans", "pt_n_relationships", "pt_n_distinct_entities",
    "sefa_n_notes", "sa_n_secondary_auditors",
    "org_n_additional_eins", "org_n_additional_ueis",
]

HIST_BOOL_COLS = [
    "is_going_concern_included", "is_internal_control_deficiency_disclosed",
    "is_internal_control_material_weakness_disclosed",
    "is_material_noncompliance_disclosed", "is_low_risk_auditee",
    "agencies_with_prior_findings", "was_resubmitted", "is_late_submission",
    "fnd_has_findings", "sefa_is_minimis_rate_used",
]

LATEST_SNAPSHOT_COLS = [
    "audit_year", "entity_type", "auditee_name", "auditee_city", "auditee_state",
    "auditee_zip", "auditee_uei", "auditor_firm_name", "cognizant_agency",
    "oversight_agency", "audit_type", "gaap_results", "audit_period_covered",
] + HIST_NUMERIC_COLS + HIST_BOOL_COLS


def trend_slope(years: pd.Series, values: pd.Series) -> float:
    mask = years.notna() & values.notna()
    if mask.sum() < 2:
        return np.nan
    return float(np.polyfit(years[mask].astype(float), values[mask].astype(float), 1)[0])


def build_school_table(panel: pd.DataFrame) -> pd.DataFrame:
    panel = panel[panel[SCHOOL_ID_COL].notna()].copy()
    panel = panel.sort_values(["audit_year", "fac_accepted_date"])

    # auditor_firm_name is free text with inconsistent case/punctuation across
    # years (e.g. "PRICEWATERHOUSECOOPERS LLP" vs "PricewaterhouseCoopers LLP"
    # vs "... (PWC.COM)"), so a raw nunique() overstates auditor turnover.
    # This case/whitespace normalization is a heuristic, not entity
    # resolution - it will still overcount if a firm's suffix varies.
    if "auditor_firm_name" in panel.columns:
        panel["auditor_firm_name"] = panel["auditor_firm_name"].str.strip().str.upper()

    latest_snapshot_cols = [c for c in LATEST_SNAPSHOT_COLS if c in panel.columns]
    latest_idx = panel.groupby(SCHOOL_ID_COL)["audit_year"].idxmax()
    latest = panel.loc[latest_idx, [SCHOOL_ID_COL] + latest_snapshot_cols].copy()
    latest = latest.rename(columns={c: f"latest_{c}" for c in latest_snapshot_cols})

    max_year_overall = panel["audit_year"].max()
    coverage = panel.groupby(SCHOOL_ID_COL).agg(
        n_audit_years=("audit_year", "nunique"),
        first_audit_year=("audit_year", "min"),
        last_audit_year=("audit_year", "max"),
        all_names=("auditee_name", lambda s: "; ".join(sorted(set(s.dropna())))),
        all_ueis=("auditee_uei", lambda s: "; ".join(sorted(set(s.dropna())))),
        has_valid_uei=("auditee_uei", lambda s: s.notna().any()),
        n_distinct_auditor_firms=("auditor_firm_name", "nunique"),
    ).reset_index()
    coverage["years_since_last_audit"] = max_year_overall - coverage["last_audit_year"]
    coverage["audit_span_years"] = coverage["last_audit_year"] - coverage["first_audit_year"] + 1
    coverage["audit_frequency"] = coverage["n_audit_years"] / coverage["audit_span_years"]

    g = panel.groupby(SCHOOL_ID_COL)
    hist = pd.DataFrame(index=g.size().index)
    for col in HIST_NUMERIC_COLS:
        if col not in panel.columns:
            continue
        hist[f"hist_sum_{col}"] = g[col].sum(min_count=1)
        hist[f"hist_mean_{col}"] = g[col].mean()
        hist[f"hist_max_{col}"] = g[col].max()
    for col in HIST_BOOL_COLS:
        if col not in panel.columns:
            continue
        hist[f"hist_pct_years_{col}"] = g[col].mean()
    hist = hist.reset_index()

    trend = g.apply(lambda d: pd.Series({
        "trend_total_amount_expended": trend_slope(d["audit_year"], d.get("total_amount_expended", pd.Series(dtype=float))),
        "trend_fnd_n_findings": trend_slope(d["audit_year"], d.get("fnd_n_findings", pd.Series(dtype=float))),
    }), include_groups=False).reset_index()

    school = coverage.merge(latest, on=SCHOOL_ID_COL, how="left") \
                      .merge(hist, on=SCHOOL_ID_COL, how="left") \
                      .merge(trend, on=SCHOOL_ID_COL, how="left")
    return school


def profile_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for col in df.columns:
        s = df[col]
        rec = {
            "column": col,
            "dtype": str(s.dtype),
            "n_missing": int(s.isna().sum()),
            "pct_missing": round(100 * s.isna().mean(), 1),
            "n_unique": int(s.nunique(dropna=True)),
        }
        if pd.api.types.is_numeric_dtype(s):
            rec.update({
                "min": s.min(), "mean": round(s.mean(), 2) if s.notna().any() else np.nan,
                "max": s.max(),
            })
        rows.append(rec)
    return pd.DataFrame(rows)


def process(args: argparse.Namespace) -> None:
    os.makedirs(args.out_dir, exist_ok=True)

    print("[process] Building audit-year panel table ...")
    panel = build_panel(args.raw_dir)
    panel_path = os.path.join(args.out_dir, "fac_panel_by_audit_year.csv")
    panel.to_csv(panel_path, index=False)
    print(f"[process] Wrote {panel_path} ({len(panel)} rows, {panel.shape[1]} columns).")

    print("[process] Building school-level (one row per auditee_uei) table ...")
    school = build_school_table(panel)
    school_path = os.path.join(args.out_dir, "fac_school_level_features.csv")
    school.to_csv(school_path, index=False)
    print(f"[process] Wrote {school_path} ({len(school)} rows, {school.shape[1]} columns).")

    for label, df in [("panel", panel), ("school", school)]:
        prof_path = os.path.join(args.out_dir, f"eda_summary_{label}.csv")
        profile_dataframe(df).to_csv(prof_path, index=False)
        print(f"[process] Wrote {prof_path}")

    print("\n[process] Quick summary:")
    print(f"  Panel:  {len(panel)} audit reports, "
          f"{panel[SCHOOL_ID_COL].nunique()} distinct auditees, "
          f"years {int(panel['audit_year'].min())}-{int(panel['audit_year'].max())}")
    print(f"  School: {len(school)} rows (target grain: one row per school)")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    pull_p = sub.add_parser("pull", help="Pull raw data from the FAC API")
    pull_p.add_argument("--entity-type", default="higher-ed",
                         help="FAC entity_type filter value (default: higher-ed). "
                              "Other observed values: local, state, tribal, non-profit, unknown.")
    pull_p.add_argument("--out-dir", default="data/raw")
    pull_p.add_argument("--audit-year-min", type=int, default=None)
    pull_p.add_argument("--audit-year-max", type=int, default=None)
    pull_p.add_argument("--max-reports", type=int, default=None,
                         help="Cap number of general rows pulled (useful for a quick test run).")
    pull_p.add_argument("--batch-size", type=int, default=100,
                         help="How many report_ids to pack into each child-endpoint in.() filter.")
    pull_p.add_argument("--page-size", type=int, default=10000)
    pull_p.add_argument("--sleep", type=float, default=0.0,
                         help="Seconds to sleep between child-endpoint batch requests.")
    pull_p.add_argument("--refresh", action="store_true",
                         help="Re-pull even if the raw CSV already exists.")
    pull_p.set_defaults(func=pull)

    proc_p = sub.add_parser("process", help="Build panel + school-level tables from raw CSVs")
    proc_p.add_argument("--raw-dir", default="data/raw")
    proc_p.add_argument("--out-dir", default="data/processed")
    proc_p.set_defaults(func=process)

    all_p = sub.add_parser("all", help="Run pull then process")
    all_p.add_argument("--entity-type", default="higher-ed")
    all_p.add_argument("--out-dir", default="data",
                        help="Parent dir; raw/ and processed/ subdirs are created under it.")
    all_p.add_argument("--audit-year-min", type=int, default=None)
    all_p.add_argument("--audit-year-max", type=int, default=None)
    all_p.add_argument("--max-reports", type=int, default=None)
    all_p.add_argument("--batch-size", type=int, default=100)
    all_p.add_argument("--page-size", type=int, default=10000)
    all_p.add_argument("--sleep", type=float, default=0.0)
    all_p.add_argument("--refresh", action="store_true")

    def run_all(args):
        raw_dir = os.path.join(args.out_dir, "raw")
        processed_dir = os.path.join(args.out_dir, "processed")
        args.out_dir_pull = raw_dir
        pull_args = argparse.Namespace(**{**vars(args), "out_dir": raw_dir})
        pull(pull_args)
        proc_args = argparse.Namespace(raw_dir=raw_dir, out_dir=processed_dir)
        process(proc_args)

    all_p.set_defaults(func=run_all)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
