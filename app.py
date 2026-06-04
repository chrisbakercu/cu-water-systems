"""CU Drinking Water Dashboard — Louisiana prototype.

Reads the LA sample xlsx and renders the scorecard, watchlist, system detail,
lead & copper, and violations views from the spec. Designed so the data loader
can later be swapped for a DuckDB / Parquet warehouse fed by the quarterly ETL.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

ROOT = Path(__file__).parent
PARQUET_DIR = ROOT / "data"
FIPS_FILE = PARQUET_DIR / "national_county2020.txt"
GEOJSON_FILE = PARQUET_DIR / "us_counties_fips.geojson"
SMALL_SYSTEM_THRESHOLD = 3300
LCR_ACTION_LEVEL_PPB = 15

PARQUET_FILES = ["water_systems", "violations", "lcr_samples", "geo"]

# Census spells some county names differently from the EPA feed.
COUNTY_NAME_ALIASES = {
    ("LA", "la salle"): "lasalle",
}

OWNER_TYPE_LABELS = {
    "L": "Local government",
    "P": "Private",
    "F": "Federal",
    "S": "State",
    "M": "Mixed public/private",
    "N": "Native American",
}
SOURCE_LABELS = {
    "GW": "Groundwater",
    "SW": "Surface water",
    "GU": "Groundwater under SW influence",
    "GWP": "Purchased groundwater",
    "SWP": "Purchased surface water",
}
TYPE_LABELS = {
    "CWS": "Community",
    "NTNCWS": "Non-transient non-community",
    "TNCWS": "Transient non-community",
}

st.set_page_config(
    page_title="CU Water Systems",
    page_icon="💧",
    layout="wide",
)

# --- CU brand ---------------------------------------------------------------
CU_PRIMARY = "#085eaa"
CU_SECONDARY = "#66b1e2"
CU_TERTIARY = "#0088ce"
CU_GRAY = "#888b8d"
CU_BLUE_SCALE = ["#f4f7fb", "#66b1e2", "#0088ce", "#085eaa"]
CU_QUALITATIVE = [CU_PRIMARY, CU_TERTIARY, CU_SECONDARY, CU_GRAY, "#1f2933"]

px.defaults.color_discrete_sequence = CU_QUALITATIVE
px.defaults.color_continuous_scale = CU_BLUE_SCALE
px.defaults.template = "plotly_white"


def render_system_detail(pwsid: str, systems_df, violations_df, lcr_df) -> None:
    """Render the full system detail block — used by Find a System tab + modal."""
    matches = systems_df[systems_df["pwsid"] == pwsid]
    if matches.empty:
        st.warning(f"No record found for {pwsid}.")
        return
    row = matches.iloc[0]

    st.markdown(
        f"### {row['pws_name']}  \n"
        f"<span style='color:#888b8d;font-size:0.9rem;'>"
        f"{row['pwsid']} · {row.get('city_name', '')}, "
        f"{row.get('state_code', '')}</span>",
        unsafe_allow_html=True,
    )

    admin = row.get("admin_name") or "—"
    org = row.get("org_name") or "—"
    email = row.get("email_addr") or ""
    phone = row.get("phone_number") or ""
    alt_phone = row.get("alt_phone_number") or ""

    email_html = (
        f"<a href='mailto:{email}' style='color:#085eaa;text-decoration:none;'>{email}</a>"
        if email and email != "—"
        else "<span style='color:#888b8d;'>Not on file</span>"
    )
    phone_html = (
        f"<a href='tel:{phone}' style='color:#085eaa;text-decoration:none;'>{phone}</a>"
        if phone and phone != "—"
        else "<span style='color:#888b8d;'>Not on file</span>"
    )
    alt_phone_html = (
        f" · <span style='color:#888b8d;'>alt</span> "
        f"<a href='tel:{alt_phone}' style='color:#085eaa;text-decoration:none;'>{alt_phone}</a>"
        if alt_phone else ""
    )

    st.markdown(
        f"""
        <div style='background:#ffffff;border:1px solid #d8e2ee;border-left:4px solid #085eaa;
            border-radius:8px;padding:1.25rem 1.5rem;margin:0.5rem 0 1.25rem 0;
            box-shadow:0 1px 2px rgba(8,94,170,0.05);'>
          <div style='font-size:0.75rem;font-weight:600;letter-spacing:0.08em;
              color:#888b8d;text-transform:uppercase;margin-bottom:0.5rem;'>Operator contact</div>
          <div style='font-size:1.15rem;font-weight:600;color:#1f2933;'>{admin}</div>
          <div style='color:#888b8d;margin-bottom:0.85rem;'>{org}</div>
          <div style='display:flex;flex-wrap:wrap;gap:1.5rem;font-size:0.95rem;'>
            <div><div style='color:#888b8d;font-size:0.8rem;'>Email</div><div>{email_html}</div></div>
            <div><div style='color:#888b8d;font-size:0.8rem;'>Phone</div><div>{phone_html}{alt_phone_html}</div></div>
          </div>
          <div style='margin-top:0.85rem;padding-top:0.75rem;border-top:1px solid #f0f2f5;
              font-size:0.75rem;color:#888b8d;'>PII — handle per CU confidentiality rules.</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    addr = (row.get("address_line1") or "").strip()
    addr_full = (f"{addr}  \n" if addr else "") + (
        f"{row.get('city_name', '')}, {row.get('state_code', '')} {row.get('zip_code', '')}"
    )
    st.markdown("**Mailing address**")
    st.write(addr_full)

    c1, c2, c3 = st.columns(3)
    c1.metric("Population served", f"{int(row['population_served_count'] or 0):,}")
    c2.metric("Connections", f"{int(row['service_connections_count'] or 0):,}")
    c3.metric("System type", TYPE_LABELS.get(row["pws_type_code"], row["pws_type_code"]))

    c4, c5, c6 = st.columns(3)
    c4.metric("Source", SOURCE_LABELS.get(row["primary_source_code"], row["primary_source_code"]))
    c5.metric("Owner", OWNER_TYPE_LABELS.get(row["owner_type_code"], row["owner_type_code"]))
    c6.metric("Status", "Active" if row["pws_activity_code"] == "A" else "Inactive")

    st.markdown("**Violation history**")
    v = violations_df[violations_df["pwsid"] == pwsid].copy()
    if v.empty:
        st.info("No violations in the federal feed.")
    else:
        v["status"] = v["rtc_date"].apply(
            lambda d: "Returned to compliance" if pd.notna(d) else "Open"
        )
        v_display = v[[
            "compl_per_begin_date", "violation_code", "violation_category_code",
            "is_health_based_ind", "contaminant_code", "status", "rtc_date",
        ]].sort_values("compl_per_begin_date", ascending=False)
        v_display.columns = ["Begin", "Code", "Category", "Health-based",
                             "Contaminant", "Status", "RTC date"]
        st.dataframe(v_display, width="stretch", hide_index=True)

    st.markdown("**Lead & copper samples**")
    l = lcr_df[lcr_df["pwsid"] == pwsid]
    if l.empty:
        st.info("No LCR samples in the federal feed.")
    else:
        st.dataframe(
            l[["sample_id", "sampling_start_date", "sampling_end_date"]],
            width="stretch",
            hide_index=True,
        )


def section(title: str, subtitle: str | None = None) -> None:
    """Consistent section heading: H3 in brand color + optional muted subtitle."""
    st.markdown(
        f"""
        <div style='margin: 0.75rem 0 0.5rem 0;'>
          <div style='
              font-size:1.05rem;
              font-weight:600;
              color:#085eaa;
              letter-spacing:-0.005em;
          '>{title}</div>
          {f"<div style='color:#6b7280;font-size:0.85rem;margin-top:0.15rem;'>{subtitle}</div>" if subtitle else ""}
        </div>
        """,
        unsafe_allow_html=True,
    )

# --- Password gate (only enforced when secrets define a password) -----------
def _password_gate() -> None:
    expected = ""
    try:
        expected = st.secrets.get("password", "")
    except Exception:
        expected = ""
    if not expected:
        return  # No password configured — local / open access
    if st.session_state.get("auth_ok"):
        return

    # Self-contained styling — main brand CSS hasn't run yet at this point.
    st.markdown(
        """
        <style>
          @import url('https://fonts.googleapis.com/css2?family=Poppins:wght@400;500;600;700&display=swap');
          html, body, .stApp { font-family: 'Poppins', system-ui, sans-serif !important; background: #f7f9fc !important; }
          [data-testid="stHeader"], #MainMenu, footer { visibility: hidden; }
          .block-container { padding-top: 4rem !important; max-width: 480px !important; }
          .login-card {
            background: #ffffff;
            border: 1px solid #e5eaf2;
            border-radius: 12px;
            padding: 2rem 2.25rem 1.5rem 2.25rem;
            box-shadow: 0 6px 24px rgba(8, 94, 170, 0.08);
          }
          .login-eyebrow {
            font-size: 0.72rem;
            font-weight: 600;
            letter-spacing: 0.16em;
            color: #0088ce;
            text-transform: uppercase;
            margin-bottom: 0.25rem;
          }
          .login-title {
            font-family: 'Poppins', sans-serif;
            font-size: 1.5rem;
            font-weight: 600;
            color: #085eaa;
            margin-bottom: 0.35rem;
          }
          .login-help {
            color: #6b7280;
            font-size: 0.9rem;
            margin-bottom: 1.25rem;
          }
          /* Big, obvious password input */
          .stTextInput input {
            font-family: 'Poppins', sans-serif !important;
            font-size: 1.05rem !important;
            padding: 0.85rem 1rem !important;
            border-radius: 8px !important;
            border: 1.5px solid #d8e2ee !important;
            background: #f7f9fc !important;
          }
          .stTextInput input:focus {
            border-color: #085eaa !important;
            background: #ffffff !important;
            box-shadow: 0 0 0 3px rgba(8, 94, 170, 0.12) !important;
          }
          .stTextInput label {
            font-family: 'Poppins', sans-serif !important;
            font-size: 0.75rem !important;
            font-weight: 600 !important;
            text-transform: uppercase !important;
            letter-spacing: 0.08em !important;
            color: #085eaa !important;
          }
          .stFormSubmitButton button {
            font-family: 'Poppins', sans-serif !important;
            background: #085eaa !important;
            color: #ffffff !important;
            border: none !important;
            border-radius: 8px !important;
            padding: 0.75rem 1.25rem !important;
            font-size: 1rem !important;
            font-weight: 500 !important;
            width: 100% !important;
            margin-top: 0.5rem !important;
          }
          .stFormSubmitButton button:hover {
            background: #0088ce !important;
          }
        </style>
        """,
        unsafe_allow_html=True,
    )
    with st.container(border=True):
        st.markdown(
            """
            <div class='login-eyebrow'>Communities Unlimited</div>
            <div class='login-title'>Water Systems</div>
            <div class='login-help'>
              This dashboard is access-controlled. Enter the password your CU
              contact shared with you to continue.
            </div>
            """,
            unsafe_allow_html=True,
        )
        with st.form("login", clear_on_submit=True):
            pw = st.text_input(
                "Password",
                type="password",
                placeholder="Type the access password here",
                label_visibility="visible",
            )
            submitted = st.form_submit_button("Sign in")
    if submitted:
        if pw == expected:
            st.session_state["auth_ok"] = True
            st.rerun()
        else:
            st.error("That password didn't match. Try again.")
    st.stop()


_password_gate()

st.markdown(
    """
    <style>
      @import url('https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;500;600;700&display=swap');

      /* ---------- Typography ---------- */
      html, body, .stApp,
      .stMarkdown, .stMetric, .stDataFrame, .stSelectbox, .stMultiSelect,
      .stRadio, .stCheckbox, .stButton button, h1, h2, h3, h4, h5, h6,
      [data-testid="stMetricLabel"], [data-testid="stMetricValue"],
      [data-testid="stMetricDelta"], [data-testid="stCaptionContainer"],
      [data-baseweb="select"] *, [data-baseweb="input"] *,
      [data-baseweb="tab"], [data-testid="stSidebar"] * {
        font-family: 'Poppins', system-ui, sans-serif !important;
      }
      /* Preserve Material Symbols icon glyphs */
      [class*="material-symbols"], [class*="material-icons"],
      .material-symbols-outlined, .material-symbols-rounded,
      .material-icons, span[role="img"][class*="icon"],
      [data-baseweb="icon"], [data-baseweb="icon"] *,
      [data-testid="stIcon"], [data-testid="stIcon"] *,
      [data-testid="stIconMaterial"], [data-testid="stIconMaterial"] *,
      [data-testid="stExpanderIcon"], [data-testid="stExpanderIcon"] *,
      span[translate="no"] {
        font-family: 'Material Symbols Outlined', 'Material Symbols Rounded',
                     'Material Icons' !important;
      }
      body, .stApp { color: #1f2933; }
      h1, h2, h3 { color: #085eaa; font-weight: 600; letter-spacing: -0.01em; }
      h2 { font-size: 1.35rem; margin-top: 0.5rem; }
      h3 { font-size: 1.1rem; }
      [data-testid="stCaptionContainer"], .stCaption { color: #6b7280 !important; }
      hr { border-color: #e5eaf2 !important; }

      /* ---------- App container ---------- */
      .stApp { background: #f7f9fc; }
      .block-container { padding-top: 1.5rem; padding-bottom: 3rem; max-width: 1400px; }
      /* Hide Streamlit's hamburger menu + footer for a cleaner internal-app
         look. Keep the toolbar visible — it hosts the sidebar expand button. */
      #MainMenu, footer { visibility: hidden; }
      [data-testid="stToolbar"] [data-testid="stMainMenu"] { display: none; }

      /* Make the sidebar expand/collapse arrow obviously a button */
      [data-testid="stHeader"] button,
      [data-testid="stSidebarHeader"] button {
        background: #eaf2fa !important;
        border: 1px solid #d8e2ee !important;
        border-radius: 8px !important;
        width: 36px !important;
        height: 36px !important;
        color: #085eaa !important;
        visibility: visible !important;
        opacity: 1 !important;
        transition: background 0.15s, border-color 0.15s !important;
      }
      [data-testid="stHeader"] button:hover,
      [data-testid="stSidebarHeader"] button:hover {
        background: #085eaa !important;
        border-color: #085eaa !important;
        color: #ffffff !important;
      }
      [data-testid="stHeader"] button [data-testid="stIconMaterial"],
      [data-testid="stSidebarHeader"] button [data-testid="stIconMaterial"] {
        font-size: 20px !important;
        color: inherit !important;
      }

      /* ---------- Tabs (pill-style) ---------- */
      .stTabs [data-baseweb="tab-list"] {
        gap: 0.25rem;
        background: transparent;
        border-bottom: 1px solid #e5eaf2;
        padding-bottom: 0.25rem;
        margin-bottom: 1rem;
      }
      .stTabs [data-baseweb="tab"] {
        padding: 0.55rem 1rem !important;
        background: transparent;
        border-radius: 6px 6px 0 0;
        color: #52606d;
        font-weight: 500;
        transition: color 0.15s, background 0.15s;
      }
      .stTabs [data-baseweb="tab"]:hover { color: #085eaa; background: #eef3f9; }
      .stTabs [aria-selected="true"] {
        color: #085eaa !important;
        font-weight: 600;
        background: transparent;
      }
      .stTabs [data-baseweb="tab-highlight"] {
        background-color: #085eaa !important;
        height: 3px;
        border-radius: 2px;
      }

      /* ---------- Metric tiles as cards ---------- */
      [data-testid="stMetric"] {
        background: #ffffff;
        border: 1px solid #e5eaf2;
        border-radius: 10px;
        padding: 1rem 1.15rem;
        box-shadow: 0 1px 2px rgba(8,94,170,0.04);
        transition: box-shadow 0.15s, border-color 0.15s;
      }
      [data-testid="stMetric"]:hover {
        box-shadow: 0 4px 12px rgba(8,94,170,0.08);
        border-color: #d8e2ee;
      }
      [data-testid="stMetricLabel"] {
        color: #6b7280 !important;
        font-size: 0.75rem !important;
        font-weight: 600 !important;
        text-transform: uppercase;
        letter-spacing: 0.06em;
      }
      [data-testid="stMetricValue"] {
        color: #085eaa;
        font-weight: 600;
        font-size: 1.65rem !important;
        line-height: 1.1;
      }
      [data-testid="stMetricDelta"] { font-size: 0.8rem !important; }

      /* ---------- Sidebar ---------- */
      [data-testid="stSidebar"] {
        background: #ffffff;
        border-right: 1px solid #e5eaf2;
      }
      [data-testid="stSidebar"] > div { padding-top: 1.25rem; }
      [data-testid="stSidebar"] h2,
      [data-testid="stSidebar"] h3 {
        color: #085eaa;
        font-size: 0.75rem !important;
        font-weight: 600 !important;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        margin-bottom: 0.5rem;
      }
      [data-testid="stSidebar"] [data-testid="stCaptionContainer"] {
        color: #888b8d !important;
        font-size: 0.78rem !important;
      }
      [data-testid="stSidebar"] hr { margin: 1.25rem 0; }

      /* ---------- Inputs (selectbox, multiselect, text) ---------- */
      [data-baseweb="select"] > div, [data-baseweb="input"] > div {
        border-radius: 8px !important;
        border-color: #d8e2ee !important;
      }
      [data-baseweb="select"]:hover > div, [data-baseweb="input"]:hover > div {
        border-color: #0088ce !important;
      }
      [data-baseweb="tag"] {
        background: #eaf2fa !important;
        color: #085eaa !important;
        border-radius: 6px !important;
      }

      /* ---------- Buttons ---------- */
      .stButton button, .stFormSubmitButton button {
        border-radius: 8px !important;
        font-weight: 500 !important;
        padding: 0.45rem 1rem !important;
        border: 1px solid #d8e2ee !important;
        transition: background 0.15s, border-color 0.15s;
      }
      .stButton button[kind="primary"], .stFormSubmitButton button {
        background: #085eaa !important;
        color: white !important;
        border-color: #085eaa !important;
      }
      .stButton button[kind="primary"]:hover, .stFormSubmitButton button:hover {
        background: #0088ce !important;
        border-color: #0088ce !important;
      }

      /* ---------- Bordered containers (cards) ---------- */
      [data-testid="stVerticalBlockBorderWrapper"] {
        border-color: #e5eaf2 !important;
        border-radius: 10px !important;
        background: #ffffff;
        box-shadow: 0 1px 2px rgba(8,94,170,0.04);
      }

      /* ---------- Expanders ---------- */
      [data-testid="stExpander"] {
        border: 1px solid #e5eaf2 !important;
        border-radius: 8px !important;
        background: #ffffff;
      }
      [data-testid="stExpander"] summary { font-weight: 500; }

      /* ---------- DataFrames / tables ---------- */
      .stDataFrame { border-radius: 8px; overflow: hidden; }
      .stDataFrame [data-testid="StyledDataFrameRowHeader"],
      .stDataFrame thead tr th {
        background: #eef3f9 !important;
        color: #085eaa !important;
        font-weight: 600 !important;
        border-bottom: 1px solid #d8e2ee !important;
      }
      .stDataFrame tbody tr:hover { background: #f7f9fc !important; }

      /* ---------- Plotly chart containers ---------- */
      [data-testid="stPlotlyChart"] {
        background: #ffffff;
        border: 1px solid #e5eaf2;
        border-radius: 10px;
        padding: 0;
        box-shadow: 0 1px 2px rgba(8,94,170,0.04);
        overflow: hidden;
      }
      [data-testid="stPlotlyChart"] > div { padding: 0.5rem; }
      /* Streamlit wraps every element in a container with overflow:auto by
         default; that draws a scrollbar whenever our card adds any padding.
         Suppress it — we never need per-element scrolling. */
      [data-testid="stElementContainer"] { overflow: visible !important; }
      [data-testid="stFullScreenFrame"] { overflow: visible !important; }

      /* ---------- Alerts (info / warning / success) ---------- */
      [data-testid="stAlert"] {
        border-radius: 8px !important;
        border: 1px solid #e5eaf2 !important;
      }
    </style>
    """,
    unsafe_allow_html=True,
)


def _available_states() -> list[str]:
    if not PARQUET_DIR.exists():
        return []
    return sorted(
        d.name
        for d in PARQUET_DIR.iterdir()
        if d.is_dir() and all((d / f"{f}.parquet").exists() for f in PARQUET_FILES)
    )


def _coerce_dates(frames: dict[str, pd.DataFrame]) -> None:
    date_cols = {
        "violations": ["compl_per_begin_date", "compl_per_end_date", "rtc_date"],
        "lcr": ["sampling_start_date", "sampling_end_date"],
        "systems": ["pws_deactivation_date"],
    }
    for key, cols in date_cols.items():
        df = frames[key]
        for c in cols:
            if c in df.columns:
                df[c] = pd.to_datetime(df[c], errors="coerce")


@st.cache_data(show_spinner="Loading water system data...")
def load_from_parquet(states: tuple[str, ...]) -> dict[str, pd.DataFrame]:
    name_map = {
        "systems": "water_systems",
        "violations": "violations",
        "lcr": "lcr_samples",
        "geo": "geo",
    }
    frames: dict[str, pd.DataFrame] = {}
    for key, fname in name_map.items():
        parts = [
            pd.read_parquet(PARQUET_DIR / state / f"{fname}.parquet")
            for state in states
        ]
        frames[key] = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    _coerce_dates(frames)
    return frames


@st.cache_data
def load_fips_lookup() -> dict[tuple[str, str], tuple[str, str]]:
    """Map (state, normalized_county_name) -> (fips5, display_name)."""
    if not FIPS_FILE.exists():
        return {}
    fips = pd.read_csv(FIPS_FILE, sep="|", dtype=str)
    lookup: dict[tuple[str, str], tuple[str, str]] = {}
    for _, r in fips.iterrows():
        norm = _normalize_county(r["COUNTYNAME"])
        lookup[(r["STATE"], norm)] = (
            r["STATEFP"] + r["COUNTYFP"],
            r["COUNTYNAME"],
        )
    return lookup


@st.cache_data
def load_counties_geojson() -> dict | None:
    if not GEOJSON_FILE.exists():
        return None
    import json
    return json.loads(GEOJSON_FILE.read_text())


def _normalize_county(name: str) -> str:
    import re
    return re.sub(
        r"\s+(County|Parish|Borough|Census Area|Municipio)$",
        "",
        str(name),
        flags=re.IGNORECASE,
    ).strip().lower()


def explode_geo_to_counties(geo: pd.DataFrame) -> pd.DataFrame:
    """One row per (pwsid, county). Splits comma-separated multi-county strings."""
    g = geo[geo["area_type_code"].isin(["CN", "CN,CT"])].copy()
    g = g[g["county_served"].notna()]
    g = g.assign(
        county_served=g["county_served"].str.split(","),
    ).explode("county_served")
    g["county_served"] = g["county_served"].str.strip()
    g["norm"] = g["county_served"].map(_normalize_county)
    g["norm"] = g.apply(
        lambda r: COUNTY_NAME_ALIASES.get((r["primacy_agency_code"], r["norm"]), r["norm"]),
        axis=1,
    )
    lookup = load_fips_lookup()
    g["fips5"] = g.apply(
        lambda r: (lookup.get((r["primacy_agency_code"], r["norm"])) or (None, None))[0],
        axis=1,
    )
    g["county_display"] = g.apply(
        lambda r: (lookup.get((r["primacy_agency_code"], r["norm"])) or (None, r["county_served"]))[1],
        axis=1,
    )
    return g.dropna(subset=["fips5"])[
        ["pwsid", "primacy_agency_code", "county_served", "county_display", "fips5"]
    ]


@st.cache_data
def load_manifests(states: tuple[str, ...]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for s in states:
        p = PARQUET_DIR / s / "manifest.json"
        if p.exists():
            import json
            out[s] = json.loads(p.read_text())
    return out


def active_cws(systems: pd.DataFrame) -> pd.DataFrame:
    return systems[
        (systems["pws_activity_code"] == "A") & (systems["pws_type_code"] == "CWS")
    ].copy()


def open_violations(violations: pd.DataFrame) -> pd.DataFrame:
    return violations[violations["rtc_date"].isna()].copy()


def label(series: pd.Series, mapping: dict[str, str]) -> pd.Series:
    return series.map(mapping).fillna(series)


# ---------------------------------------------------------------------------
# Header + data source selection
# ---------------------------------------------------------------------------

available = _available_states()

if not available:
    st.error(
        "No data found. Run `python etl.py --states AR AL LA MS OK TN TX` to "
        "populate `data/<STATE>/` with Parquet files."
    )
    st.stop()

with st.sidebar:
    st.header("States")
    for s in available:
        st.session_state.setdefault(f"state_{s}", True)
    selected_states = [s for s in available if st.checkbox(s, key=f"state_{s}")]
    all_on = len(selected_states) == len(available)
    if st.button(
        "Clear all" if all_on else "Select all",
        width="stretch",
    ):
        new_value = not all_on
        for s in available:
            st.session_state[f"state_{s}"] = new_value
        st.rerun()
    if not selected_states:
        st.warning("Pick at least one state.")
        st.stop()
    data = load_from_parquet(tuple(selected_states))
    manifests = load_manifests(tuple(selected_states))
    pulled = sorted({m.get("pulled_at", "") for m in manifests.values()})
    source_label = (
        f"EPA Envirofacts SDWIS · {', '.join(selected_states)} · "
        f"Last pull: {pulled[-1] if pulled else 'unknown'}"
    )

    st.divider()
    st.header("Filters")

systems_all = data["systems"]
violations_all = data["violations"]
lcr_all = data["lcr"]
geo_all = data["geo"]

# --- App header band --------------------------------------------------------
pull_short = (pulled[-1].split("T")[0] if pulled and pulled[-1] else "—")
state_chip = ", ".join(selected_states) if len(selected_states) <= 4 else (
    f"{len(selected_states)} states"
)
st.markdown(
    f"""
    <div style='
        display:flex;
        align-items:flex-end;
        justify-content:space-between;
        padding:0.25rem 0 1rem 0;
        border-bottom:1px solid #e5eaf2;
        margin-bottom:1.25rem;
    '>
      <div>
        <div style='
            font-size:0.72rem;
            font-weight:600;
            letter-spacing:0.14em;
            color:#0088ce;
            text-transform:uppercase;
            margin-bottom:0.15rem;
        '>Communities Unlimited</div>
        <div style='
            font-size:1.75rem;
            font-weight:600;
            color:#085eaa;
            line-height:1.1;
        '>Water Systems</div>
      </div>
      <div style='display:flex;gap:0.6rem;align-items:center;'>
        <span style='
            background:#ffffff;
            border:1px solid #d8e2ee;
            color:#1f2933;
            padding:0.35rem 0.7rem;
            border-radius:999px;
            font-size:0.78rem;
        '>
          <span style='color:#888b8d;'>States</span>
          &nbsp;<span style='font-weight:600;'>{state_chip}</span>
        </span>
        <span style='
            background:#ffffff;
            border:1px solid #d8e2ee;
            color:#1f2933;
            padding:0.35rem 0.7rem;
            border-radius:999px;
            font-size:0.78rem;
        '>
          <span style='color:#888b8d;'>Last pull</span>
          &nbsp;<span style='font-weight:600;'>{pull_short}</span>
        </span>
      </div>
    </div>
    <div style='
        color:#888b8d;
        font-size:0.82rem;
        margin-top:-0.5rem;
        margin-bottom:0.75rem;
    '>EPA Envirofacts SDWIS · Federal feed may lag state viewers by ~1 quarter.</div>
    """,
    unsafe_allow_html=True,
)

with st.sidebar:
    counties = sorted(geo_all["county_served"].dropna().unique().tolist())
    selected_counties = st.multiselect("County / Parish", counties, default=[])
    only_small = st.checkbox(
        f"Small systems only (<{SMALL_SYSTEM_THRESHOLD:,} served)", value=False
    )
    only_active_cws = True
    st.divider()
    st.caption(
        "Showing active community water systems. "
        "Contact names, emails, and phone numbers are PII and only shown on "
        "the System Detail tab."
    )

systems = active_cws(systems_all) if only_active_cws else systems_all.copy()
if only_small:
    systems = systems[systems["population_served_count"] < SMALL_SYSTEM_THRESHOLD]
if selected_counties:
    parish_pwsids = geo_all[geo_all["county_served"].isin(selected_counties)][
        "pwsid"
    ].unique()
    systems = systems[systems["pwsid"].isin(parish_pwsids)]

filtered_pwsids = set(systems["pwsid"])
violations = violations_all[violations_all["pwsid"].isin(filtered_pwsids)]
lcr = lcr_all[lcr_all["pwsid"].isin(filtered_pwsids)]

# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------
# Modal + global search
# ---------------------------------------------------------------------------
@st.dialog("System detail", width="large")
def show_system_dialog(pwsid: str, back_county_fips: str | None = None) -> None:
    if back_county_fips:
        if st.button("← Back to county", key=f"back_county_{back_county_fips}"):
            st.session_state["reopen_county_fips"] = back_county_fips
            st.rerun()
    render_system_detail(pwsid, systems_all, violations_all, lcr_all)


@st.dialog("County summary", width="large")
def show_county_dialog(fips: str, by_county_df, geo_exp_df) -> None:
    county_row = by_county_df[by_county_df["fips5"] == fips]
    if county_row.empty:
        st.info("Selected county has no matched data.")
        return
    cr = county_row.iloc[0]

    st.markdown(
        f"### {cr['county_display']}, {cr['primacy_agency_code']}"
    )
    c1, c2, c3 = st.columns(3)
    c1.metric("Active CWS", f"{int(cr['systems']):,}")
    c2.metric(
        "With open health-based viol.",
        f"{int(cr['with_open_health']):,}",
        delta=f"{cr['pct_open_health']:.1f}%",
        delta_color="inverse",
    )
    c3.metric("Population served", f"{int(cr['pop_served']):,}")

    county_pwsids = geo_exp_df[geo_exp_df["fips5"] == fips]["pwsid"].unique()
    county_systems = active_cws(
        systems_all[systems_all["pwsid"].isin(county_pwsids)].copy()
    )

    viol_counts = (
        violations_all[
            (violations_all["pwsid"].isin(county_pwsids))
            & (violations_all["rtc_date"].isna())
            & (violations_all["is_health_based_ind"] == "Y")
        ]
        .groupby("pwsid").size().rename("open_health_violations")
    )
    county_systems = county_systems.merge(
        viol_counts, left_on="pwsid", right_index=True, how="left"
    )
    county_systems["open_health_violations"] = (
        county_systems["open_health_violations"].fillna(0).astype(int)
    )

    table = county_systems.sort_values(
        ["open_health_violations", "population_served_count"],
        ascending=[False, False],
    )[
        [
            "pwsid",
            "pws_name",
            "city_name",
            "population_served_count",
            "service_connections_count",
            "open_health_violations",
        ]
    ].rename(
        columns={
            "pwsid": "PWSID",
            "pws_name": "System",
            "city_name": "City",
            "population_served_count": "Population",
            "service_connections_count": "Connections",
            "open_health_violations": "Open viols.",
        }
    )

    st.markdown(
        "<div style='font-size:0.85rem;color:#888b8d;margin:0.5rem 0 0.25rem 0;'>"
        "Click any row to open the system detail.</div>",
        unsafe_allow_html=True,
    )
    selection = st.dataframe(
        table,
        width="stretch",
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key=f"county_dialog_table_{fips}",
    )
    rows = (selection or {}).get("selection", {}).get("rows", [])
    if rows:
        chosen_pws = table.iloc[rows[0]]["PWSID"]
        st.session_state["open_system_modal"] = chosen_pws
        st.session_state["system_modal_back"] = fips
        st.rerun()


# Global search: type system name or PWSID from any tab to open the modal.
search_pool = (
    systems_all[["pwsid", "pws_name", "primacy_agency_code", "city_name"]]
    .dropna(subset=["pwsid", "pws_name"])
    .drop_duplicates("pwsid")
    .sort_values("pws_name")
)
search_pool["label"] = (
    search_pool["pws_name"]
    + " — "
    + search_pool["pwsid"]
    + " ("
    + search_pool["primacy_agency_code"].fillna("")
    + ")"
)
search_options = search_pool["pwsid"].tolist()
search_index = {p: l for p, l in zip(search_options, search_pool["label"])}

with st.container(border=True):
    st.markdown(
        "<div style='font-size:0.75rem;font-weight:600;letter-spacing:0.08em;"
        "color:#085eaa;text-transform:uppercase;margin-bottom:0.35rem;'>"
        "Find a system</div>",
        unsafe_allow_html=True,
    )
    picked = st.selectbox(
        "Search by system name or PWSID",
        options=search_options,
        index=None,
        placeholder="Type a system name or PWSID…",
        format_func=lambda p: search_index.get(p, p),
        label_visibility="collapsed",
        key="global_search",
    )
    if picked:
        show_system_dialog(picked)
        st.session_state["global_search"] = None

# Pending modal trigger from Map drill-down or other panels
pending = st.session_state.pop("open_system_modal", None)
back_fips = st.session_state.pop("system_modal_back", None)
if pending:
    show_system_dialog(pending, back_county_fips=back_fips)

# ---------------------------------------------------------------------------
# Pre-compute county-level aggregates so every tab can drive the county dialog
# ---------------------------------------------------------------------------
geo_exp = explode_geo_to_counties(geo_all)
_active_pop = active_cws(systems_all)[["pwsid", "population_served_count"]]
_attr = geo_exp.merge(_active_pop, on="pwsid", how="inner")
_open_health_pwsids = set(
    violations_all[
        (violations_all["rtc_date"].isna())
        & (violations_all["is_health_based_ind"] == "Y")
    ]["pwsid"]
)
_attr["has_open_health"] = _attr["pwsid"].isin(_open_health_pwsids).astype(int)
_attr["is_small"] = (
    _attr["population_served_count"] < SMALL_SYSTEM_THRESHOLD
).astype(int)
by_county = _attr.groupby(
    ["fips5", "county_display", "primacy_agency_code"], as_index=False
).agg(
    systems=("pwsid", "nunique"),
    pop_served=("population_served_count", "sum"),
    with_open_health=("has_open_health", "sum"),
    small_with_open_health=(
        "has_open_health",
        lambda s: int(((s == 1) & (_attr.loc[s.index, "is_small"] == 1)).sum()),
    ),
)
by_county["pct_open_health"] = (
    100 * by_county["with_open_health"] / by_county["systems"].clip(lower=1)
)


def trigger_system_modal(pwsid: str, key: str, back_fips: str | None = None) -> None:
    """If pwsid differs from the table's last-picked sentinel, open the system
    dialog. Used by selectable system tables across tabs."""
    if not pwsid:
        return
    if st.session_state.get(key) == pwsid:
        return
    st.session_state[key] = pwsid
    st.session_state["open_system_modal"] = pwsid
    if back_fips:
        st.session_state["system_modal_back"] = back_fips
    st.rerun()


def trigger_county_modal(fips: str, key: str) -> None:
    if not fips:
        return
    if st.session_state.get(key) == fips:
        return
    st.session_state[key] = fips
    show_county_dialog(fips, by_county, geo_exp)


(
    tab_scorecard,
    tab_detail,
    tab_watchlist,
    tab_map,
    tab_lcr,
    tab_violations,
) = st.tabs(
    [
        "Scorecard",
        "Find a System",
        "CU Watchlist",
        "Map",
        "Lead & Copper",
        "Violations",
    ]
)

# --- Scorecard ----------------------------------------------------------------
with tab_scorecard:
    section(
        "State scorecard",
        "System counts, population reach, and where compliance is slipping.",
    )
    pop = int(systems["population_served_count"].fillna(0).sum())
    small_count = (systems["population_served_count"] < SMALL_SYSTEM_THRESHOLD).sum()
    open_v = open_violations(violations)
    health_open = open_v[open_v["is_health_based_ind"] == "Y"]
    systems_with_health = health_open["pwsid"].nunique()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Water systems", f"{len(systems):,}")
    c2.metric("Population served", f"{pop:,}")
    c3.metric(
        "Small systems",
        f"{small_count:,}",
        f"{small_count / max(len(systems), 1):.0%} of total",
    )
    c4.metric(
        "Systems with open health-based violations",
        f"{systems_with_health:,}",
        delta=f"{systems_with_health / max(len(systems), 1):.0%}",
        delta_color="inverse",
    )

    st.divider()
    section("Systems by source water")
    src = systems.assign(source=label(systems["primary_source_code"], SOURCE_LABELS))
    src_counts = src["source"].value_counts().reset_index()
    src_counts.columns = ["Source", "Systems"]
    fig = px.bar(src_counts, x="Source", y="Systems")
    fig.update_layout(height=480, margin=dict(l=0, r=0, t=10, b=0))
    st.plotly_chart(fig, width="stretch")

    section("Systems by owner type")
    own = systems.assign(owner=label(systems["owner_type_code"], OWNER_TYPE_LABELS))
    own_counts = own["owner"].value_counts().reset_index()
    own_counts.columns = ["Owner type", "Systems"]
    fig = px.bar(own_counts, x="Owner type", y="Systems")
    fig.update_layout(height=480, margin=dict(l=0, r=0, t=10, b=0))
    st.plotly_chart(fig, width="stretch")

    section(
        "Top counties & parishes",
        "By active community water system count. Check a row to open the county.",
    )
    parish_rollup = (
        by_county[by_county["primacy_agency_code"].isin(selected_states)]
        .sort_values("systems", ascending=False)
        .head(15)
        .reset_index(drop=True)[
            ["county_display", "primacy_agency_code", "systems",
             "with_open_health", "pct_open_health", "fips5"]
        ]
        .rename(
            columns={
                "county_display": "County / parish",
                "primacy_agency_code": "State",
                "systems": "Systems",
                "with_open_health": "Open health-based",
                "pct_open_health": "% open health-based",
            }
        )
    )
    sel_parish = st.dataframe(
        parish_rollup,
        width="stretch",
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        column_order=[c for c in parish_rollup.columns if c != "fips5"],
        key="scorecard_parish_table",
    )
    rows = (sel_parish or {}).get("selection", {}).get("rows", [])
    if rows:
        trigger_county_modal(
            parish_rollup.iloc[rows[0]]["fips5"], "scorecard_parish_last"
        )


# --- Map ----------------------------------------------------------------------
with tab_map:
    section(
        "County choropleth",
        "Hover for county details. Click a county to see every system serving it.",
    )

    geojson = load_counties_geojson()
    if geojson is None:
        st.warning(
            "Missing `data/us_counties_fips.geojson`. Download with the curl "
            "command in the README and reload."
        )
    else:
        metric_choice = st.selectbox(
            "Color by",
            [
                "% of CWS with open health-based violation",
                "Active CWS count",
                "Active CWS with open health-based violation (count)",
                "Population served (active CWS)",
                "Small CWS (<3,300) with open health-based violation",
            ],
            index=0,
        )
        st.caption(
            "A water system serving multiple counties is counted in each county it serves. "
            "Population is the system's reported total, attributed to each served county "
            "(don't sum across counties for a state total — use the Scorecard tab)."
        )

        # by_county and geo_exp are pre-computed at module scope.
        metric_to_col = {
            "% of CWS with open health-based violation": ("pct_open_health", ".1f", "%"),
            "Active CWS count": ("systems", ",", ""),
            "Active CWS with open health-based violation (count)": (
                "with_open_health", ",", ""
            ),
            "Population served (active CWS)": ("pop_served", ",.0f", ""),
            "Small CWS (<3,300) with open health-based violation": (
                "small_with_open_health", ",", ""
            ),
        }
        col, fmt, suffix = metric_to_col[metric_choice]

        cu_state_fips = {"AR": "05", "AL": "01", "LA": "22", "MS": "28", "OK": "40", "TN": "47", "TX": "48"}
        showing_state_fips = [
            cu_state_fips[s] for s in by_county["primacy_agency_code"].unique()
            if s in cu_state_fips
        ]
        plot_geojson = {
            "type": "FeatureCollection",
            "features": [
                f for f in geojson["features"]
                if f["properties"].get("STATE") in showing_state_fips
                or f["id"][:2] in showing_state_fips
            ],
        }

        fig = px.choropleth_map(
            by_county,
            geojson=plot_geojson,
            locations="fips5",
            featureidkey="id",
            color=col,
            color_continuous_scale="OrRd"
            if "open_health" in col or "pct" in col
            else CU_BLUE_SCALE,
            map_style="carto-positron",
            zoom=4.2,
            center={"lat": 33.0, "lon": -92.0},
            opacity=0.75,
            hover_name="county_display",
            hover_data={
                "primacy_agency_code": True,
                "systems": True,
                "with_open_health": True,
                "pct_open_health": ":.1f",
                "pop_served": ":,.0f",
                "fips5": False,
                col: ":" + fmt,
            },
            labels={
                "primacy_agency_code": "State",
                "systems": "Active CWS",
                "with_open_health": "w/ open health-based viol.",
                "pct_open_health": "% w/ open health-based viol.",
                "pop_served": "Population served",
                col: metric_choice,
            },
        )
        fig.update_layout(
            height=620,
            margin=dict(l=0, r=0, t=10, b=0),
            coloraxis_colorbar=dict(title=metric_choice),
        )

        map_selection = st.plotly_chart(
            fig,
            width="stretch",
            on_select="rerun",
            selection_mode=("points",),
            key="map",
        )

        # "Back to county" path — user closed system dialog and asked to return.
        reopen_fips = st.session_state.pop("reopen_county_fips", None)
        if reopen_fips:
            show_county_dialog(reopen_fips, by_county, geo_exp)
        else:
            # New county click → open county dialog over the map
            points = (map_selection or {}).get("selection", {}).get("points", [])
            clicked_fips = points[0].get("location") if points else None
            if clicked_fips and clicked_fips != st.session_state.get("county_dialog_last"):
                st.session_state["county_dialog_last"] = clicked_fips
                show_county_dialog(clicked_fips, by_county, geo_exp)

        with st.expander("County data table"):
            st.caption("Check the box on any row to open that county.")
            disp = by_county.sort_values(col, ascending=False).rename(
                columns={
                    "fips5": "FIPS",
                    "county_display": "County / parish",
                    "primacy_agency_code": "State",
                    "systems": "Active CWS",
                    "with_open_health": "w/ open health-based viol.",
                    "pct_open_health": "% w/ open health-based viol.",
                    "pop_served": "Population served",
                    "small_with_open_health": "Small w/ open health-based viol.",
                }
            ).reset_index(drop=True)
            table_selection = st.dataframe(
                disp,
                width="stretch",
                hide_index=True,
                on_select="rerun",
                selection_mode="single-row",
                key="county_table_select",
            )
            sel_rows = (table_selection or {}).get("selection", {}).get("rows", [])
            if sel_rows:
                trigger_county_modal(
                    disp.iloc[sel_rows[0]]["FIPS"], "county_table_last"
                )


# --- CU Watchlist -------------------------------------------------------------
with tab_watchlist:
    section(
        "CU watchlist",
        "Small active community systems with open health-based violations.",
    )
    st.caption(
        "Small active community systems with at least one open health-based "
        "violation. Sorted by population × open-violation count."
    )

    small_active = active_cws(systems_all)
    small_active = small_active[
        small_active["population_served_count"] < SMALL_SYSTEM_THRESHOLD
    ]

    open_health = violations_all[
        (violations_all["rtc_date"].isna())
        & (violations_all["is_health_based_ind"] == "Y")
    ]

    counts = (
        open_health.groupby("pwsid")
        .size()
        .reset_index(name="open_health_violations")
    )

    watch = small_active.merge(counts, on="pwsid", how="inner")
    watch["priority_score"] = (
        watch["population_served_count"].fillna(0) * watch["open_health_violations"]
    )

    parish = (
        geo_all.dropna(subset=["county_served"])
        .drop_duplicates("pwsid")[["pwsid", "county_served"]]
    )
    if "county_served" in watch.columns:
        watch = watch.drop(columns=["county_served"])
    watch = watch.merge(parish, on="pwsid", how="left")

    watch_display = (
        watch[
            [
                "primacy_agency_code",
                "pwsid",
                "pws_name",
                "county_served",
                "population_served_count",
                "service_connections_count",
                "open_health_violations",
                "priority_score",
            ]
        ]
        .sort_values("priority_score", ascending=False)
        .rename(
            columns={
                "primacy_agency_code": "State",
                "pwsid": "PWSID",
                "pws_name": "System",
                "county_served": "Parish / county",
                "population_served_count": "Population",
                "service_connections_count": "Connections",
                "open_health_violations": "Open health-based viols.",
                "priority_score": "Priority",
            }
        )
    )

    st.metric("Systems on watchlist", f"{len(watch_display):,}")
    st.caption("Check a row to open the system detail.")
    sel_watch = st.dataframe(
        watch_display.reset_index(drop=True),
        width="stretch",
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key="watchlist_table",
    )
    rows = (sel_watch or {}).get("selection", {}).get("rows", [])
    if rows:
        trigger_system_modal(
            watch_display.reset_index(drop=True).iloc[rows[0]]["PWSID"],
            "watchlist_last",
        )


# --- System detail ------------------------------------------------------------
with tab_detail:
    section(
        "Find a system",
        "Pick a state, then a system. The full record opens in a popup. "
        "Or use the search at the top of the page from any tab.",
    )

    state_options = sorted(systems_all["primacy_agency_code"].dropna().unique().tolist())
    if not state_options:
        st.warning("No systems available.")
        st.stop()

    with st.container(border=True):
        col_state, col_system = st.columns([1, 3])
        with col_state:
            state_choice = st.selectbox("**State**", state_options, index=0)

        state_systems = systems_all[systems_all["primacy_agency_code"] == state_choice]
        pws_list = state_systems[["pwsid", "pws_name"]].dropna().drop_duplicates()
        pws_list = pws_list.sort_values("pws_name")
        pws_list["label"] = pws_list["pwsid"] + " — " + pws_list["pws_name"]
        options = pws_list["pwsid"].tolist()
        with col_system:
            choice = st.selectbox(
                "**System**",
                options=options,
                index=None,
                placeholder="Type or scroll…",
                format_func=lambda p: pws_list.set_index("pwsid").loc[p, "label"],
                key="find_system_picker",
            )
        if choice:
            show_system_dialog(choice)
            st.session_state["find_system_picker"] = None


# --- Lead & Copper ------------------------------------------------------------
with tab_lcr:
    section(
        "Lead & copper sampling",
        f"Federal action level is {LCR_ACTION_LEVEL_PPB} ppb at the 90th percentile.",
    )
    st.caption(
        f"Federal action level is {LCR_ACTION_LEVEL_PPB} ppb at the 90th percentile. "
        "Sample-level results aren't in this Envirofacts table — only sampling "
        "periods. Full results require a per-state pull."
    )

    by_year = (
        lcr.assign(year=lcr["sampling_end_date"].dt.year)
        .dropna(subset=["year"])
        .groupby("year")
        .agg(samples=("sample_id", "count"), systems=("pwsid", "nunique"))
        .reset_index()
    )
    if not by_year.empty:
        fig = px.line(
            by_year, x="year", y=["samples", "systems"], markers=True,
            labels={"value": "Count", "variable": "Metric", "year": "Sampling year"},
        )
        fig.update_layout(height=360, margin=dict(l=0, r=0, t=10, b=0))
        st.plotly_chart(fig, width="stretch")

    section("Most recent sampling", "Top 50 systems by last sample date.")
    recent = (
        lcr.sort_values("sampling_end_date", ascending=False)
        .drop_duplicates("pwsid")
        .merge(
            systems[["pwsid", "pws_name", "population_served_count"]],
            on="pwsid",
            how="left",
        )
        .head(50)[
            ["pwsid", "pws_name", "population_served_count", "sampling_end_date"]
        ]
        .rename(
            columns={
                "pwsid": "PWSID",
                "pws_name": "System",
                "population_served_count": "Population",
                "sampling_end_date": "Last sample",
            }
        )
    )
    st.caption("Check a row to open the system detail.")
    sel_lcr = st.dataframe(
        recent.reset_index(drop=True),
        width="stretch",
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key="lcr_recent_table",
    )
    rows = (sel_lcr or {}).get("selection", {}).get("rows", [])
    if rows:
        trigger_system_modal(
            recent.reset_index(drop=True).iloc[rows[0]]["PWSID"], "lcr_recent_last"
        )


# --- Violations ---------------------------------------------------------------
with tab_violations:
    section(
        "Violations browser",
        "Filter by category, status, and severity across selected states.",
    )
    health_only = st.checkbox("Health-based only", value=True)
    open_only = st.checkbox("Open only (no return-to-compliance date)", value=True)

    v = violations.copy()
    if health_only:
        v = v[v["is_health_based_ind"] == "Y"]
    if open_only:
        v = v[v["rtc_date"].isna()]

    c1, c2, c3 = st.columns(3)
    c1.metric("Violations", f"{len(v):,}")
    c2.metric("Distinct systems", f"{v['pwsid'].nunique():,}")
    c3.metric(
        "Median age (days)",
        f"{(pd.Timestamp.now() - v['compl_per_begin_date']).dt.days.median():.0f}"
        if not v.empty
        else "—",
    )

    if not v.empty:
        cat_counts = (
            v["violation_category_code"]
            .value_counts()
            .reset_index()
        )
        cat_counts.columns = ["Category", "Violations"]
        fig = px.bar(cat_counts, x="Category", y="Violations")
        fig.update_layout(height=320, margin=dict(l=0, r=0, t=10, b=0))
        st.plotly_chart(fig, width="stretch")

    v_display = (
        v.merge(systems[["pwsid", "pws_name"]], on="pwsid", how="left")[
            [
                "pwsid",
                "pws_name",
                "compl_per_begin_date",
                "violation_category_code",
                "contaminant_code",
                "is_health_based_ind",
                "rtc_date",
            ]
        ]
        .sort_values("compl_per_begin_date", ascending=False)
        .rename(
            columns={
                "pwsid": "PWSID",
                "pws_name": "System",
                "compl_per_begin_date": "Begin",
                "violation_category_code": "Category",
                "contaminant_code": "Contaminant",
                "is_health_based_ind": "Health-based",
                "rtc_date": "RTC date",
            }
        )
    )
    st.caption("Check a row to open the system detail.")
    sel_v = st.dataframe(
        v_display.reset_index(drop=True),
        width="stretch",
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key="violations_table",
    )
    rows = (sel_v or {}).get("selection", {}).get("rows", [])
    if rows:
        trigger_system_modal(
            v_display.reset_index(drop=True).iloc[rows[0]]["PWSID"],
            "violations_last",
        )
