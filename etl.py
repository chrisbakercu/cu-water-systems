"""EPA Envirofacts SDWIS extractor.

Pulls the tables defined in the spec for one or more CU primacy agencies,
writes Parquet files to ``data/<STATE>/<table>.parquet``, and records a
small manifest with row counts and timestamps.

Usage:
    python etl.py --states LA
    python etl.py --states AR AL LA MS OK TN TX
    python etl.py --states LA --tables water_systems violations
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

BASE = "https://data.epa.gov/efservice"
DATA_DIR = Path(__file__).parent / "data"
BATCH = 10_000
SLEEP = 0.3
TIMEOUT = 120
RETRIES = 3

CU_STATES = ["AR", "AL", "LA", "MS", "OK", "TN", "TX"]


@dataclass(frozen=True)
class Table:
    key: str
    api_name: str
    where: str = ""  # extra filter appended after PRIMACY_AGENCY_CODE/<state>

    def url(self, state: str, start: int, end: int) -> str:
        parts = [BASE, self.api_name, "PRIMACY_AGENCY_CODE", state]
        if self.where:
            parts.append(self.where.strip("/"))
        parts += ["ROWS", f"{start}:{end}", "JSON"]
        return "/".join(parts)

    def count_url(self, state: str) -> str:
        parts = [BASE, self.api_name, "PRIMACY_AGENCY_CODE", state]
        if self.where:
            parts.append(self.where.strip("/"))
        parts += ["COUNT", "JSON"]
        return "/".join(parts)


TABLES: dict[str, Table] = {
    "water_systems": Table("water_systems", "WATER_SYSTEM"),
    "geo":           Table("geo", "GEOGRAPHIC_AREA"),
    "violations":    Table("violations", "VIOLATION"),
    "lcr_samples":   Table("lcr_samples", "LCR_SAMPLE"),
    # ENFORCEMENT_ACTION is excluded by default — ~2M rows per state because
    # EPA logs every reminder letter. Add it back via --tables when a
    # filter on enforcement_action_type_code is in place.
    "enforcement":   Table("enforcement", "ENFORCEMENT_ACTION"),
}

DEFAULT_TABLES = ["water_systems", "geo", "violations", "lcr_samples"]


def _get_with_retry(url: str) -> list[dict]:
    last_err: Exception | None = None
    for attempt in range(RETRIES):
        try:
            r = requests.get(url, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last_err = e
            wait = 2 ** attempt
            print(f"    retry {attempt + 1}/{RETRIES} after {wait}s: {e}")
            time.sleep(wait)
    raise RuntimeError(f"failed after {RETRIES} attempts: {url}") from last_err


def fetch_count(table: Table, state: str) -> int:
    data = _get_with_retry(table.count_url(state))
    if not data:
        return 0
    return int(data[0].get("TOTALQUERYRESULTS", 0))


def fetch_all(table: Table, state: str, expected: int | None = None) -> pd.DataFrame:
    rows: list[dict] = []
    start = 0
    while True:
        end = start + BATCH - 1
        chunk = _get_with_retry(table.url(state, start, end))
        if not chunk:
            break
        rows.extend(chunk)
        got = len(chunk)
        if expected:
            print(f"    {start:>7,}-{start + got - 1:>7,} of ~{expected:,}")
        else:
            print(f"    {start:>7,}-{start + got - 1:>7,}")
        if got < BATCH:
            break
        start += BATCH
        time.sleep(SLEEP)
    return pd.DataFrame(rows)


def pull_state(state: str, tables: list[str], outdir: Path) -> dict:
    outdir.mkdir(parents=True, exist_ok=True)
    summary: dict[str, int] = {}
    for key in tables:
        table = TABLES[key]
        print(f"  {state} · {key} ({table.api_name})")
        try:
            count = fetch_count(table, state)
        except Exception as e:  # noqa: BLE001
            print(f"    count failed: {e} — skipping")
            continue
        print(f"    expected rows: {count:,}")
        if count == 0:
            summary[key] = 0
            continue
        df = fetch_all(table, state, expected=count)
        # Envirofacts returns lowercase column names; normalize anyway.
        df.columns = [c.lower() for c in df.columns]
        out = outdir / f"{key}.parquet"
        df.to_parquet(out, index=False)
        summary[key] = len(df)
        print(f"    wrote {len(df):,} rows -> {out.relative_to(DATA_DIR.parent)}")
    return summary


def write_manifest(state: str, summary: dict[str, int], outdir: Path) -> None:
    path = outdir / "manifest.json"
    existing = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text()).get("row_counts", {})
        except Exception:  # noqa: BLE001
            pass
    merged = {**existing, **summary}
    manifest = {
        "state": state,
        "pulled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "row_counts": merged,
    }
    path.write_text(json.dumps(manifest, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--states", nargs="+", default=["LA"], help="Primacy agency codes")
    parser.add_argument(
        "--tables", nargs="+", default=DEFAULT_TABLES, choices=list(TABLES.keys())
    )
    parser.add_argument(
        "--data-dir", default=str(DATA_DIR), help="Output directory for Parquet"
    )
    args = parser.parse_args()

    root = Path(args.data_dir)
    print(f"output: {root}")
    for state in args.states:
        print(f"\n== {state} ==")
        outdir = root / state
        summary = pull_state(state, args.tables, outdir)
        write_manifest(state, summary, outdir)
        print(f"  manifest: {summary}")


if __name__ == "__main__":
    main()
