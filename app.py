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
    st.title("CU Water Systems")
    st.caption("Enter the access password to continue.")
    with st.form("login", clear_on_submit=True):
        pw = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Enter")
    if submitted:
        if pw == expected:
            st.session_state["auth_ok"] = True
            st.rerun()
        else:
            st.error("Incorrect password.")
    st.stop()


_password_gate()

st.markdown(
    """
    <style>
      @import url('https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;500;600;700&display=swap');

      html, body, .stApp,
      .stMarkdown, .stMetric, .stDataFrame, .stSelectbox, .stMultiSelect,
      .stRadio, .stCheckbox, .stButton button, h1, h2, h3, h4, h5, h6,
      [data-testid="stMetricLabel"], [data-testid="stMetricValue"],
      [data-testid="stMetricDelta"], [data-testid="stCaptionContainer"],
      [data-baseweb="select"] *, [data-baseweb="input"] *,
      [data-baseweb="tab"], [data-testid="stSidebar"] * {
        font-family: 'Poppins', system-ui, sans-serif !important;
      }

      /* Keep Material Symbols glyphs rendering as icons, not text */
      [class*="material-symbols"], [class*="material-icons"],
      .material-symbols-outlined, .material-symbols-rounded,
      .material-icons, span[role="img"][class*="icon"] {
        font-family: 'Material Symbols Outlined', 'Material Symbols Rounded',
                     'Material Icons' !important;
      }

      h1, h2, h3 { color: #085eaa; font-weight: 600; letter-spacing: -0.01em; }

      /* Tabs: brand the active tab underline */
      .stTabs [aria-selected="true"] { color: #085eaa !important; }
      .stTabs [data-baseweb="tab-highlight"] { background-color: #085eaa !important; }

      /* Metric value emphasis */
      [data-testid="stMetricValue"] {
        color: #085eaa;
        font-weight: 600;
      }

      /* Sidebar accent */
      [data-testid="stSidebar"] h2,
      [data-testid="stSidebar"] h3 { color: #085eaa; }
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

st.title("CU Water Systems")

available = _available_states()

if not available:
    st.error(
        "No data found. Run `python etl.py --states AR AL LA MS OK TN TX` to "
        "populate `data/<STATE>/` with Parquet files."
    )
    st.stop()

with st.sidebar:
    st.header("States")
    selected_states = st.multiselect(
        "Include", available, default=available,
    )
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

st.caption(
    f"{source_label} · Federal feed may lag state viewers by ~1 quarter."
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
(
    tab_scorecard,
    tab_map,
    tab_watchlist,
    tab_detail,
    tab_lcr,
    tab_violations,
) = st.tabs(
    [
        "Scorecard",
        "Map",
        "CU Watchlist",
        "System Detail",
        "Lead & Copper",
        "Violations",
    ]
)

# --- Scorecard ----------------------------------------------------------------
with tab_scorecard:
    st.subheader("State scorecard")
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
    col_left, col_right = st.columns(2)

    with col_left:
        st.markdown("**Systems by source water**")
        src = systems.assign(source=label(systems["primary_source_code"], SOURCE_LABELS))
        src_counts = src["source"].value_counts().reset_index()
        src_counts.columns = ["Source", "Systems"]
        fig = px.bar(src_counts, x="Source", y="Systems")
        fig.update_layout(height=320, margin=dict(l=0, r=0, t=10, b=0))
        st.plotly_chart(fig, width="stretch")

    with col_right:
        st.markdown("**Systems by owner type**")
        own = systems.assign(owner=label(systems["owner_type_code"], OWNER_TYPE_LABELS))
        own_counts = own["owner"].value_counts().reset_index()
        own_counts.columns = ["Owner type", "Systems"]
        fig = px.bar(own_counts, x="Owner type", y="Systems")
        fig.update_layout(height=320, margin=dict(l=0, r=0, t=10, b=0))
        st.plotly_chart(fig, width="stretch")

    st.markdown("**Top parishes by system count**")
    parish_rollup = (
        geo_all[geo_all["pwsid"].isin(filtered_pwsids)]
        .groupby("county_served")
        .agg(systems=("pwsid", "nunique"))
        .sort_values("systems", ascending=False)
        .head(15)
        .reset_index()
        .rename(columns={"county_served": "Parish", "systems": "Systems"})
    )
    st.dataframe(parish_rollup, width="stretch", hide_index=True)


# --- Map ----------------------------------------------------------------------
with tab_map:
    st.subheader("County choropleth")

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

        # Build per-(pwsid, fips5) attribution table
        geo_exp = explode_geo_to_counties(geo_all)
        active = active_cws(systems_all)[
            ["pwsid", "population_served_count"]
        ]
        attr = geo_exp.merge(active, on="pwsid", how="inner")

        open_health_pwsids = set(
            violations_all[
                (violations_all["rtc_date"].isna())
                & (violations_all["is_health_based_ind"] == "Y")
            ]["pwsid"]
        )
        attr["has_open_health"] = attr["pwsid"].isin(open_health_pwsids).astype(int)
        attr["is_small"] = (
            attr["population_served_count"] < SMALL_SYSTEM_THRESHOLD
        ).astype(int)

        by_county = attr.groupby(
            ["fips5", "county_display", "primacy_agency_code"], as_index=False
        ).agg(
            systems=("pwsid", "nunique"),
            pop_served=("population_served_count", "sum"),
            with_open_health=("has_open_health", "sum"),
            small_with_open_health=(
                "has_open_health",
                lambda s: int(((s == 1) & (attr.loc[s.index, "is_small"] == 1)).sum()),
            ),
        )
        by_county["pct_open_health"] = (
            100 * by_county["with_open_health"] / by_county["systems"].clip(lower=1)
        )

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

        # --- Drill-down panel: systems in the clicked county ---
        clicked_fips: str | None = None
        points = (map_selection or {}).get("selection", {}).get("points", [])
        if points:
            clicked_fips = points[0].get("location")

        if clicked_fips:
            county_row = by_county[by_county["fips5"] == clicked_fips]
            if not county_row.empty:
                cr = county_row.iloc[0]
                st.markdown(
                    f"### {cr['county_display']} ({cr['primacy_agency_code']}) — "
                    f"{int(cr['systems'])} active CWS"
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

                county_pwsids = geo_exp[geo_exp["fips5"] == clicked_fips]["pwsid"].unique()
                county_systems = systems_all[systems_all["pwsid"].isin(county_pwsids)].copy()
                county_systems = active_cws(county_systems)

                viol_counts = (
                    violations_all[
                        (violations_all["pwsid"].isin(county_pwsids))
                        & (violations_all["rtc_date"].isna())
                        & (violations_all["is_health_based_ind"] == "Y")
                    ]
                    .groupby("pwsid")
                    .size()
                    .rename("open_health_violations")
                )
                county_systems = county_systems.merge(
                    viol_counts, left_on="pwsid", right_index=True, how="left"
                )
                county_systems["open_health_violations"] = (
                    county_systems["open_health_violations"].fillna(0).astype(int)
                )
                county_systems["source"] = label(
                    county_systems["primary_source_code"], SOURCE_LABELS
                )
                county_systems["owner"] = label(
                    county_systems["owner_type_code"], OWNER_TYPE_LABELS
                )

                table = county_systems[
                    [
                        "pwsid",
                        "pws_name",
                        "city_name",
                        "population_served_count",
                        "service_connections_count",
                        "source",
                        "owner",
                        "open_health_violations",
                    ]
                ].sort_values(
                    ["open_health_violations", "population_served_count"],
                    ascending=[False, False],
                ).rename(
                    columns={
                        "pwsid": "PWSID",
                        "pws_name": "System",
                        "city_name": "City",
                        "population_served_count": "Population",
                        "service_connections_count": "Connections",
                        "source": "Source",
                        "owner": "Owner",
                        "open_health_violations": "Open health-based viols.",
                    }
                )

                selection = st.dataframe(
                    table,
                    width="stretch",
                    hide_index=True,
                    on_select="rerun",
                    selection_mode="single-row",
                    key="county_systems_table",
                )
                rows = (selection or {}).get("selection", {}).get("rows", [])
                if rows:
                    chosen_pws = table.iloc[rows[0]]["PWSID"]
                    st.session_state["selected_pws"] = chosen_pws
                    st.success(
                        f"Selected **{chosen_pws}** — open the **System Detail** "
                        f"tab to view the full record."
                    )
            else:
                st.info("Selected county has no matched data.")
        else:
            st.caption("Click a county on the map to see the systems serving it.")

        with st.expander("County data table"):
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
            )
            st.dataframe(disp, width="stretch", hide_index=True)


# --- CU Watchlist -------------------------------------------------------------
with tab_watchlist:
    st.subheader("CU watchlist")
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
    st.dataframe(watch_display, width="stretch", hide_index=True)


# --- System detail ------------------------------------------------------------
with tab_detail:
    st.subheader("System detail")
    # Allow picking from the full systems table if the user came from the Map tab
    # and the chosen PWSID isn't in the current filtered set.
    preselected = st.session_state.get("selected_pws")
    if preselected and preselected not in set(systems["pwsid"]):
        pws_source = systems_all
        st.info(
            f"Showing {preselected} from the Map selection (current sidebar filters "
            f"don't include this system)."
        )
    else:
        pws_source = systems

    preselected_state = (
        systems_all.loc[systems_all["pwsid"] == preselected, "primacy_agency_code"]
        .iloc[0]
        if preselected and (systems_all["pwsid"] == preselected).any()
        else None
    )

    state_options = sorted(pws_source["primacy_agency_code"].dropna().unique().tolist())
    if not state_options:
        st.warning("No systems available with the current filters.")
        st.stop()

    default_state_idx = (
        state_options.index(preselected_state)
        if preselected_state in state_options
        else 0
    )
    with st.container(border=True):
        st.markdown("#### Choose a system to view")
        st.caption("Pick a state, then the system you want details on.")
        col_state, col_system = st.columns([1, 3])
        with col_state:
            state_choice = st.selectbox(
                "**State**", state_options, index=default_state_idx
            )

        state_systems = pws_source[pws_source["primacy_agency_code"] == state_choice]
        pws_list = state_systems[["pwsid", "pws_name"]].dropna().drop_duplicates()
        pws_list = pws_list.sort_values("pws_name")
        pws_list["label"] = pws_list["pwsid"] + " — " + pws_list["pws_name"]
        options = pws_list["pwsid"].tolist()
        with col_system:
            system_index = (
                options.index(preselected) if preselected in options else None
            )
            choice = st.selectbox(
                "**System**",
                options=options,
                index=system_index,
                placeholder="Select a system...",
                format_func=lambda p: pws_list.set_index("pwsid").loc[p, "label"],
            )

    if choice:
        row = systems_all[systems_all["pwsid"] == choice].iloc[0]
        st.markdown(f"### {row['pws_name']}")

        c1, c2, c3 = st.columns(3)
        c1.metric("Population served", f"{int(row['population_served_count'] or 0):,}")
        c2.metric("Connections", f"{int(row['service_connections_count'] or 0):,}")
        c3.metric(
            "System type",
            TYPE_LABELS.get(row["pws_type_code"], row["pws_type_code"]),
        )

        c4, c5, c6 = st.columns(3)
        c4.metric(
            "Source",
            SOURCE_LABELS.get(row["primary_source_code"], row["primary_source_code"]),
        )
        c5.metric(
            "Owner",
            OWNER_TYPE_LABELS.get(row["owner_type_code"], row["owner_type_code"]),
        )
        c6.metric("Status", "Active" if row["pws_activity_code"] == "A" else "Inactive")

        st.markdown("**Location**")
        st.write(
            f"{row.get('address_line1', '') or ''}  \n"
            f"{row.get('city_name', '')}, {row.get('state_code', '')} "
            f"{row.get('zip_code', '')}"
        )

        with st.expander("Contact (PII — handle per CU confidentiality rules)"):
            st.write(f"**Administrator:** {row.get('admin_name', '—')}")
            st.write(f"**Organization:** {row.get('org_name', '—')}")
            st.write(f"**Email:** {row.get('email_addr', '—')}")
            st.write(f"**Phone:** {row.get('phone_number', '—')}")

        st.divider()
        st.markdown("**Violation history**")
        v = violations_all[violations_all["pwsid"] == choice].copy()
        if v.empty:
            st.info("No violations in the sample.")
        else:
            v["status"] = v["rtc_date"].apply(
                lambda d: "Returned to compliance" if pd.notna(d) else "Open"
            )
            v_display = v[
                [
                    "compl_per_begin_date",
                    "violation_code",
                    "violation_category_code",
                    "is_health_based_ind",
                    "contaminant_code",
                    "status",
                    "rtc_date",
                ]
            ].sort_values("compl_per_begin_date", ascending=False)
            v_display.columns = [
                "Begin",
                "Code",
                "Category",
                "Health-based",
                "Contaminant",
                "Status",
                "RTC date",
            ]
            st.dataframe(v_display, width="stretch", hide_index=True)

        st.markdown("**Lead & copper samples**")
        l = lcr_all[lcr_all["pwsid"] == choice]
        if l.empty:
            st.info("No LCR samples in the sample.")
        else:
            st.dataframe(
                l[["sample_id", "sampling_start_date", "sampling_end_date"]],
                width="stretch",
                hide_index=True,
            )


# --- Lead & Copper ------------------------------------------------------------
with tab_lcr:
    st.subheader("Lead & copper sampling")
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

    st.markdown("**Systems with most recent sampling**")
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
    st.dataframe(recent, width="stretch", hide_index=True)


# --- Violations ---------------------------------------------------------------
with tab_violations:
    st.subheader("Violations browser")
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
    st.dataframe(v_display, width="stretch", hide_index=True)
