"""Enrich CU's Texas systems with operator contacts from TX Water Districts.

Pulls the TCEQ Texas Water Districts dataset from data.texas.gov (an open
data portal with bulk download — no scraping), then fuzzy-matches each
district to a Texas EPA SDWIS system by normalized name + county. Writes
one PWSID-keyed Parquet the dashboard loads at runtime.

Why fuzzy match: the TX Water Districts dataset has rich contacts
(first/last name, title, phone, address) but does NOT include PWSIDs.
A district named "Harris County MUD 123" usually corresponds 1:1 with a
PWS named "HARRIS CO MUD 123 / HARRIS COUNTY MUD #123" — but not always
cleanly. We score on multiple signals and keep only confident matches.

Usage:
    # 1. Get a free Socrata app token (~2 min, no payment):
    #    https://data.texas.gov/profile/edit/developer_settings
    # 2. Set the env var:
    #    [Environment]::SetEnvironmentVariable("SOCRATA_APP_TOKEN", "<token>", "User")
    # 3. Restart PowerShell so the var is loaded, then:
    python enrich_tx_districts.py

Output:
    data/tx_district_contacts.parquet
        pwsid, district_number, district_name, district_type,
        contact_name, contact_title, phone, address_full, county,
        match_score, match_reason
        (one row per matched PWSID — unmatched systems just absent)

Caveats:
    - Coverage is partial: many small TX CWS are NOT districts and will
      have no match. That's expected — the dashboard shows TX contacts
      as supplemental, never replacing EPA's admin_name.
    - Matches at score >= MATCH_THRESHOLD are surfaced. Below that, the
      script logs the candidate for spot-check but does not write it.
    - Source field names contain typos ("distict_name", "distict_address_1")
      preserved from the publisher. We normalize on read.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
TX_PARQUET_DIR = DATA_DIR / "TX"
OUTPUT_FILE = DATA_DIR / "tx_district_contacts.parquet"

# Both the v2 SODA endpoint (/resource/.json) and the CSV export endpoint
# silently strip column values for anonymous requests on this dataset.
# An app token unlocks full access. Tokens are free at
# https://data.texas.gov/profile/edit/developer_settings — no payment.
SODA_URL = "https://data.texas.gov/resource/ruhk-kxgs.json"
CSV_URL = "https://data.texas.gov/api/views/ruhk-kxgs/rows.csv?accessType=DOWNLOAD"
PAGE_SIZE = 50_000  # Dataset is ~3k rows — one page covers it.

# Score thresholds — tuned for high precision, accepting low recall.
# Wrong matches are worse than missing matches for staff confidence.
MATCH_THRESHOLD = 60
SCORE_EXACT_NAME = 50
SCORE_CONTAINS_NAME = 30
SCORE_COUNTY = 20
SCORE_CITY = 10

# Words to strip when normalizing system / district names before comparing.
# Order matters: longer patterns first so e.g. "WATER SUPPLY CORP" gets
# stripped before "WATER" alone would catch part of it.
NORMALIZE_STRIP = [
    "WATER SUPPLY CORPORATION", "WATER SUPPLY CORP", "WATER SUPPLY CO",
    "WATER CONTROL AND IMPROVEMENT DISTRICT",
    "WATER CONTROL & IMPROVEMENT DISTRICT",
    "MUNICIPAL UTILITY DISTRICT",
    "SPECIAL UTILITY DISTRICT",
    "FRESH WATER SUPPLY DISTRICT",
    "PUBLIC UTILITY DISTRICT",
    "PUBLIC WATER SYSTEM",
    "WATER DISTRICT",
    "UTILITY DISTRICT",
    "RIVER AUTHORITY",
    "WATERWORKS",
    "WSC", "WCID", "MUD", "SUD", "FWSD", "PUD",  # common abbreviations
    "WATER", "SUPPLY", "DISTRICT", "CORPORATION", "CORP", "COMPANY",
    "INCORPORATED", "INC", "LLC", "CO", "LTD",
    "CITY OF", "TOWN OF", "VILLAGE OF",
]


def _norm(s: str) -> str:
    """Uppercase, strip non-alphanumeric, drop common org/type words."""
    if not s:
        return ""
    s = s.upper()
    # Strip parenthesized suffixes first
    s = re.sub(r"\([^)]*\)", "", s)
    # Replace special chars with spaces so word boundaries are clean
    s = re.sub(r"[^A-Z0-9 ]+", " ", s)
    # Iteratively strip the canonical patterns (with word boundaries)
    for word in NORMALIZE_STRIP:
        s = re.sub(rf"\b{re.escape(word)}\b", " ", s)
    # Collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _norm_county(s: str) -> str:
    if not s:
        return ""
    s = s.upper().strip()
    return re.sub(r"\s+COUNTY$", "", s)


def _require_token() -> str:
    token = os.environ.get("SOCRATA_APP_TOKEN", "").strip()
    if not token:
        print(
            "ERROR: SOCRATA_APP_TOKEN env var not set.\n"
            "Get a free token at https://data.texas.gov/profile/edit/developer_settings\n"
            "Then in PowerShell:\n"
            '  [Environment]::SetEnvironmentVariable("SOCRATA_APP_TOKEN", "<token>", "User")\n'
            "Restart PowerShell, then re-run this script.",
            file=sys.stderr,
        )
        sys.exit(1)
    return token


def fetch_tx_districts() -> pd.DataFrame:
    """Pull all rows from the TCEQ Texas Water Districts dataset via SODA JSON.

    Uses the app token. SODA's JSON returns proper rows once authenticated
    (the empty-row behavior was the anonymous-access guard).
    """
    token = _require_token()
    print(f"Fetching TX Water Districts (authenticated SODA JSON) ...")
    r = requests.get(
        SODA_URL,
        params={"$limit": PAGE_SIZE},
        timeout=180,
        headers={
            "User-Agent": "communities-unlimited-water-dashboard/1.0",
            "X-App-Token": token,
        },
    )
    r.raise_for_status()
    rows = r.json()
    if not rows:
        print("  ERROR: API returned 0 rows. Aborting.", file=sys.stderr)
        sys.exit(1)
    df = pd.DataFrame(rows)
    print(f"  got {len(df):,} districts, {len(df.columns)} columns")
    print(f"  columns: {sorted(df.columns.tolist())}")
    if len(df.columns) == 0:
        print(
            "  ERROR: rows came back as empty objects. Your token may be invalid\n"
            "  or this dataset requires additional grants. Check your token at\n"
            "  https://data.texas.gov/profile/edit/developer_settings",
            file=sys.stderr,
        )
        sys.exit(1)
    return df


def normalize_districts(raw: pd.DataFrame) -> pd.DataFrame:
    """Normalize TCEQ field names + filter to actionable rows.

    TCEQ exposes this dataset under two naming conventions depending on the
    endpoint: snake_case (with publisher typos like 'distict_name') via SODA
    JSON, Title Case With Spaces via CSV export. We accept either.
    """
    # Each target column lists every source name we've seen for it.
    # Match is case-insensitive; first hit wins per target.
    column_aliases = {
        "district_name":   ["district_name", "distict_name", "District Name", "Distict Name"],
        "address_1":       ["distict_address_1", "district_address_1", "District Address 1", "Distict Address 1", "Address 1"],
        "district_type":   ["district_type", "District Type"],
        "activity_status": ["activity_status", "Activity Status", "Status"],
        "city":            ["district_city", "District City", "City"],
        "zip":             ["district_zip_code", "District Zip Code", "Zip Code", "Zip"],
        "first_name":      ["first_name", "First Name"],
        "last_name":       ["last_name", "Last Name"],
        "job_title":       ["job_title", "Job Title", "Title"],
        "phone":           ["phone", "Phone", "Phone Number"],
        "county":          ["county", "County"],
        "district_number": ["district_number", "District Number"],
    }
    # Build case-insensitive lookup of raw columns -> actual column name.
    raw_ci = {c.lower(): c for c in raw.columns}
    keep: dict[str, str] = {}  # raw_column_name -> target_name
    missing_targets: list[str] = []
    for target, aliases in column_aliases.items():
        found = None
        for alias in aliases:
            if alias.lower() in raw_ci:
                found = raw_ci[alias.lower()]
                break
        if found:
            keep[found] = target
        else:
            missing_targets.append(target)
    if "district_name" in missing_targets:
        print(
            "ERROR: could not find a 'district_name'-style column. Got:\n  "
            + "\n  ".join(sorted(raw.columns.tolist())),
            file=sys.stderr,
        )
        sys.exit(1)
    if missing_targets:
        print(f"  note: optional columns not present: {missing_targets}")
    df = raw[list(keep.keys())].rename(columns=keep).copy()

    # Ensure every target column exists as a Series. df.get() on a DataFrame
    # returns a scalar when the column is missing, which breaks .fillna() etc.
    for target in column_aliases:
        if target not in df.columns:
            df[target] = ""

    # Filter to active districts only — inactive contacts are stale by definition.
    before = len(df)
    df = df[df["activity_status"].astype(str).str.upper().str.strip() == "ACTIVE"]
    print(f"  filtered to ACTIVE: {len(df):,} of {before:,}")

    # Build derived fields used for matching + display.
    for c in ("district_name", "county", "city", "first_name", "last_name",
              "job_title", "phone", "address_1", "zip", "district_type",
              "district_number"):
        df[c] = df[c].fillna("").astype(str).str.strip()

    df["norm_name"] = df["district_name"].map(_norm)
    df["norm_county"] = df["county"].map(_norm_county)
    df["norm_city"] = df["city"].str.upper().str.strip()

    # Drop rows we can't match on.
    df = df[df["norm_name"] != ""]
    print(f"  with normalizable names: {len(df):,}")

    df["contact_name"] = (df["first_name"] + " " + df["last_name"]).str.strip()
    df["address_full"] = df.apply(
        lambda r: " ".join(
            x for x in [
                r["address_1"],
                ", ".join(
                    p for p in [
                        r["city"],
                        f"TX {r['zip']}".strip(),
                    ] if p
                ),
            ] if x
        ).strip(),
        axis=1,
    )
    return df


def load_tx_systems() -> pd.DataFrame:
    """Read EPA's Texas PWS roster + their county-served lookup."""
    sys_path = TX_PARQUET_DIR / "water_systems.parquet"
    geo_path = TX_PARQUET_DIR / "geo.parquet"
    if not sys_path.exists():
        print(f"ERROR: {sys_path} not found — run the SDWIS ETL first.", file=sys.stderr)
        sys.exit(1)

    sys_df = pd.read_parquet(
        sys_path,
        columns=["pwsid", "pws_name", "city_name", "pws_activity_code"],
    )
    sys_df = sys_df[sys_df["pws_activity_code"] == "A"].copy()
    sys_df["norm_name"] = sys_df["pws_name"].fillna("").map(_norm)
    sys_df["norm_city"] = sys_df["city_name"].fillna("").str.upper().str.strip()
    print(f"  loaded {len(sys_df):,} active TX systems from EPA pull")

    counties_per_pws: dict[str, set[str]] = {}
    if geo_path.exists():
        geo_df = pd.read_parquet(
            geo_path, columns=["pwsid", "county_served"]
        )
        for _, r in geo_df.iterrows():
            cnt = _norm_county(str(r.get("county_served") or ""))
            if cnt:
                counties_per_pws.setdefault(r["pwsid"], set()).add(cnt)
        print(f"  loaded county-served for {len(counties_per_pws):,} PWS")
    sys_df["counties"] = sys_df["pwsid"].map(
        lambda p: counties_per_pws.get(p, set())
    )
    return sys_df


def match(sys_df: pd.DataFrame, dist_df: pd.DataFrame) -> pd.DataFrame:
    """For each TX system, find the best district match (if any).

    Returns one row per PWSID with score >= MATCH_THRESHOLD.
    """
    # Index districts by normalized first token of name for cheap candidate filter.
    # Without this we'd do len(sys) * len(dist) compares (~5k * ~3k = 15M).
    dist_by_first: dict[str, list[int]] = {}
    for idx, row in dist_df.iterrows():
        first_token = row["norm_name"].split(" ")[0] if row["norm_name"] else ""
        if first_token:
            dist_by_first.setdefault(first_token, []).append(idx)

    out_rows = []
    near_misses = 0
    for _, sys_row in sys_df.iterrows():
        sys_norm = sys_row["norm_name"]
        if not sys_norm:
            continue
        sys_first = sys_norm.split(" ")[0]
        # Candidates: any district whose normalized name shares the first
        # significant token. Adequate filter for our domain.
        candidate_idx = dist_by_first.get(sys_first, [])
        # Also try districts where any token of the system name matches the
        # district's first token (catches "MUD 123 OF BAYTOWN" vs "BAYTOWN MUD 123").
        for tok in sys_norm.split(" ")[1:4]:
            candidate_idx = candidate_idx + dist_by_first.get(tok, [])
        if not candidate_idx:
            continue

        best_score = 0
        best_idx = None
        best_reason = ""
        for di in set(candidate_idx):
            d = dist_df.loc[di]
            d_norm = d["norm_name"]
            score = 0
            reasons = []
            if d_norm == sys_norm:
                score += SCORE_EXACT_NAME
                reasons.append("exact-name")
            elif d_norm and (d_norm in sys_norm or sys_norm in d_norm):
                score += SCORE_CONTAINS_NAME
                reasons.append("contains-name")
            if d["norm_county"] and d["norm_county"] in sys_row["counties"]:
                score += SCORE_COUNTY
                reasons.append("county")
            if d["norm_city"] and d["norm_city"] == sys_row["norm_city"]:
                score += SCORE_CITY
                reasons.append("city")
            if score > best_score:
                best_score = score
                best_idx = di
                best_reason = "+".join(reasons)

        if best_idx is None:
            continue
        if best_score < MATCH_THRESHOLD:
            near_misses += 1
            continue

        d = dist_df.loc[best_idx]
        out_rows.append({
            "pwsid": sys_row["pwsid"],
            "district_number": d.get("district_number", ""),
            "district_name": d.get("district_name", ""),
            "district_type": d.get("district_type", ""),
            "contact_name": d.get("contact_name", "").strip() or "",
            "contact_title": d.get("job_title", "") or "",
            "phone": d.get("phone", "") or "",
            "address_full": d.get("address_full", "") or "",
            "county": d.get("county", "") or "",
            "match_score": int(best_score),
            "match_reason": best_reason,
        })

    matched = pd.DataFrame(out_rows)
    print(
        f"\n  matched: {len(matched):,} of {len(sys_df):,} TX systems "
        f"(score >= {MATCH_THRESHOLD})"
    )
    print(f"  near-misses (below threshold, dropped): {near_misses:,}")
    if not matched.empty:
        print(
            f"  score distribution: min={matched['match_score'].min()}, "
            f"median={int(matched['match_score'].median())}, "
            f"max={matched['match_score'].max()}"
        )
    return matched


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    print("Step 1: Fetch TX Water Districts from data.texas.gov")
    raw = fetch_tx_districts()

    print("\nStep 2: Normalize")
    dist_df = normalize_districts(raw)

    print("\nStep 3: Load EPA Texas systems")
    sys_df = load_tx_systems()

    print("\nStep 4: Fuzzy match")
    matched = match(sys_df, dist_df)

    if matched.empty:
        print("\nNo matches met the threshold. Not writing output.", file=sys.stderr)
        sys.exit(1)

    matched.to_parquet(OUTPUT_FILE, index=False)
    print(f"\nWrote {len(matched):,} matched contacts -> {OUTPUT_FILE.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
