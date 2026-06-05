"""County context enrichment — Census ACS + USDA persistent poverty.

Pulls American Community Survey 5-year estimates for every county in CU's
seven primacy states, then joins them with an optional persistent poverty
county (PPC) list. Writes a single small Parquet that the dashboard
loads at runtime — no API calls in production.

Usage:
    # 1. Get a free Census API key: https://api.census.gov/data/key_signup.html
    # 2. Add it to a local .env file or set the env var:
    export CENSUS_API_KEY=your_key_here
    python enrich.py

Output:
    data/county_context.parquet
        fips5, state, county_display,
        total_population, poverty_rate, median_hh_income,
        high_poverty_current (rate >= 20%),
        persistent_poverty (from USDA list, if available)

PPC list source (optional):
    Drop a CSV at data/persistent_poverty_counties.csv with at minimum a
    column named 'fips' containing 5-digit county FIPS codes. The USDA ERS
    "County Economic Types" data product is the canonical source:
    https://www.ers.usda.gov/data-products/county-economic-types/
"""

from __future__ import annotations

import csv
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
PPC_FILE = DATA_DIR / "persistent_poverty_counties.csv"
OUTPUT_FILE = DATA_DIR / "county_context.parquet"

# CU primacy agency -> 2-digit state FIPS prefix
CU_STATES = {
    "AL": "01",
    "AR": "05",
    "LA": "22",
    "MS": "28",
    "OK": "40",
    "TN": "47",
    "TX": "48",
}

# ACS 5-year estimates (most recent vintage as of writing). The Census API
# keeps prior vintages indexed indefinitely, so this URL stays stable.
ACS_VINTAGE = "2022"
ACS_VARS = [
    "NAME",            # "County Name, State"
    "B01003_001E",     # Total population
    "B17001_001E",     # Population for whom poverty status is determined
    "B17001_002E",     # Population below poverty
    "B19013_001E",     # Median household income
]

HIGH_POVERTY_THRESHOLD = 0.20  # 20% — the threshold USDA uses for PPC


@dataclass(frozen=True)
class CountyRow:
    fips5: str
    state: str
    county_display: str
    total_population: int
    poverty_rate: float | None
    median_hh_income: int | None


def _require_key() -> str:
    key = os.environ.get("CENSUS_API_KEY", "").strip()
    if not key:
        print(
            "ERROR: CENSUS_API_KEY environment variable not set.\n"
            "Get a free key in ~30s: https://api.census.gov/data/key_signup.html\n"
            "Then `export CENSUS_API_KEY=...` and rerun.",
            file=sys.stderr,
        )
        sys.exit(1)
    return key


def fetch_acs_for_state(state_fips: str, key: str) -> list[list[str]]:
    """One Census API call per state — returns the raw JSON-as-list payload."""
    url = (
        f"https://api.census.gov/data/{ACS_VINTAGE}/acs/acs5"
        f"?get={','.join(ACS_VARS)}"
        f"&for=county:*"
        f"&in=state:{state_fips}"
        f"&key={key}"
    )
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return r.json()


def parse_acs(state: str, state_fips: str, payload: list[list[str]]) -> list[CountyRow]:
    header, *rows = payload
    idx = {name: header.index(name) for name in header}
    out: list[CountyRow] = []
    for row in rows:
        total_pop = _to_int(row[idx["B01003_001E"]])
        pov_denom = _to_int(row[idx["B17001_001E"]])
        pov_count = _to_int(row[idx["B17001_002E"]])
        med_hh = _to_int(row[idx["B19013_001E"]])
        county_fips = row[idx["county"]]
        # Census "NAME" reads like "Pulaski County, Arkansas"
        full_name = row[idx["NAME"]]
        display = full_name.split(",")[0].strip()
        out.append(
            CountyRow(
                fips5=f"{state_fips}{county_fips}",
                state=state,
                county_display=display,
                total_population=total_pop or 0,
                poverty_rate=(pov_count / pov_denom) if (pov_denom and pov_count is not None) else None,
                median_hh_income=med_hh,
            )
        )
    return out


def _to_int(val: str | None) -> int | None:
    if val is None or val == "" or val == "null":
        return None
    try:
        n = int(val)
    except (TypeError, ValueError):
        return None
    # Census uses negative sentinels for suppressed estimates
    return n if n >= 0 else None


def load_persistent_poverty_fips() -> set[str]:
    """Read the optional PPC CSV. Returns empty set if file is missing."""
    if not PPC_FILE.exists():
        print(f"  (no PPC list found at {PPC_FILE.name} — persistent_poverty column will be False everywhere)")
        return set()
    fips: set[str] = set()
    with PPC_FILE.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        col = None
        for candidate in ("fips", "FIPS", "fips5", "FIPS5", "county_fips"):
            if candidate in (reader.fieldnames or []):
                col = candidate
                break
        if not col:
            print(f"  WARNING: {PPC_FILE.name} has no recognizable FIPS column. Skipping PPC join.")
            return set()
        for row in reader:
            raw = (row.get(col) or "").strip()
            if not raw:
                continue
            # Accept both "5083" (Arkansas:Hot Spring) and "05083"
            padded = raw.zfill(5)
            if len(padded) == 5 and padded.isdigit():
                fips.add(padded)
    print(f"  loaded {len(fips):,} persistent-poverty county FIPS from {PPC_FILE.name}")
    return fips


def main() -> None:
    key = _require_key()
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Fetching ACS {ACS_VINTAGE} 5-year estimates for {len(CU_STATES)} states...")
    all_rows: list[CountyRow] = []
    for state, fips in CU_STATES.items():
        print(f"  {state} (FIPS {fips})...", end=" ", flush=True)
        payload = fetch_acs_for_state(fips, key)
        rows = parse_acs(state, fips, payload)
        all_rows.extend(rows)
        print(f"{len(rows)} counties")
        time.sleep(0.2)

    df = pd.DataFrame(
        [
            {
                "fips5": r.fips5,
                "state": r.state,
                "county_display": r.county_display,
                "total_population": r.total_population,
                "poverty_rate": r.poverty_rate,
                "median_hh_income": r.median_hh_income,
            }
            for r in all_rows
        ]
    )
    df["high_poverty_current"] = (
        df["poverty_rate"].fillna(0) >= HIGH_POVERTY_THRESHOLD
    )

    print("Joining persistent-poverty designation...")
    ppc = load_persistent_poverty_fips()
    df["persistent_poverty"] = df["fips5"].isin(ppc)

    df.to_parquet(OUTPUT_FILE, index=False)
    print(f"\nWrote {len(df):,} county rows -> {OUTPUT_FILE.relative_to(ROOT)}")
    print(
        f"  High-poverty (current ACS, rate >= {HIGH_POVERTY_THRESHOLD:.0%}): "
        f"{int(df['high_poverty_current'].sum()):,}"
    )
    print(
        f"  Persistent-poverty (USDA list): "
        f"{int(df['persistent_poverty'].sum()):,}"
    )


if __name__ == "__main__":
    main()
