# FAC EDA & ML Preprocessing

Turns Federal Audit Clearinghouse (FAC) Single Audit data into an ML-ready
table with **one row per school**, so it can be joined onto an existing
schools master table (e.g. a College Scorecard-based institution list).

Pipeline script: [scripts/fac_eda_pipeline.py](scripts/fac_eda_pipeline.py)
Data dictionaries this was built from: [FAC API Data Dictionary CSVs/](FAC%20API%20Data%20Dictionary%20CSVs/)

## Why this exists

FAC's API (`api.fac.gov`) exposes Single Audit data as ~10 normalized,
relational endpoints (one row per audit for `general`, but many rows per
audit for `federal_awards`, `findings`, `passthrough`, etc., all linked by
`report_id`). An ML feature table needs one row per entity. This pipeline
pulls the relevant endpoints, aggregates every child endpoint up to one row
per audit report, then rolls all of a school's audit reports (it typically
has one per year, only in years it crossed the federal-expenditure audit
threshold) up into one row per school.

## Decisions made, and why

### 1. Scope: `entity_type = higher-ed`

FAC's `entity_type` field (on `/general`) takes the values `higher-ed`,
`local`, `state`, `tribal`, `non-profit`, `unknown` — confirmed live against
the API, since the data dictionary only describes the field in prose
("States, Local Governments, Indian Tribes, Institutions of Higher
Education, NonProfit") without giving the actual enum codes. This pipeline
defaults to `higher-ed` to match the workspace's existing College
Scorecard / higher-ed school data (`Most-Recent-Cohorts-Institution.csv`,
`college_scorecard_crosswalk/`). It's a `--entity-type` flag, not a
hardcoded filter, if you need a different population later.

As of this writing: **~2,262 distinct higher-ed institutions** across
**~15,976 audit-report rows**, spanning audit years 2016–2026 (partial).

### 2. Grain: raw → panel (one row per audit) → school (one row per school)

- **Raw**: each FAC endpoint, pulled as-is, one CSV per endpoint, in
  `data/raw/`.
- **Panel** (`fac_panel_by_audit_year.csv`): one row per `report_id`
  (≈ one row per school per audit year). All child endpoints are
  aggregated up to this grain (see field list below) and merged onto
  `general`. This is the natural grain of Single Audit data and is useful
  on its own for panel/time-series modeling.
- **School** (`fac_school_level_features.csv`): one row per school,
  built by rolling the panel table up across every audit year available
  for that school. This is the deliverable that matches "one row per
  school." See "School-level rollup" below for exactly how a multi-year
  history collapses into one row.

### 3. Join key: `auditee_ein`, not `auditee_uei`

This was the single biggest gotcha found while building this, verified
against live data:

FAC's pre-~2022 audits were migrated from the legacy Census Bureau
Federal Audit Clearinghouse system and were **never backfilled with a real
SAM.gov UEI** — `auditee_uei` is the literal string `"GSA_MIGRATION"` for
those rows. This is not an edge case: **~59% of higher-ed audit rows
(9,452 of 15,976, essentially all of audit_year ≤ 2021) carry this
placeholder.** Grouping by `auditee_uei` as originally planned silently
collapsed unrelated schools together.

`auditee_ein` (the institution's IRS Employer Identification Number), by
contrast, is populated on 100% of higher-ed rows across the entire date
range (verified by direct query). So:

- `auditee_ein` is the **grouping key** used to build the school-level
  table (`SCHOOL_ID_COL` in the script).
- `auditee_uei` is still carried through — as `GSA_MIGRATION` values
  converted to null, plus a `latest_auditee_uei` and a semicolon-joined
  `all_ueis` column at the school level, and a `has_valid_uei` flag — so
  you can use it for joins to systems that key on UEI once a school has a
  post-2022 audit on file.

One real example that motivated this (Stanford University, EIN
`941156365`): the same institution appears under `auditee_uei =
GSA_MIGRATION` for audit years 2016–2021, gets a real UEI
(`HJD6G4D6TJY5`) starting 2022, and its `auditee_name` on file changes
from `"STANFORD UNIVERSITY"` to `"Stanford University"` to `"Board of
Trustees of the Leland Stanford Junior University"` across that same
span. EIN is the only stable identifier across the whole window.

**Caveat to carry forward**: `auditee_ein` is still self-reported per
audit, not a verified master identifier. `general.csv`'s
`is_multiple_eins` flag and the `additional_eins` endpoint indicate some
institutions (mainly large university systems) file under more than one
EIN for different sub-units in different years. This pipeline does not
attempt that reconciliation — it's flagged (`org_n_additional_eins`) but
not resolved. If you find a school splitting into two rows that should be
one, this is the first thing to check.

### 4. Joining to your existing school master table

**FAC has no UNITID, OPEID, or IPEDS identifier of any kind**, and College
Scorecard (`Most-Recent-Cohorts-Institution.csv`) has no EIN or UEI. There
is no clean key to join on directly. The school-level output carries
`latest_auditee_name`, `latest_auditee_city`, `latest_auditee_state`,
`latest_auditee_zip`, `all_names` (every name variant seen), `auditee_ein`,
and `latest_auditee_uei` specifically so you can fuzzy-match on
name + state (optionally + zip) against your school master, the same
pattern already used in this workspace for the SEVIS↔IPEDS crosswalk
(`college_scorecard_crosswalk/crosswalk.py`, which uses `rapidfuzz`). That
matching step is not included here — it needs your actual school master
file as an input — but this output is shaped to make it straightforward.
Expect to hand-review ambiguous matches: name strings in FAC are
self-reported and inconsistently formatted (see the Stanford example
above).

### 5. Resubmissions: no extra handling needed

`/general` already returns only the current (`resubmission_status =
most_recent`) version of each audit — confirmed by querying with and
without an explicit `resubmission_status=eq.most_recent` filter and
getting identical counts (15,976 both times). Superseded versions live
only in `/resubmission` and are not pulled or needed here. `general`'s own
`resubmission_version` field is kept and turned into a `was_resubmitted`
boolean (`resubmission_version > 0`) as a light data-quality signal, not
as a filter.

### 6. API pagination cap

`api.fac.gov` (PostgREST) hard-caps every response at **20,000 rows**
regardless of the requested `limit` (confirmed: a request with
`limit=100000` still returned exactly 20,000 rows, with
`Content-Range: 0-19999/22577` revealing the true total). The pull step
therefore pages every request with `limit`/`offset` and keeps requesting
until a short page is returned — this matters most for `federal_awards`,
`passthrough`, and `notes_to_sefa`, which are large: a single large
university system's 2016 audit alone returned 9,737 `federal_awards`
rows.

## Fields brought over, and why

Everything below is computed by `scripts/fac_eda_pipeline.py process`.
Prefixes identify source endpoint: `fa_` = federal_awards, `fnd_` =
findings, `fndtxt_` = findings_text, `cap_` = corrective_action_plans,
`pt_` = passthrough, `sefa_` = notes_to_sefa, `sa_` = secondary_auditors,
`org_` = additional_eins/additional_ueis.

| Endpoint | Row grain in FAC | What's kept, and why |
|---|---|---|
| `general` | 1 per audit | Kept almost whole: entity/contact identity fields, audit type & opinion results (`gaap_results`, `sp_framework_*`), risk-relevant Y/N disclosures (going concern, internal control deficiency/material weakness, material noncompliance, low-risk auditee, prior findings), `total_amount_expended`, `dollar_threshold`, filing dates. These are the standard Single Audit risk indicators auditors and cognizant agencies already use. |
| — derived | — | `days_fyend_to_submitted`, `days_fyend_to_accepted`, `is_late_submission` (> 273 days ≈ the 9-month Uniform Guidance §200.512 deadline). We don't have the "auditor's report received" date, so this is an approximation against the statutory ceiling, not exact days-late. |
| `federal_awards` | 1 per award line | Aggregated per audit: award count, distinct clusters/agencies, $ expended, major-program count & rate, loan count & balance, direct vs. passthrough rate & $, findings_count sum, and a breakdown of audit opinion type per award (`fa_n_unqualified/qualified/adverse/disclaimer`). This is the richest table (federal dependency, program diversity, funding scale) so it gets the most feature engineering. |
| `findings` | 1 per (finding × award) | De-duplicated to distinct `(report_id, reference_number)` before counting, since one finding can be listed against multiple awards — otherwise counts would overstate distinct compliance problems. Counts of material weaknesses, significant deficiencies, modified opinions, questioned costs, repeat findings, other matters, and distinct compliance-requirement-type codes touched (`fnd_n_distinct_type_requirements`). |
| `findings_text` | 1 per finding | Count + average narrative length. Not NLP-processed here (see Future Work). |
| `corrective_action_plans` | 1 per finding | Plan count only (`cap_n_plans`). No coverage-ratio-vs-findings column is computed because CAPs and findings are keyed differently (`finding_ref_number` vs. `award_reference`/`reference_number`) and a naive ratio was judged more likely to mislead than help — revisit if you need it. |
| `passthrough` | 1 per passthrough relationship | Relationship count and distinct passthrough-entity count — a rough proxy for subrecipient-monitoring complexity. |
| `notes_to_sefa` | 1 per note | Note count, whether the 10% de minimis indirect cost rate was used, chart/table presence. |
| `secondary_auditors` | 1 per secondary auditor | Count only — multiple secondary auditors can indicate a decentralized/multi-campus audit. |
| `additional_eins` / `additional_ueis` | 1 per extra ID | Counts only, as an organizational-complexity signal (umbrella entities filing under one report but covering multiple sub-entities). |
| `resubmission` | — | **Not pulled.** `general` already reflects the current version (see decision #5); the separate endpoint's linkage chain (`previous_report_id`/`next_report_id`) wasn't needed for anything computed here. |

Free-text fields (`planned_action`, `finding_text`, `accounting_policies`,
`content`, `rate_explained`) are **not** brought into the feature tables —
only their presence/length is. See Future Work.

## School-level rollup (`fac_school_level_features.csv`)

For each `auditee_ein`:

- **Coverage**: `n_audit_years`, `first_audit_year`, `last_audit_year`,
  `years_since_last_audit` (relative to the newest audit_year in the pull),
  `audit_span_years`, `audit_frequency` (years audited ÷ span — schools are
  only required to file in years they cross the federal expenditure
  threshold, so this is rarely 1.0), `n_distinct_auditor_firms` (auditor
  name is upper-cased/trimmed first since the same firm is inconsistently
  cased across years in FAC's own data — e.g. `"PRICEWATERHOUSECOOPERS
  LLP"` vs. `"PricewaterhouseCoopers LLP"` — but this is a heuristic, not
  true entity resolution, and can still overcount, e.g. a
  `"(PWC.COM)"`-suffixed variant was left uncollapsed in testing).
- **`latest_*`**: a snapshot of every feature from the school's most
  recent audit year (identity fields, opinion results, all `fa_`/`fnd_`/
  etc. aggregates, and boolean risk flags).
- **`hist_sum_*` / `hist_mean_*` / `hist_max_*`**: sum, mean, and max of
  every numeric feature (see `HIST_NUMERIC_COLS` in the script) across
  all of the school's audit years — e.g. `hist_sum_fnd_n_findings` is
  lifetime finding count on file, `hist_mean_fa_n_awards` is typical award
  portfolio size.
- **`hist_pct_years_*`**: for boolean risk flags (e.g.
  `is_low_risk_auditee`, `is_late_submission`, `fnd_has_findings`), the
  fraction of audited years the flag was true — a persistence signal that
  a single latest-year snapshot would miss.
- **`trend_total_amount_expended`**, **`trend_fnd_n_findings`**: linear
  slope (via `numpy.polyfit`) vs. audit year — direction/rate of change
  in federal funding scale and finding count. Needs ≥2 audit years or is
  `NaN`.

This is deliberately feature-rich (~158 columns in testing) rather than
pre-selected down to a modeling-ready subset — column pruning/selection is
left for the actual ML step once a target variable is defined, since the
"right" features depend on what's being predicted (e.g. audit risk vs.
funding growth vs. compliance-burden classification).

## Known limitations / not handled

- **No target variable is defined or engineered here.** This is a feature
  table, not a labeled dataset — decide what you're predicting before
  pruning/selecting columns.
- **EIN can still be ambiguous** for umbrella/multi-entity university
  systems (see decision #3's caveat).
- **No fuzzy join to a school master table is performed** — see decision
  #4. `crosswalk.py` in `college_scorecard_crosswalk/` is a working
  reference for the rapidfuzz-based approach if you want this pipeline
  extended to actually produce the join.
- **Free text isn't mined.** `findings_text`, `corrective_action_plans`,
  and `notes_to_sefa` content is rich (auditor descriptions of the actual
  compliance problem) but only length/presence is captured.
- **`is_late_submission` is approximate** (see decision, endpoint table).
- **`cap_n_plans` isn't matched against `fnd_n_findings`** per-school, due
  to the finding-vs-CAP key mismatch noted above.
- Every numeric/boolean field pulled straight from `general`/`federal_awards`/etc.
  can itself be missing/blank in FAC's source data (self-reported by the
  auditee) — `data/processed/eda_summary_panel.csv` and
  `eda_summary_school.csv` report per-column missingness so this is
  visible before modeling, not hidden.

## Future work

- NLP features from finding/CAP narrative text (e.g. embedding similarity
  to known compliance-issue categories, or simple keyword flags).
- Resolve the EIN/UEI multi-entity ambiguity using `additional_eins`/
  `additional_ueis` rather than just counting them.
- Build the actual fuzzy-match join to the school master table (extending
  `college_scorecard_crosswalk/crosswalk.py`'s approach) so this becomes a
  true one-step FAC-features-onto-schools pipeline.
- `cognizant_agency`/`oversight_agency` are currently passed through as raw
  two-digit codes; consider mapping to agency names for interpretability.

## Running it

```bash
# from FAC_API_Audit/, with a .fac_api_key file in place (already gitignored)

# 1. Pull raw data (writes data/raw/*.csv)
python scripts/fac_eda_pipeline.py pull --entity-type higher-ed --out-dir data/raw

# 2. Build the panel + school-level feature tables (writes data/processed/*.csv)
python scripts/fac_eda_pipeline.py process --raw-dir data/raw --out-dir data/processed

# Or both in one go:
python scripts/fac_eda_pipeline.py all --out-dir data
```

Useful flags on `pull`: `--max-reports N` for a fast test run,
`--audit-year-min` / `--audit-year-max` to restrict the date range,
`--batch-size` to change how many `report_id`s are packed into each
child-endpoint request (default 100), `--refresh` to re-pull over
existing raw CSVs.

**Scale note**: a full unfiltered `higher-ed` pull is ~16,000 audit
reports; `federal_awards`/`passthrough`/`notes_to_sefa` are the largest
child tables (a single large university system's one-year audit alone can
run several thousand `federal_awards` rows) — expect the full raw pull to
take a while and produce tens of MB of CSVs. Test with `--max-reports`
first. `data/` is gitignored (see `.gitignore`) since it's fully
regenerable from the script.

Outputs, all in `data/processed/`:

- `fac_panel_by_audit_year.csv` — one row per audit report.
- `fac_school_level_features.csv` — one row per school (join target).
- `eda_summary_panel.csv`, `eda_summary_school.csv` — per-column dtype,
  missingness %, distinct-value count, and (for numeric columns)
  min/mean/max, for both output tables.

## Local setup and run guide

The repo includes a few different workflows:

1. Split the Excel data dictionary into one CSV per sheet.
2. Query the FAC API for a small proof-of-concept sample of audits.
3. Merge the collected CSV outputs into a single Excel workbook.
4. Run the larger EDA / feature-building pipeline.

Follow the steps below from the repo root (`/Users/mmg/Desktop/Guidehouse/Work/FAC_API_Audit`).

### 1. Create a local Python environment

```bash
cd /Users/mmg/Desktop/Guidehouse/Work/FAC_API_Audit
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Install dependencies

```bash
pip install --upgrade pip
pip install pandas openpyxl requests
```

### 3. Create your local FAC API key file

The script expects a local key file named `.fac_api_key` in the repo root. This file is ignored by git and should never be committed.

```bash
cd /Users/mmg/Desktop/Guidehouse/Work/FAC_API_Audit
printf '%s\n' 'YOUR_FAC_API_KEY_HERE' > .fac_api_key
```

You can also set the key via environment variable instead:

```bash
export FAC_API_KEY="YOUR_FAC_API_KEY_HERE"
```

The scripts will read the key from `FAC_API_KEY` first, then fall back to `.fac_api_key`.

### 4. Split the Excel workbook into CSV files

This converts `FAC API Data Dictionary.xlsx` into one CSV per worksheet, stored under `FAC API Data Dictionary CSVs/`.

```bash
python scripts/split_excel_sheets_to_csv.py "FAC API Data Dictionary.xlsx"
```

This writes files such as:

- `FAC API Data Dictionary CSVs/General.csv`
- `FAC API Data Dictionary CSVs/Federal Awards.csv`
- `FAC API Data Dictionary CSVs/Findings.csv`

The script strips the workbook prefix from file names and stores the outputs in a folder named `FAC API Data Dictionary CSVs`.

### 5. Run the FAC API proof-of-concept

The proof-of-concept script can run against either explicit `report_id` values or a CSV file containing recent IDs.

#### Example A: run against specific report IDs

```bash
python scripts/poc_collect_audit_details.py \
  --ids 2025-12-GSAFAC-0000422565,2024-12-GSAFAC-0000423894 \
  --out poc_results_sample
```

#### Example B: read the most recent IDs from the `general` CSV

```bash
python scripts/poc_collect_audit_details.py \
  --from-csv "FAC API Data Dictionary CSVs/General.csv" \
  --id-column report_id \
  --count 10 \
  --out poc_results_10
```

The script will:

- iterate over each `report_id`
- query the main FAC endpoints (`general`, `federal_awards`, `findings`, etc.)
- save one JSON file per audit in the output directory
- write a combined CSV for each endpoint in `poc_results_10/csvs/`

Typical output structure:

```text
poc_results_10/
  2025-12-GSAFAC-0000422565.json
  2024-12-GSAFAC-0000423894.json
  ...
  csvs/
    general.csv
    federal_awards.csv
    findings.csv
    findings_text.csv
    corrective_action_plans.csv
    passthrough.csv
    notes_to_sefa.csv
    resubmission.csv
```

### 6. Merge all collected CSVs into one Excel workbook

Once the POC has written endpoint CSVs, this script combines them into a single `.xlsx` workbook with one sheet per CSV.

```bash
python scripts/merge_csvs_to_excel.py \
  --csv-dir poc_results_10/csvs \
  --out poc_results_10/combined.xlsx
```

This creates a workbook like:

```text
poc_results_10/combined.xlsx
```

with sheets such as `general`, `federal_awards`, `findings`, `corrective_action_plans`, etc.

### 7. Run the full FAC EDA / feature-building pipeline

The project also includes a larger pipeline script that pulls raw FAC API data and prepares feature tables.

```bash
# Pull raw data into data/raw
python scripts/fac_eda_pipeline.py pull --entity-type higher-ed --out-dir data/raw

# Build processed panel and school-level tables into data/processed
python scripts/fac_eda_pipeline.py process --raw-dir data/raw --out-dir data/processed

# Or do both in one command
python scripts/fac_eda_pipeline.py all --out-dir data
```

### 8. Useful notes

- The repo ignores generated data and API key files via `.gitignore`.
- `data/` is generated output and should not be committed.
- `FAC_API_KEY` and `.fac_api_key` are the only supported local key locations.
- For a small test run, use `--count` on the POC script or `--max-reports` on the EDA pipeline to keep the dataset small while validating the logic.

## Running it

```bash
# from FAC_API_Audit/, with a .fac_api_key file in place (already gitignored)

# 1. Pull raw data (writes data/raw/*.csv)
python scripts/fac_eda_pipeline.py pull --entity-type higher-ed --out-dir data/raw

# 2. Build the panel + school-level feature tables (writes data/processed/*.csv)
python scripts/fac_eda_pipeline.py process --raw-dir data/raw --out-dir data/processed

# Or both in one go:
python scripts/fac_eda_pipeline.py all --out-dir data
```

Useful flags on `pull`: `--max-reports N` for a fast test run,
`--audit-year-min` / `--audit-year-max` to restrict the date range,
`--batch-size` to change how many `report_id`s are packed into each
child-endpoint request (default 100), `--refresh` to re-pull over
existing raw CSVs.

**Scale note**: a full unfiltered `higher-ed` pull is ~16,000 audit
reports; `federal_awards`/`passthrough`/`notes_to_sefa` are the largest
child tables (a single large university system's one-year audit alone can
run several thousand `federal_awards` rows) — expect the full raw pull to
take a while and produce tens of MB of CSVs. Test with `--max-reports`
first. `data/` is gitignored (see `.gitignore`) since it's fully
regenerable from the script.