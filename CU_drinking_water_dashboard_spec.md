# CU Drinking Water Dashboard — Tech Spec

Data source for all 7 CU states: **EPA Envirofacts SDWIS REST API**. No auth, no rate-limit headers, refreshed quarterly. Skip scraping LDH and the equivalent state viewers — they're captcha-gated Angular SPAs and lag the federal feed by less than they're worth fighting.

## API basics

```
GET https://data.epa.gov/efservice/<TABLE>/<COL>/<VALUE>/[<COL>/<VALUE>/...]/ROWS/<start>:<end>/JSON
```

- Output formats: append `JSON`, `CSV`, `EXCEL`, `XML`, `PARQUET`, `JSONP`, `PDF`, `HTML`.
- Append `/COUNT/JSON` instead of `/ROWS/...` to get just a row count — use this before paging.
- Pagination: `ROWS/0:9999` returns 10,000 rows max per call. Loop, incrementing `start` by batch size, until you get back fewer rows than you asked for.
- Filter chains: just keep adding `/COL/VALUE/` pairs. They AND together.
- Strings are case-sensitive in filters; codes are uppercase.
- No throttling I hit, but be polite (200–500 ms between calls).

## CU state codes

Filter every table by `PRIMACY_AGENCY_CODE/<XX>`:

```
AR  AL  LA  MS  OK  TN  TX
```

For tribal systems inside CU's footprint, also pull `PRIMACY_AGENCY_CODE/06` (EPA Region 6 direct implementation) and `PRIMACY_AGENCY_CODE/04` for Region 4 tribes — filter further by `STATE_SERVED` or by joining geographic_area.

## Tables that matter

Primary key on almost everything is `pwsid` (Public Water System ID, e.g. `LA1001001`).

| Table | What's in it | Per-state row count (LA) | Use for |
|---|---|---|---|
| `WATER_SYSTEM` | Name, type, source, population served, connections, owner type, contact, activity status | 5,160 | Master inventory |
| `GEOGRAPHIC_AREA` | Parish/county/city/zip served per system | 5,137 | Mapping, parish rollup |
| `SERVICE_AREA` | Service area type codes | ~same | Residential/commercial mix |
| `VIOLATION` | Every violation: type, contaminant, period, health-based flag, return-to-compliance | 36,745 | Compliance scorecard |
| `ENFORCEMENT_ACTION` | State + EPA actions, action type, date | very large — page hard | Escalation timeline |
| `LCR_SAMPLE` | Lead & copper sample results | 4,403 | LSLR planning |
| `SITE_VISIT` | Sanitary surveys | varies | Inspection history |
| `TREATMENT` | Treatment processes per facility | varies | Source resilience |
| `FACILITY` | Wells, intakes, storage, etc. | varies | Asset inventory |
| `EVENT_MILESTONE` | Compliance milestones | varies | Workflow status |

The viewer's "Service Line Inventory" is **not** in the federal feed yet for most states. If you need it, the cleanest path is sanctioned per-state exports (LDH has the "Export to Excel" button) or a state-by-state FOIA/data request.

## Key filters

- Active community water systems: `PWS_ACTIVITY_CODE/A/PWS_TYPE_CODE/CWS`
- Small systems (<3,300 served, where CU usually plays): post-filter on `population_served_count`
- Health-based violations only: `IS_HEALTH_BASED_IND/Y` on `VIOLATION`
- Open violations: `VIOL_STATUS/Unaddressed` or `Returned to Compliance` (compare)
- Recent: filter on `COMPL_PER_BEGIN_DATE` (string YYYY-MM-DD; Envirofacts supports `>`, `<` operators in the URL — see API docs)

## Joins

```
WATER_SYSTEM.pwsid  ⟶  GEOGRAPHIC_AREA.pwsid     (1:many, parish)
WATER_SYSTEM.pwsid  ⟶  VIOLATION.pwsid           (1:many)
VIOLATION.pwsid + viol_id  ⟶  ENFORCEMENT_ACTION (1:many)
WATER_SYSTEM.pwsid  ⟶  LCR_SAMPLE.pwsid          (1:many)
```

## Architecture for the 7-state dashboard

```
┌──────────────────────────────────────────────┐
│  Quarterly ETL (Python + requests + DuckDB)  │
│  Pulls all 7 states for all key tables       │
│  Writes Parquet files to S3 / local          │
└──────────────────┬───────────────────────────┘
                   │
                   ▼
┌──────────────────────────────────────────────┐
│  DuckDB / Postgres warehouse                 │
│   - water_systems  (joined w/ parish)        │
│   - violations     (incremental)             │
│   - lcr_samples                              │
│   - enforcement                              │
│   - cu_watchlist  (materialized view)        │
└──────────────────┬───────────────────────────┘
                   │
                   ▼
┌──────────────────────────────────────────────┐
│  Dashboard layer — pick one:                 │
│   • Streamlit / Dash (Python, fastest)       │
│   • Metabase / Superset (self-host BI)       │
│   • Observable Framework (static, cheap)     │
│   • Next.js + Recharts (most custom)         │
└──────────────────────────────────────────────┘
```

## Refresh strategy

- Envirofacts updates quarterly. Schedule the full pull monthly anyway — sometimes EPA backfills mid-quarter.
- Incremental loads on `VIOLATION` and `ENFORCEMENT_ACTION` are smart once the warehouse exists; filter on `LAST_REPORTED_DATE > <max we have>`.
- LDH and other state viewers sometimes have data EPA doesn't yet. For violations less than ~120 days old, treat the state viewer as the source of truth and flag it in the UI ("federal feed; may lag state by 1 quarter").

## Suggested dashboard views

1. **State scorecard** — system count, population served, % with active health-based violations, % small systems (<3,300), parishes/counties covered.
2. **CU watchlist** — small systems in CU's persistent-poverty counties with open violations or open enforcement; sort by population × violation severity. This is the daily-driver view.
3. **System detail** — every public field for a single PWS, with full violation/enforcement timeline. Link out to the state viewer for sample-level data.
4. **Lead & Copper** — 90th-percentile trend per system, flag any above 15 ppb action level.
5. **Map** — choropleth at parish/county level; click drills into systems in that county.
6. **What's new** — diff of last pull vs. previous pull. New violations, new enforcement, new systems, deactivated systems.

## Code skeleton (Python)

```python
import requests, time, pandas as pd
from pathlib import Path

BASE = "https://data.epa.gov/efservice"
CU_STATES = ["AR", "AL", "LA", "MS", "OK", "TN", "TX"]

def fetch_all(table: str, where: str, batch: int = 10000) -> list[dict]:
    rows, start = [], 0
    while True:
        url = f"{BASE}/{table}/{where}/ROWS/{start}:{start+batch-1}/JSON"
        r = requests.get(url, timeout=60); r.raise_for_status()
        chunk = r.json()
        if not chunk: break
        rows.extend(chunk)
        if len(chunk) < batch: break
        start += batch
        time.sleep(0.3)
    return rows

def pull_state(state: str, outdir: Path):
    outdir.mkdir(parents=True, exist_ok=True)
    tables = {
        "water_systems":  ("WATER_SYSTEM",       f"PRIMACY_AGENCY_CODE/{state}"),
        "geo":            ("GEOGRAPHIC_AREA",    f"PRIMACY_AGENCY_CODE/{state}"),
        "violations":     ("VIOLATION",          f"PRIMACY_AGENCY_CODE/{state}"),
        "enforcement":    ("ENFORCEMENT_ACTION", f"PRIMACY_AGENCY_CODE/{state}"),
        "lcr_samples":    ("LCR_SAMPLE",         f"PRIMACY_AGENCY_CODE/{state}"),
    }
    for name, (tbl, where) in tables.items():
        df = pd.DataFrame(fetch_all(tbl, where))
        df.to_parquet(outdir / f"{state}_{name}.parquet", index=False)
        print(f"{state} {name}: {len(df):,}")

for s in CU_STATES:
    pull_state(s, Path("data") / s)
```

## Gotchas

- `pwsid` prefixes by state but the `PRIMACY_AGENCY_CODE` filter is what you actually want — some systems are physically in one state but regulated by another, and tribal systems are coded by EPA region.
- `pws_activity_code`: `A` = active, `I` = inactive, `N` = changed-from-public, `M` = merged, `P` = pending. Default to `A` unless you're doing historical analysis.
- `pws_type_code`: `CWS` = community, `NTNCWS` = non-transient non-community (schools, factories), `TNCWS` = transient non-community (campgrounds, gas stations). CU's work is mostly CWS but NTNCWS schools/daycares matter too.
- Population counts are self-reported and can be years stale.
- "Enforcement action" rows balloon because EPA logs every reminder letter as an action. Filter on `enforcement_action_type_code` if you want only formal actions.
- Contact emails in the feed are real — treat them as PII for any rate/billing context per CU's confidentiality rules.

## Reference

- API root: https://data.epa.gov/efservice/
- Metadata: https://enviro.epa.gov/enviro/ef_metadata_html.all_other_data?p_topic=SDWIS
- Quarterly bulk download (CSV zip): https://echo.epa.gov/tools/data-downloads/sdwa-download-summary
- Data element dictionary: https://echo.epa.gov/tools/data-downloads/sdwa-download-summary (link to PDF on that page)
