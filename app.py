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
COUNTY_CONTEXT_FILE = PARQUET_DIR / "county_context.parquet"
TX_DISTRICT_CONTACTS_FILE = PARQUET_DIR / "tx_district_contacts.parquet"
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

# --- EPA SDWIS code decoding -------------------------------------------------
# Plain-language labels for the violation codes a TA provider needs to read at
# a glance. Only codes verified against known federal MCLs/definitions are
# mapped; anything unmapped falls back to the raw code so nothing is ever
# mislabeled. Sources: EPA SDWIS_FED data dictionary + 40 CFR 141 MCLs.
VIOLATION_CATEGORY_LABELS = {
    "MCL": "Max contaminant level exceeded",
    "MRDL": "Max disinfectant level exceeded",
    "TT": "Treatment technique failure",
    "MR": "Monitoring & reporting",
    "MON": "Monitoring (no sample)",
    "RPT": "Reporting (late/missing)",
    "Other": "Other",
}
# Monitoring & reporting categories — administrative, not a contaminant
# exceedance. A small system buried in these is a capacity-support candidate.
MR_CATEGORIES = {"MR", "MON", "RPT"}

# Public notification tier drives urgency. Tier 1 = acute, 24-hour notice.
PN_TIER_LABELS = {
    "1": "Tier 1 — acute (24-hr notice)",
    "2": "Tier 2 — 30-day notice",
    "3": "Tier 3 — annual notice",
}

# Rule codes — map the well-known rules; fall back to "Rule NNN" otherwise.
RULE_CODE_LABELS = {
    "110": "Total Coliform Rule",
    "111": "Revised Total Coliform Rule",
    "121": "Surface Water Treatment Rule",
    "122": "Interim Enhanced SWTR",
    "123": "Long Term 1 Enhanced SWTR",
    "124": "Long Term 2 Enhanced SWTR",
    "140": "Ground Water Rule",
    "210": "Stage 1 Disinfection Byproducts",
    "220": "Stage 2 Disinfection Byproducts",
    "310": "Volatile Organic Chemicals",
    "320": "Synthetic Organic Chemicals",
    "331": "Nitrates",
    "340": "Radionuclides",
    "350": "Lead & Copper Rule",
    "351": "Lead & Copper Rule Revisions",
    "410": "Public Notification Rule",
    "420": "Consumer Confidence Report Rule",
    "500": "Not regulated",
}

# Contaminant codes verified against their federal MCL during this build.
CONTAMINANT_LABELS = {
    "2950": "Total trihalomethanes (TTHM)",
    "2456": "Haloacetic acids (HAA5)",
    "1005": "Arsenic",
    "1040": "Nitrate",
    "1041": "Nitrite",
    "1025": "Fluoride",
    "1020": "Chromium (total)",
    "1094": "Asbestos",
    "4000": "Gross alpha",
    "4010": "Combined radium (226/228)",
    "4006": "Uranium",
    "8000": "Coliform (total)",
    "5000": "Lead & copper (treatment technique)",
}


def decode_category(code) -> str:
    c = _safe_str(code)
    return VIOLATION_CATEGORY_LABELS.get(c, c or "—")


def decode_tier(code) -> str:
    c = _safe_str(code).split(".")[0]  # tier sometimes arrives as "1.0"
    return PN_TIER_LABELS.get(c, c or "—")


def decode_rule(code) -> str:
    c = _safe_str(code).split(".")[0]
    return RULE_CODE_LABELS.get(c, f"Rule {c}" if c else "—")


def decode_contaminant(code) -> str:
    c = _safe_str(code)
    return CONTAMINANT_LABELS.get(c, f"Code {c}" if c and c != "—" else "—")


def fmt_measure_vs_mcl(viol_measure, unit, state_mcl) -> str:
    """Render 'measured X unit vs Y MCL' when the numbers are present."""
    m = _safe_str(viol_measure)
    u = _safe_str(unit)
    mcl = _safe_str(state_mcl)
    if not m:
        return "—"
    out = m + (f" {u}" if u else "")
    if mcl:
        out += f" (limit {mcl}{f' {u}' if u else ''})"
    return out

st.set_page_config(
    page_title="CU Water Systems",
    page_icon="💧",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# --- CU brand ---------------------------------------------------------------
CU_PRIMARY = "#085eaa"
CU_SECONDARY = "#66b1e2"
CU_TERTIARY = "#0088ce"
CU_GRAY = "#6b7280"
CU_BLUE_SCALE = ["#f4f7fb", "#66b1e2", "#0088ce", "#085eaa"]
CU_QUALITATIVE = [CU_PRIMARY, CU_TERTIARY, CU_SECONDARY, CU_GRAY, "#1f2933"]

px.defaults.color_discrete_sequence = CU_QUALITATIVE
px.defaults.color_continuous_scale = CU_BLUE_SCALE
px.defaults.template = "plotly_white"




def _safe_str(value) -> str:
    """Coerce a pandas-row cell to a plain str, treating NaN/None as empty."""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value)


def _safe_int(value) -> int:
    try:
        if value is None or pd.isna(value):
            return 0
    except (TypeError, ValueError):
        pass
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# State Drinking Water Watch deep-link URLs.
# These are EPA's distributed Drinking Water Watch app, deployed per-state.
# The SearchDispatch endpoint accepts a PWSID via the `number` param and lands
# the user on the matching system page. URLs verified per state's portal root.
# Add new states here as we confirm their DWW URL pattern.
STATE_DWW_URLS = {
    # AL DWW server (dww.adem.alabama.gov) is unreachable. Point to ADEM's
    # Drinking Water Branch info page — has the branch phone and email
    # for staff to call when EPA contact is stale.
    "AL": "https://adem.alabama.gov/water/drinking-water-branch",
    # OK DEQ DWW (sdwis.deq.state.ok.us) is unreachable too. Point to the
    # DEQ Public Water Supply program page — has branch contact info.
    "OK": "https://oklahoma.gov/deq/divisions/water-quality/public-water-supply.html",
    # MS DWW's standard SearchDispatch URL throws 500s — land on DWW home.
    "MS": "https://apps.msdh.ms.gov/DWW/",
    # TN moved off classic DWW to a Tableau viewer that doesn't accept
    # per-system URL params — land on the home view.
    "TN": "https://data.tn.gov/t/Public/views/TNDrinkingWaterWatch/Home_page",
    # TX uses TCEQ Drinking Water Viewer (custom app, JS-rendered) — search-style.
    "TX": "https://dwv.tceq.texas.gov/",
    # LA's Safe Drinking Water portal (sdw.ldh.la.gov). robots.txt blocks
    # crawlers but human navigation via a clicked link is fine — same as
    # a bookmark. Staff search by PWSID inside the portal.
    "LA": "https://sdw.ldh.la.gov/",
    # AR has no public per-system viewer. Point to ADH's Drinking Water
    # Systems & Operators info page — has program navigation and the
    # Engineering Section direct line (501-661-2623).
    "AR": "https://healthy.arkansas.gov/programs-services/public-health-safety/drinking-water-systems-operators/",
}
STATE_DWW_LABELS = {
    "AL": "ADEM Drinking Water Branch",
    "MS": "MSDH Drinking Water Watch",
    "OK": "OK DEQ Public Water Supply",
    "TN": "TDEC Drinking Water Watch",
    "TX": "TCEQ Drinking Water Viewer",
    "LA": "LDH Safe Drinking Water",
    "AR": "ADH Drinking Water Program",
}
# States whose link lands users on a homepage instead of a pre-filled
# detail page. Hint text adapts to show the PWSID inline as a copy chip.
# AL lands on an info page (no search box) — hint text mentions calling
# the branch directly instead.
STATE_DWW_REQUIRES_SEARCH = {"MS", "TN", "TX", "LA"}
STATE_DWW_INFO_PAGE = {"AL", "OK", "AR"}


def _render_state_dww_link(pwsid: str, state_code: str) -> None:
    """Render a small 'View on [state] DWW' link for states without bulk data.

    Gives staff a one-click jump to the state's authoritative record —
    useful when EPA's federal feed has stale or missing operator contacts.
    No-op for states not in STATE_DWW_URLS.
    """
    state_code = (state_code or "").upper().strip()
    if state_code not in STATE_DWW_URLS:
        return
    url = STATE_DWW_URLS[state_code].format(pwsid=pwsid)
    label = STATE_DWW_LABELS[state_code]
    if state_code in STATE_DWW_INFO_PAGE:
        button_text = f"Open {label}"
        hint = (
            "The state's per-system Drinking Water Watch is currently "
            "unreachable. This page has branch phone/email to contact the "
            "agency directly."
        )
    elif state_code in STATE_DWW_REQUIRES_SEARCH:
        button_text = f"Search {label}"
        hint = (
            f"Lands on the search page. Paste this PWSID to find the system: "
            f"<code style='background:#eef3f9;padding:0.05rem 0.35rem;border-radius:4px;"
            f"font-size:0.8rem;color:#085eaa;'>{pwsid}</code>"
        )
    else:
        button_text = f"View on {label}"
        hint = "State portal may have fresher operator info than the federal feed."
    st.markdown(
        f"""
        <div style='margin: 0.25rem 0 1rem 0;'>
          <a href='{url}' target='_blank' rel='noopener noreferrer' style='
              display:inline-flex;
              align-items:center;
              gap:0.45rem;
              background:#ffffff;
              border:1px solid #d8e2ee;
              color:#085eaa;
              padding:0.45rem 0.85rem;
              border-radius:8px;
              font-size:0.875rem;
              font-weight:500;
              text-decoration:none;
              transition: background 0.15s, border-color 0.15s;
          ' onmouseover="this.style.background='#eaf2fa';this.style.borderColor='#0088ce'"
            onmouseout="this.style.background='#ffffff';this.style.borderColor='#d8e2ee'">
            {button_text} <span style='opacity:0.7;'>↗</span>
          </a>
          <div style='font-size:0.75rem;color:#6b7280;margin-top:0.35rem;'>
            {hint}
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_service_area_context(pwsid: str) -> None:
    """Show Census ACS + persistent-poverty context for the counties this system serves.

    Quietly does nothing if the enrichment parquet hasn't been built or the
    system has no matched county.
    """
    ctx = load_county_context()
    if ctx.empty:
        return

    # Map this pwsid to the counties it serves via the cached geo explode.
    served = geo_exp[geo_exp["pwsid"] == pwsid][["fips5", "county_display", "primacy_agency_code"]]
    if served.empty:
        return

    rows = served.merge(ctx, on="fips5", how="left", suffixes=("", "_ctx"))
    rows = rows.dropna(subset=["total_population"])
    if rows.empty:
        return

    any_ppc = bool(rows["persistent_poverty"].fillna(False).any())
    any_high = bool(rows["high_poverty_current"].fillna(False).any())

    flag_html = ""
    if any_ppc:
        flag_html = (
            "<span style='background:#fbe9e7;color:#a32c14;border:1px solid #f4c5b5;"
            "padding:0.2rem 0.55rem;border-radius:999px;font-size:0.72rem;"
            "font-weight:600;letter-spacing:0.04em;margin-left:0.5rem;'>"
            "Persistent poverty</span>"
        )
    elif any_high:
        flag_html = (
            "<span style='background:#fff4e0;color:#8a5a00;border:1px solid #f1d8a3;"
            "padding:0.2rem 0.55rem;border-radius:999px;font-size:0.72rem;"
            "font-weight:600;letter-spacing:0.04em;margin-left:0.5rem;'>"
            "High current poverty</span>"
        )

    header = (
        "<div style='font-size:0.72rem;font-weight:700;letter-spacing:0.14em;"
        "color:#085eaa;text-transform:uppercase;margin:1rem 0 0.5rem 0;'>"
        f"Service area context{flag_html}</div>"
    )
    st.markdown(header, unsafe_allow_html=True)

    def _fmt_pct(v: float | None) -> str:
        return f"{v * 100:.1f}%" if pd.notna(v) else "—"

    def _fmt_money(v) -> str:
        return f"${int(v):,}" if pd.notna(v) and v else "—"

    def _fmt_pop(v) -> str:
        return f"{int(v):,}" if pd.notna(v) else "—"

    table_rows = []
    for _, r in rows.iterrows():
        badges = []
        if bool(r.get("persistent_poverty")):
            badges.append("PPC")
        if bool(r.get("high_poverty_current")) and "PPC" not in badges:
            badges.append("High poverty")
        badge_str = ", ".join(badges) if badges else "—"
        table_rows.append({
            "County": f"{r['county_display']}, {r['primacy_agency_code']}",
            "Population": _fmt_pop(r["total_population"]),
            "Poverty rate": _fmt_pct(r["poverty_rate"]),
            "Median HH income": _fmt_money(r["median_hh_income"]),
            "Flags": badge_str,
        })

    ctx_df = pd.DataFrame(table_rows)
    st.dataframe(
        ctx_df,
        width="stretch",
        hide_index=True,
        column_config={col: st.column_config.TextColumn(col) for col in ctx_df.columns},
    )
    st.caption(
        "Census ACS 5-year estimates. Persistent-poverty flag from USDA ERS "
        "(20%+ poverty in 1980, 1990, 2000 censuses and most recent ACS). "
        "'High current poverty' = current ACS rate ≥ 20%."
    )


def render_system_detail(pwsid: str, systems_df, violations_df, lcr_df) -> None:
    """Render the full system detail block — used by Find a System tab + modal."""
    matches = systems_df[systems_df["pwsid"] == pwsid]
    if matches.empty:
        st.warning(f"No record found for {pwsid}.")
        return
    row = matches.iloc[0]

    st.markdown(
        f"### {_safe_str(row['pws_name'])}  \n"
        f"<span style='color:#6b7280;font-size:0.875rem;'>"
        f"{_safe_str(row['pwsid'])} · {_safe_str(row.get('city_name', ''))}, "
        f"{_safe_str(row.get('state_code', ''))}</span>",
        unsafe_allow_html=True,
    )

    admin = _safe_str(row.get("admin_name")) or "—"
    org = _safe_str(row.get("org_name")) or "—"
    email = _safe_str(row.get("email_addr"))
    phone = _safe_str(row.get("phone_number"))
    alt_phone = _safe_str(row.get("alt_phone_number"))

    email_html = (
        f"<a href='mailto:{email}' style='color:#085eaa;text-decoration:none;'>{email}</a>"
        if email and email != "—"
        else "<span style='color:#6b7280;'>Not on file</span>"
    )
    phone_html = (
        f"<a href='tel:{phone}' style='color:#085eaa;text-decoration:none;'>{phone}</a>"
        if phone and phone != "—"
        else "<span style='color:#6b7280;'>Not on file</span>"
    )
    alt_phone_html = (
        f" · <span style='color:#6b7280;'>alt</span> "
        f"<a href='tel:{alt_phone}' style='color:#085eaa;text-decoration:none;'>{alt_phone}</a>"
        if alt_phone else ""
    )

    # Supplemental TX district contact, if a fuzzy match exists. EPA fields
    # above always take precedence — this is appended, never replaces.
    tx_supplemental_html = ""
    state_code_for_tx = _safe_str(row.get("state_code")).upper()
    if state_code_for_tx == "TX":
        tx_contacts = load_tx_district_contacts()
        tx_match = tx_contacts[tx_contacts["pwsid"] == pwsid]
        if not tx_match.empty:
            tx = tx_match.iloc[0]
            tx_name = _safe_str(tx.get("contact_name"))
            tx_title = _safe_str(tx.get("contact_title"))
            tx_phone = _safe_str(tx.get("phone"))
            tx_address = _safe_str(tx.get("address_full"))
            tx_score = int(tx.get("match_score") or 0)
            parts = []
            if tx_name:
                parts.append(
                    f"<span style='color:#1f2933;font-weight:600;'>{tx_name}</span>"
                    + (f" · <span style='color:#6b7280;'>{tx_title}</span>" if tx_title else "")
                )
            if tx_phone:
                parts.append(
                    f"<a href='tel:{tx_phone}' style='color:#085eaa;text-decoration:none;'>{tx_phone}</a>"
                )
            if tx_address:
                parts.append(
                    f"<span style='color:#6b7280;'>{tx_address}</span>"
                )
            if parts:
                tx_supplemental_html = (
                    "<div style='margin-top:0.85rem;padding-top:0.75rem;"
                    "border-top:1px solid #f0f2f5;'>"
                    "<div style='font-size:0.72rem;font-weight:700;letter-spacing:0.14em;"
                    "color:#0088ce;text-transform:uppercase;margin-bottom:0.35rem;'>"
                    "Also in TX Water Districts "
                    f"<span style='font-weight:500;letter-spacing:0;text-transform:none;"
                    f"color:#6b7280;font-size:0.7rem;'>"
                    f"· best-effort name match (score {tx_score})</span>"
                    "</div>"
                    f"<div style='font-size:0.95rem;line-height:1.5;'>"
                    + "<br/>".join(parts)
                    + "</div></div>"
                )

    st.markdown(
        f"""
        <div style='background:#ffffff;border:1px solid #d8e2ee;border-left:4px solid #085eaa;
            border-radius:8px;padding:1.25rem 1.5rem;margin:0.5rem 0 1.25rem 0;
            box-shadow:0 1px 2px rgba(8,94,170,0.05);'>
          <div style='font-size:0.72rem;font-weight:700;letter-spacing:0.14em;
              color:#085eaa;text-transform:uppercase;margin-bottom:0.5rem;'>Operator contact</div>
          <div style='font-size:1.1rem;font-weight:600;color:#1f2933;'>{admin}</div>
          <div style='color:#6b7280;font-size:0.875rem;margin-bottom:0.85rem;'>{org}</div>
          <div style='display:flex;flex-wrap:wrap;gap:1.5rem;font-size:1rem;'>
            <div><div style='color:#6b7280;font-size:0.75rem;font-weight:600;text-transform:uppercase;letter-spacing:0.06em;margin-bottom:0.15rem;'>Email</div><div>{email_html}</div></div>
            <div><div style='color:#6b7280;font-size:0.75rem;font-weight:600;text-transform:uppercase;letter-spacing:0.06em;margin-bottom:0.15rem;'>Phone</div><div>{phone_html}{alt_phone_html}</div></div>
          </div>
          {tx_supplemental_html}
          <div style='margin-top:0.85rem;padding-top:0.75rem;border-top:1px solid #f0f2f5;
              font-size:0.75rem;color:#6b7280;'>PII — handle per CU confidentiality rules.</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    addr = _safe_str(row.get("address_line1")).strip()
    city = _safe_str(row.get("city_name"))
    state_code = _safe_str(row.get("state_code"))
    zip_code = _safe_str(row.get("zip_code"))
    addr_full = (f"{addr}  \n" if addr else "") + f"{city}, {state_code} {zip_code}"

    # Deep link to state Drinking Water Watch / Viewer (MS / OK / AL / TN / TX
    # — AR and LA still pending data-share requests).
    _render_state_dww_link(pwsid, state_code)

    st.markdown("**Mailing address**")
    st.write(addr_full)

    _render_service_area_context(pwsid)

    pws_type = _safe_str(row.get("pws_type_code"))
    src_code = _safe_str(row.get("primary_source_code"))
    own_code = _safe_str(row.get("owner_type_code"))

    c1, c2, c3 = st.columns(3)
    c1.metric("Population served", f"{_safe_int(row.get('population_served_count')):,}")
    c2.metric("Connections", f"{_safe_int(row.get('service_connections_count')):,}")
    c3.metric("System type", TYPE_LABELS.get(pws_type, pws_type or "—"))

    c4, c5, c6 = st.columns(3)
    c4.metric("Source", SOURCE_LABELS.get(src_code, src_code or "—"))
    c5.metric("Owner", OWNER_TYPE_LABELS.get(own_code, own_code or "—"))
    c6.metric("Status", "Active" if _safe_str(row.get("pws_activity_code")) == "A" else "Inactive")

    # Funding + sensitive-population flags — small chips when set. Grant
    # eligibility is a warm handoff to CU's Lending program; school/daycare
    # flags a sensitive population for prioritization.
    grant_elig = _safe_str(row.get("is_grant_eligible_ind")).upper() == "Y"
    school = _safe_str(row.get("is_school_or_daycare_ind")).upper() == "Y"
    chips = []
    if grant_elig:
        chips.append(
            "<span style='background:#e8f4ea;color:#1d6b34;border:1px solid #bfe0c6;"
            "padding:0.25rem 0.7rem;border-radius:999px;font-size:0.8rem;font-weight:600;'>"
            "✓ Grant-eligible · possible Lending handoff</span>"
        )
    if school:
        chips.append(
            "<span style='background:#fff4e0;color:#8a5a00;border:1px solid #f1d8a3;"
            "padding:0.25rem 0.7rem;border-radius:999px;font-size:0.8rem;font-weight:600;'>"
            "Serves a school or daycare</span>"
        )
    if chips:
        st.markdown(
            "<div style='display:flex;gap:0.5rem;flex-wrap:wrap;margin:0.75rem 0 0.25rem 0;'>"
            + "".join(chips) + "</div>",
            unsafe_allow_html=True,
        )

    st.markdown("**Violation history**")
    v = violations_df[violations_df["pwsid"] == pwsid].copy()
    if v.empty:
        st.info("No violations in the federal feed.")
    else:
        v["status"] = v["rtc_date"].apply(
            lambda d: "Returned to compliance" if pd.notna(d) else "Open"
        )
        v = v.sort_values("compl_per_begin_date", ascending=False)
        v_display = pd.DataFrame({
            "Begin": v["compl_per_begin_date"].apply(
                lambda d: d.strftime("%Y-%m-%d") if pd.notna(d) else "—"
            ).values,
            "Rule": v["rule_code"].apply(decode_rule).values,
            "Category": v["violation_category_code"].apply(decode_category).values,
            "Contaminant": v["contaminant_code"].apply(decode_contaminant).values,
            "Measured vs limit": [
                fmt_measure_vs_mcl(m, u, mcl)
                for m, u, mcl in zip(
                    v.get("viol_measure", pd.Series([None] * len(v))),
                    v.get("unit_of_measure", pd.Series([None] * len(v))),
                    v.get("state_mcl", pd.Series([None] * len(v))),
                )
            ],
            "Health-based": v["is_health_based_ind"].apply(
                lambda x: "Yes" if _safe_str(x) == "Y" else "No"
            ).values,
            "Urgency": v.get(
                "public_notification_tier", pd.Series([None] * len(v))
            ).apply(decode_tier).values,
            "Status": v["status"].values,
            "RTC date": v["rtc_date"].apply(
                lambda d: d.strftime("%Y-%m-%d") if pd.notna(d) else "—"
            ).values,
        })
        st.dataframe(
            v_display,
            width="stretch",
            hide_index=True,
            column_config={c: st.column_config.TextColumn(c) for c in v_display.columns},
        )

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
              font-size:1.1rem;
              font-weight:600;
              color:#085eaa;
              letter-spacing:-0.005em;
          '>{title}</div>
          {f"<div style='color:#6b7280;font-size:0.875rem;margin-top:0.15rem;'>{subtitle}</div>" if subtitle else ""}
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
            font-size: 0.875rem;
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

      /* ---------- Typography: Poppins everywhere ---------- */
      /* Cast wide and force inheritance so deep Streamlit/baseweb internals
         (tab labels, dialog titles, button text, dataframe cells, etc.) all
         resolve to Poppins. */
      html, body, .stApp, .stApp *,
      [data-baseweb] *, [data-testid] *,
      button, input, textarea, select, label, p, span, div, a,
      h1, h2, h3, h4, h5, h6 {
        font-family: 'Poppins', system-ui, sans-serif !important;
      }
      /* Restore Material Symbols icon glyphs that would otherwise be
         clobbered by the rule above. */
      [class*="material-symbols"], [class*="material-icons"],
      [class*="material-symbols"] *, [class*="material-icons"] *,
      .material-symbols-outlined, .material-symbols-rounded,
      .material-icons, span[role="img"][class*="icon"],
      [data-baseweb="icon"], [data-baseweb="icon"] *,
      [data-testid="stIcon"], [data-testid="stIcon"] *,
      [data-testid="stIconMaterial"], [data-testid="stIconMaterial"] *,
      [data-testid="stExpanderIcon"], [data-testid="stExpanderIcon"] *,
      [data-testid="stSelectboxVirtualDropdown"] svg *,
      span[translate="no"] {
        font-family: 'Material Symbols Outlined', 'Material Symbols Rounded',
                     'Material Icons' !important;
      }
      body, .stApp { color: #1f2933; }
      h1, h2, h3 { color: #085eaa; font-weight: 600; letter-spacing: -0.01em; }
      h1 { font-size: 2rem; }
      h2 { font-size: 1.35rem; margin-top: 0.5rem; }
      h3 { font-size: 1.1rem; }
      [data-testid="stCaptionContainer"], .stCaption {
        color: #6b7280 !important;
        font-size: 0.875rem !important;
      }
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
        color: #6b7280;
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
      [data-testid="stMetricDelta"] { font-size: 0.75rem !important; }

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
        color: #6b7280 !important;
        font-size: 0.875rem !important;
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


# Only read the columns the app actually uses — Envirofacts returns ~40 per table.
USECOLS = {
    "water_systems": [
        "pwsid", "pws_name", "primacy_agency_code", "city_name", "state_code",
        "zip_code", "address_line1", "pws_activity_code", "pws_type_code",
        "primary_source_code", "owner_type_code", "population_served_count",
        "service_connections_count", "admin_name", "org_name", "email_addr",
        "phone_number", "alt_phone_number", "pws_deactivation_date",
        "is_grant_eligible_ind", "is_school_or_daycare_ind",
    ],
    "violations": [
        "pwsid", "primacy_agency_code", "violation_id",
        "compl_per_begin_date", "compl_per_end_date", "rtc_date",
        "is_health_based_ind", "violation_category_code", "violation_code",
        "contaminant_code", "status",
        "rule_code", "viol_measure", "unit_of_measure", "state_mcl",
        "public_notification_tier", "is_major_viol_ind",
    ],
    "lcr_samples": [
        "pwsid", "primacy_agency_code", "sample_id",
        "sampling_end_date", "sampling_start_date",
    ],
    "geo": [
        "pwsid", "primacy_agency_code", "area_type_code", "county_served",
    ],
}


def _read_parquet_subset(path: Path, allowed: list[str]) -> pd.DataFrame:
    """Read a parquet file restricted to columns that exist on disk and are in `allowed`."""
    import pyarrow.parquet as pq
    available = set(pq.read_schema(path).names)
    cols = [c for c in allowed if c in available]
    return pd.read_parquet(path, columns=cols)


# Bump when USECOLS changes so st.cache_data (keyed on body + args, NOT on
# module globals like USECOLS) can't serve a frame built with an old column
# set. Included as an arg to load_from_parquet purely to participate in the
# cache key.
USECOLS_VERSION = 2


@st.cache_data(show_spinner="Loading water system data...", max_entries=2)
def load_from_parquet(states: tuple[str, ...], _usecols_version: int = USECOLS_VERSION) -> dict[str, pd.DataFrame]:
    """Load only the requested states. Bounded cache so toggling can't grow memory.

    `_usecols_version` participates in the cache key so changing USECOLS forces
    a reload instead of serving a stale frame missing the new columns.
    """
    name_map = {
        "systems": "water_systems",
        "violations": "violations",
        "lcr": "lcr_samples",
        "geo": "geo",
    }
    frames: dict[str, pd.DataFrame] = {}
    for key, fname in name_map.items():
        parts = [
            _read_parquet_subset(PARQUET_DIR / state / f"{fname}.parquet", USECOLS[fname])
            for state in states
        ]
        frame = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
        # Guarantee every requested column exists. _read_parquet_subset silently
        # drops columns absent on disk, so a schema-drifted parquet (or older
        # pull) would otherwise KeyError downstream. Missing -> NA column.
        for col in USECOLS[fname]:
            if col not in frame.columns:
                frame[col] = pd.NA
        frames[key] = frame
    _coerce_dates(frames)
    # Cast frequently-grouped string columns to category — large memory win.
    _to_category = {
        "systems": ["primacy_agency_code", "pws_activity_code", "pws_type_code",
                    "primary_source_code", "owner_type_code"],
        "violations": ["primacy_agency_code", "is_health_based_ind",
                       "violation_category_code", "violation_code"],
        "lcr": ["primacy_agency_code"],
        "geo": ["primacy_agency_code", "area_type_code"],
    }
    for key, cols in _to_category.items():
        df = frames[key]
        for c in cols:
            if c in df.columns:
                df[c] = df[c].astype("category")
    return frames


@st.cache_data(max_entries=1)
def load_tx_district_contacts() -> pd.DataFrame:
    """TX Water Districts fuzzy-matched to PWSID, with operator contacts.

    EPA SDWIS is the primary source for every system. This is supplemental:
    a best-effort name+county match against the TCEQ Texas Water Districts
    open dataset. Only systems with a match score above the script's
    threshold appear here.

    Returns an empty DataFrame with expected columns if the parquet
    hasn't been built (i.e., `enrich_tx_districts.py` hasn't been run).
    """
    cols = [
        "pwsid", "district_number", "district_name", "district_type",
        "contact_name", "contact_title", "phone", "address_full",
        "county", "match_score", "match_reason",
    ]
    if not TX_DISTRICT_CONTACTS_FILE.exists():
        return pd.DataFrame(columns=cols)
    df = pd.read_parquet(TX_DISTRICT_CONTACTS_FILE)
    df["pwsid"] = df["pwsid"].astype(str)
    return df


@st.cache_data(max_entries=1)
def load_county_context() -> pd.DataFrame:
    """Census ACS + USDA persistent-poverty context, keyed by fips5.

    Empty DataFrame with the expected columns if the parquet hasn't been built.
    """
    cols = [
        "fips5", "state", "county_display", "total_population",
        "poverty_rate", "median_hh_income",
        "high_poverty_current", "persistent_poverty",
    ]
    if not COUNTY_CONTEXT_FILE.exists():
        return pd.DataFrame(columns=cols)
    df = pd.read_parquet(COUNTY_CONTEXT_FILE)
    # Ensure consistent fips5 padding
    df["fips5"] = df["fips5"].astype(str).str.zfill(5)
    return df


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
    g = g.assign(county_served=g["county_served"].str.split(",")).explode("county_served")
    g["county_served"] = g["county_served"].str.strip()
    g["primacy_agency_code"] = g["primacy_agency_code"].astype(str)
    g["norm"] = g["county_served"].map(_normalize_county)
    alias_keys = list(COUNTY_NAME_ALIASES.keys())
    if alias_keys:
        alias_mask = pd.Series(
            list(zip(g["primacy_agency_code"], g["norm"]))
        ).isin(alias_keys).values
        if alias_mask.any():
            g.loc[alias_mask, "norm"] = [
                COUNTY_NAME_ALIASES[(s, n)]
                for s, n in zip(
                    g.loc[alias_mask, "primacy_agency_code"],
                    g.loc[alias_mask, "norm"],
                )
            ]
    lookup = load_fips_lookup()
    lookup_df = pd.DataFrame(
        [(s, n, fips, disp) for (s, n), (fips, disp) in lookup.items()],
        columns=["primacy_agency_code", "norm", "fips5", "county_display"],
    )
    g = g.merge(lookup_df, on=["primacy_agency_code", "norm"], how="left")
    g["county_display"] = g["county_display"].fillna(g["county_served"])
    return g.dropna(subset=["fips5"])[
        ["pwsid", "primacy_agency_code", "county_served", "county_display", "fips5"]
    ].reset_index(drop=True)


@st.cache_data(max_entries=4)
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

# --- State selector (top of page, persistent across tabs) -------------------
STATE_NAMES = {
    "AL": "Alabama",
    "AR": "Arkansas",
    "LA": "Louisiana",
    "MS": "Mississippi",
    "OK": "Oklahoma",
    "TN": "Tennessee",
    "TX": "Texas",
}
ALL_STATES_LABEL = "All CU states (7)"

state_options = [ALL_STATES_LABEL] + [
    f"{STATE_NAMES.get(s, s)} ({s})" for s in available
]
state_choice = st.session_state.get("state_choice", ALL_STATES_LABEL)
if state_choice not in state_options:
    state_choice = ALL_STATES_LABEL

if state_choice == ALL_STATES_LABEL:
    selected_states = list(available)
else:
    # Extract the 2-letter code from "Arkansas (AR)"
    code = state_choice.rsplit("(", 1)[-1].rstrip(")")
    selected_states = [code] if code in available else list(available)

data = load_from_parquet(tuple(selected_states))
manifests = load_manifests(tuple(selected_states))
pulled = sorted({m.get("pulled_at", "") for m in manifests.values()})
source_label = (
    f"EPA Envirofacts SDWIS · {', '.join(selected_states)} · "
    f"Last pull: {pulled[-1] if pulled else 'unknown'}"
)

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
            font-weight:700;
            letter-spacing:0.14em;
            color:#0088ce;
            text-transform:uppercase;
            margin-bottom:0.15rem;
        '>Communities Unlimited</div>
        <div style='
            font-size:2rem;
            font-weight:600;
            color:#085eaa;
            line-height:1.1;
            letter-spacing:-0.01em;
        '>Water Systems</div>
      </div>
      <div style='display:flex;gap:0.6rem;align-items:center;'>
        <span style='
            background:#ffffff;
            border:1px solid #d8e2ee;
            color:#1f2933;
            padding:0.35rem 0.7rem;
            border-radius:999px;
            font-size:0.75rem;
        '>
          <span style='color:#6b7280;'>Last pull</span>
          &nbsp;<span style='font-weight:600;'>{pull_short}</span>
        </span>
      </div>
    </div>
    <div style='
        color:#6b7280;
        font-size:0.875rem;
        margin-top:-0.5rem;
        margin-bottom:0.75rem;
    '>EPA Envirofacts SDWIS · Federal feed may lag state viewers by ~1 quarter.</div>
    """,
    unsafe_allow_html=True,
)

# State picker — single dropdown, persistent across tabs via session_state.
# Styled as a prominent "start here" control so new users orient quickly.
# We set key="state_picker_card" on the container so Streamlit adds a stable
# .st-key-state_picker_card class we can target — more reliable than :has().
st.markdown(
    """
    <style>
      /* Border + background go on the inner content block directly. This is
         the element Streamlit assigns .st-key-state_picker_card to, and it
         already has padding we can override. Putting the border here (not on
         the outer wrapper) avoids the wrapper:has() targeting going stale. */
      .st-key-state_picker_card {
        border: 3px solid #085eaa !important;
        border-radius: 12px !important;
        background: linear-gradient(180deg, #f4f9fd 0%, #ffffff 100%) !important;
        box-shadow: 0 2px 10px rgba(8,94,170,0.12) !important;
        padding: 1.25rem 1.5rem 1.5rem 1.5rem !important;
      }
      /* Also strip border off the outer wrapper so we don't double-paint. */
      [data-testid="stVerticalBlockBorderWrapper"]:has(.st-key-state_picker_card) {
        border: none !important;
        background: transparent !important;
        box-shadow: none !important;
        padding: 0 !important;
      }
      .state-picker-marker { display: none; }
      .state-picker-eyebrow {
        font-size: 0.72rem;
        font-weight: 700;
        letter-spacing: 0.14em;
        color: #085eaa;
        text-transform: uppercase;
        display: flex;
        align-items: center;
        gap: 0.5rem;
        margin: 0 0 0.25rem 0;
      }
      .state-picker-eyebrow::before {
        content: "1";
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 1.5rem;
        height: 1.5rem;
        background: #085eaa;
        color: #ffffff;
        border-radius: 999px;
        font-size: 0.82rem;
        font-weight: 700;
        letter-spacing: 0;
      }
      .state-picker-hint {
        font-size: 0.75rem;
        color: #6b7280;
        font-weight: 400;
        line-height: 1.3;
        margin: 0.35rem 0 0.6rem 0;
      }
    </style>
    """,
    unsafe_allow_html=True,
)
picker_col, _picker_spacer = st.columns([2, 3])
with picker_col:
    with st.container(border=True, key="state_picker_card"):
        st.markdown('<div class="state-picker-marker"></div>', unsafe_allow_html=True)
        st.markdown(
            '<div class="state-picker-eyebrow">Start here · choose a state</div>',
            unsafe_allow_html=True,
        )
        picked = st.selectbox(
            "Viewing",
            options=state_options,
            index=state_options.index(state_choice),
            key="state_choice_widget",
            label_visibility="collapsed",
            help="Selection is shared across all tabs.",
        )
        if picked != state_choice:
            st.session_state["state_choice"] = picked
            st.rerun()
        st.markdown(
            '<div class="state-picker-hint">'
            "Your pick filters every tab below. Switch states any time without losing your place."
            "</div>",
            unsafe_allow_html=True,
        )
st.session_state["state_choice"] = state_choice

st.caption(
    "Showing active community water systems. Contact details are PII — "
    "handle per CU confidentiality rules."
)

# Apply default scope: active community water systems only.
systems = active_cws(systems_all)
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
            st.session_state["pending_county_modal"] = back_county_fips
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

    _county_sorted = county_systems.sort_values(
        ["open_health_violations", "population_served_count"],
        ascending=[False, False],
    )

    def _fmt_county_int(v) -> str:
        return f"{int(v):,}" if pd.notna(v) else "—"

    table = pd.DataFrame({
        "PWSID": _county_sorted["pwsid"].astype(str).values,
        "System": _county_sorted["pws_name"].fillna("—").astype(str).values,
        "City": _county_sorted["city_name"].fillna("—").astype(str).values,
        "Population": _county_sorted["population_served_count"].apply(_fmt_county_int).values,
        "Connections": _county_sorted["service_connections_count"].apply(_fmt_county_int).values,
        "Open viols.": _county_sorted["open_health_violations"].apply(_fmt_county_int).values,
    })

    st.markdown(
        "<div style='font-size:0.875rem;color:#6b7280;margin:0.5rem 0 0.25rem 0;'>"
        "Check a row to open the system detail.</div>",
        unsafe_allow_html=True,
    )
    selection = st.dataframe(
        table,
        width="stretch",
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key=f"county_dialog_table_{fips}",
        column_config={col: st.column_config.TextColumn(col) for col in table.columns},
    )
    rows = (selection or {}).get("selection", {}).get("rows", [])
    if rows:
        chosen_pws = table.iloc[rows[0]]["PWSID"]
        st.session_state["open_system_modal"] = chosen_pws
        st.session_state["system_modal_back"] = fips
        st.rerun()


_deferred_system_open = None  # Used by the Find a System tab to open the detail.

# ---------------------------------------------------------------------------
# Pre-compute county-level aggregates so every tab can drive the county dialog.
# Done BEFORE any dialog opens so render_system_detail can reference geo_exp.
# ---------------------------------------------------------------------------
@st.cache_data(show_spinner=False, max_entries=2)
def _county_rollup(states_key: tuple[str, ...]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Returns (geo_exp, by_county) for the given state set. Cached by state set."""
    frames = load_from_parquet(states_key)
    sys_f = frames["systems"]
    vio_f = frames["violations"]
    geo_exp = explode_geo_to_counties(frames["geo"])

    active = sys_f[(sys_f["pws_activity_code"] == "A") & (sys_f["pws_type_code"] == "CWS")]
    active_pop = active[["pwsid", "population_served_count"]]
    attr = geo_exp.merge(active_pop, on="pwsid", how="inner")
    open_health = set(
        vio_f[(vio_f["rtc_date"].isna()) & (vio_f["is_health_based_ind"] == "Y")]["pwsid"]
    )
    attr["has_open_health"] = attr["pwsid"].isin(open_health).astype("int8")
    attr["is_small"] = (
        attr["population_served_count"] < SMALL_SYSTEM_THRESHOLD
    ).astype("int8")
    attr["small_open_health"] = (
        (attr["has_open_health"] == 1) & (attr["is_small"] == 1)
    ).astype("int8")
    rolled = attr.groupby(
        ["fips5", "county_display", "primacy_agency_code"], as_index=False, observed=True
    ).agg(
        systems=("pwsid", "nunique"),
        pop_served=("population_served_count", "sum"),
        with_open_health=("has_open_health", "sum"),
        small_with_open_health=("small_open_health", "sum"),
    )
    rolled["pct_open_health"] = (
        100 * rolled["with_open_health"] / rolled["systems"].clip(lower=1)
    )
    return geo_exp.reset_index(drop=True), rolled


geo_exp, by_county = _county_rollup(tuple(selected_states))

# Deferred global-search dialog (now that geo_exp exists for service-area context).
if _deferred_system_open:
    show_system_dialog(_deferred_system_open)

# Pending modal trigger from drill-down clicks
pending = st.session_state.pop("open_system_modal", None)
back_fips = st.session_state.pop("system_modal_back", None)
if pending:
    show_system_dialog(pending, back_county_fips=back_fips)

# Pending county-modal trigger (e.g. "Back to county" from a system dialog)
_pending_county = st.session_state.pop("pending_county_modal", None)
if _pending_county:
    show_county_dialog(_pending_county, by_county, geo_exp)


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
    tab_lcr,
    tab_violations,
) = st.tabs(
    [
        "Scorecard",
        "Find a System",
        "CU Watchlist",
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
    _parish_src = (
        by_county[by_county["primacy_agency_code"].isin(selected_states)]
        .sort_values("systems", ascending=False)
        .head(15)
        .reset_index(drop=True)
    )
    parish_rollup = pd.DataFrame({
        "County / parish": _parish_src["county_display"].astype(str).values,
        "State": _parish_src["primacy_agency_code"].astype(str).values,
        "Systems": _parish_src["systems"].apply(lambda v: f"{int(v):,}").values,
        "Open health-based": _parish_src["with_open_health"].apply(lambda v: f"{int(v):,}").values,
        "% open health-based": _parish_src["pct_open_health"].apply(lambda v: f"{v:.1f}").values,
        "_fips5": _parish_src["fips5"].astype(str).values,
    })
    sel_parish = st.dataframe(
        parish_rollup,
        width="stretch",
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        column_order=[c for c in parish_rollup.columns if c != "_fips5"],
        key="scorecard_parish_table",
        column_config={
            col: st.column_config.TextColumn(col) for col in parish_rollup.columns
        },
    )
    rows = (sel_parish or {}).get("selection", {}).get("rows", [])
    if rows:
        trigger_county_modal(
            parish_rollup.iloc[rows[0]]["_fips5"], "scorecard_parish_last"
        )


# --- CU Watchlist -------------------------------------------------------------
with tab_watchlist:
    section(
        "CU watchlist",
        "Small active community systems that look like technical-assistance candidates.",
    )
    lens = st.radio(
        "Watchlist lens",
        options=[
            "Health-based risk",
            "Capacity risk (monitoring & reporting)",
            "Both",
        ],
        horizontal=True,
        help=(
            "Health-based: open violations that are a direct health risk. "
            "Capacity risk: open monitoring & reporting violations — missed "
            "samples and late reports that signal a system struggling to keep "
            "up administratively, often before a health problem appears."
        ),
    )
    LENS_CAPTIONS = {
        "Health-based risk": (
            "Small active community systems with at least one open health-based "
            "violation. Sorted by population × open-violation count."
        ),
        "Capacity risk (monitoring & reporting)": (
            "Small active community systems with open monitoring & reporting "
            "violations — missed samples or late reports. These are capacity-"
            "support candidates: the system is struggling to keep up, which "
            "often precedes a health-based problem."
        ),
        "Both": (
            "Small active community systems with any open health-based OR "
            "monitoring & reporting violation. Sorted by population × open-"
            "violation count."
        ),
    }
    st.caption(LENS_CAPTIONS[lens])

    small_active = active_cws(systems_all)
    small_active = small_active[
        small_active["population_served_count"] < SMALL_SYSTEM_THRESHOLD
    ]

    open_v = violations_all[violations_all["rtc_date"].isna()]
    is_health = open_v["is_health_based_ind"] == "Y"
    is_mr = open_v["violation_category_code"].isin(MR_CATEGORIES)
    if lens == "Health-based risk":
        open_subset = open_v[is_health]
        count_label = "Open health-based viols."
    elif lens == "Capacity risk (monitoring & reporting)":
        open_subset = open_v[is_mr]
        count_label = "Open M/R viols."
    else:
        open_subset = open_v[is_health | is_mr]
        count_label = "Open viols. (health + M/R)"

    counts = (
        open_subset.groupby("pwsid")
        .size()
        .reset_index(name="open_violations")
    )

    watch = small_active.merge(counts, on="pwsid", how="inner")
    watch["priority_score"] = (
        watch["population_served_count"].fillna(0) * watch["open_violations"]
    )

    parish = (
        geo_all.dropna(subset=["county_served"])
        .drop_duplicates("pwsid")[["pwsid", "county_served"]]
    )
    if "county_served" in watch.columns:
        watch = watch.drop(columns=["county_served"])
    watch = watch.merge(parish, on="pwsid", how="left")

    watch_sorted = watch.sort_values("priority_score", ascending=False)

    def _fmt_int(v) -> str:
        return f"{int(v):,}" if pd.notna(v) else "—"

    watch_display = pd.DataFrame({
        "State": watch_sorted["primacy_agency_code"].astype(str).values,
        "PWSID": watch_sorted["pwsid"].astype(str).values,
        "System": watch_sorted["pws_name"].astype(str).values,
        "Parish / county": watch_sorted["county_served"].fillna("—").astype(str).values,
        "Population": watch_sorted["population_served_count"].apply(_fmt_int).values,
        "Connections": watch_sorted["service_connections_count"].apply(_fmt_int).values,
        count_label: watch_sorted["open_violations"].apply(_fmt_int).values,
        "Priority": watch_sorted["priority_score"].apply(_fmt_int).values,
    })

    st.metric("Systems on watchlist", f"{len(watch_display):,}")
    st.caption("Check a row to open the system detail.")
    _row_count = min(len(watch_display), 100)
    sel_watch = st.dataframe(
        watch_display.reset_index(drop=True),
        width="stretch",
        height=max(_row_count * 35 + 38, 200),
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key="watchlist_table",
        column_config={
            col: st.column_config.TextColumn(col) for col in watch_display.columns
        },
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
        "Pick a system below — the full record loads right beneath the dropdown. "
        "Switch states using the picker at the top of the page.",
    )

    pws_list = (
        systems[["pwsid", "pws_name"]]
        .dropna()
        .drop_duplicates("pwsid")
        .sort_values("pws_name")
    )
    if pws_list.empty:
        st.warning("No systems match the current selection. Try a different state at the top of the page.")
    else:
        pws_list["label"] = (
            pws_list["pwsid"].astype(str) + " — " + pws_list["pws_name"].astype(str)
        )
        label_map = dict(zip(pws_list["pwsid"], pws_list["label"]))
        options = pws_list["pwsid"].tolist()

        choice = st.selectbox(
            "**System**",
            options=options,
            index=None,
            placeholder=f"Type or scroll — {len(options):,} systems",
            format_func=lambda p: label_map.get(p, p),
            key="find_system_picker",
        )

        if choice:
            st.divider()
            render_system_detail(choice, systems_all, violations_all, lcr_all)


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

    section(
        "Lapsed sampling",
        "Active community systems whose most recent LCR sample is 3+ years old, "
        "or that have no sample on record at all. These are CU technical-assistance "
        "candidates — sampling gaps suggest capacity or compliance risk.",
    )

    lapse_years = st.slider(
        "Treat as lapsed when last sample is older than (years)",
        min_value=1, max_value=10, value=3, step=1,
    )
    include_never = st.checkbox(
        "Include systems with no sample on record",
        value=True,
        help=(
            "Some active systems have never appeared in the LCR sampling feed. "
            "That can mean they're newer, recently activated, or genuinely missing data."
        ),
    )

    active = active_cws(systems)[
        ["pwsid", "pws_name", "primacy_agency_code", "population_served_count"]
    ].drop_duplicates("pwsid")

    last_sample = (
        lcr.dropna(subset=["sampling_end_date"])
        .groupby("pwsid", as_index=False)["sampling_end_date"]
        .max()
        .rename(columns={"sampling_end_date": "last_sample"})
    )

    joined = active.merge(last_sample, on="pwsid", how="left")
    today = pd.Timestamp.today().normalize()
    cutoff = today - pd.DateOffset(years=lapse_years)
    joined["years_since"] = (today - joined["last_sample"]).dt.days / 365.25

    mask_lapsed = joined["last_sample"].notna() & (joined["last_sample"] < cutoff)
    mask_never = joined["last_sample"].isna()
    flagged = joined[mask_lapsed | (mask_never if include_never else False)].copy()
    flagged["status"] = flagged["last_sample"].apply(
        lambda d: "Never sampled" if pd.isna(d) else "Lapsed"
    )
    flagged = flagged.sort_values(
        ["status", "years_since", "population_served_count"],
        ascending=[True, False, False],
    )

    c1, c2, c3 = st.columns(3)
    c1.metric("Active CWS flagged", f"{len(flagged):,}")
    c2.metric("Lapsed (have past samples)", f"{int(mask_lapsed.sum()):,}")
    c3.metric(
        "Never sampled",
        f"{int(mask_never.sum()):,}" if include_never else "—",
    )

    display = pd.DataFrame({
        "PWSID": flagged["pwsid"].astype(str).values,
        "System": flagged["pws_name"].astype(str).values,
        "State": flagged["primacy_agency_code"].astype(str).values,
        "Population": flagged["population_served_count"].apply(
            lambda v: f"{int(v):,}" if pd.notna(v) else "—"
        ).values,
        "Last sample": flagged["last_sample"].apply(
            lambda d: d.strftime("%Y-%m-%d") if pd.notna(d) else "—"
        ).values,
        "Years since": flagged["years_since"].apply(
            lambda v: f"{v:.1f}" if pd.notna(v) else "—"
        ).values,
        "Status": flagged["status"].astype(str).values,
    })

    st.caption("Check a row to open the system detail.")
    _lcr_rows = min(len(display), 100)
    sel_lcr = st.dataframe(
        display.reset_index(drop=True),
        width="stretch",
        height=max(_lcr_rows * 35 + 38, 200),
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key="lcr_lapsed_table",
        column_config={
            col: st.column_config.TextColumn(col) for col in display.columns
        },
    )
    rows = (sel_lcr or {}).get("selection", {}).get("rows", [])
    if rows:
        trigger_system_modal(
            display.reset_index(drop=True).iloc[rows[0]]["PWSID"], "lcr_lapsed_last"
        )


# --- Violations ---------------------------------------------------------------
with tab_violations:
    section(
        "Violations browser",
        "Filter by rule, urgency, status, and severity across selected states.",
    )
    fc1, fc2 = st.columns(2)
    with fc1:
        health_only = st.checkbox("Health-based only", value=True)
        open_only = st.checkbox("Open only (no return-to-compliance date)", value=True)
    with fc2:
        tier1_only = st.checkbox(
            "Tier 1 (acute) only",
            value=False,
            help="Tier 1 = acute health risk requiring 24-hour public notice.",
        )
        exclude_mr = st.checkbox(
            "Exclude monitoring & reporting",
            value=False,
            help="Hide administrative M/R violations to focus on contaminant problems.",
        )

    v = violations.copy()
    if health_only:
        v = v[v["is_health_based_ind"] == "Y"]
    if open_only:
        v = v[v["rtc_date"].isna()]
    if tier1_only:
        v = v[v["public_notification_tier"].apply(
            lambda x: _safe_str(x).split(".")[0] == "1"
        )]
    if exclude_mr:
        v = v[~v["violation_category_code"].isin(MR_CATEGORIES)]

    # Rule filter — decode to plain names, let the user pick.
    if not v.empty:
        rule_opts = (
            v["rule_code"].apply(decode_rule).value_counts().index.tolist()
        )
        picked_rules = st.multiselect(
            "Filter by rule",
            options=rule_opts,
            default=[],
            placeholder="All rules",
        )
        if picked_rules:
            v = v[v["rule_code"].apply(decode_rule).isin(picked_rules)]

    tier1_count = int(
        v["public_notification_tier"].apply(
            lambda x: _safe_str(x).split(".")[0] == "1"
        ).sum()
    ) if not v.empty else 0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Violations", f"{len(v):,}")
    c2.metric("Distinct systems", f"{v['pwsid'].nunique():,}")
    c3.metric(
        "Tier 1 (acute)",
        f"{tier1_count:,}",
        delta="urgent" if tier1_count else None,
        delta_color="inverse",
    )
    c4.metric(
        "Median age (days)",
        f"{(pd.Timestamp.now() - v['compl_per_begin_date']).dt.days.median():.0f}"
        if not v.empty
        else "—",
    )

    if not v.empty:
        cat_counts = (
            v["violation_category_code"].apply(decode_category)
            .value_counts()
            .reset_index()
        )
        cat_counts.columns = ["Category", "Violations"]
        fig = px.bar(cat_counts, x="Category", y="Violations")
        fig.update_layout(height=320, margin=dict(l=0, r=0, t=10, b=0))
        st.plotly_chart(fig, width="stretch")

    v_joined = v.merge(systems[["pwsid", "pws_name"]], on="pwsid", how="left")
    v_sorted = v_joined.sort_values("compl_per_begin_date", ascending=False)

    def _fmt_date(d) -> str:
        return d.strftime("%Y-%m-%d") if pd.notna(d) else "—"

    v_display = pd.DataFrame({
        "PWSID": v_sorted["pwsid"].astype(str).values,
        "System": v_sorted["pws_name"].fillna("—").astype(str).values,
        "Begin": v_sorted["compl_per_begin_date"].apply(_fmt_date).values,
        "Rule": v_sorted["rule_code"].apply(decode_rule).values,
        "Category": v_sorted["violation_category_code"].apply(decode_category).values,
        "Contaminant": v_sorted["contaminant_code"].apply(decode_contaminant).values,
        "Measured vs limit": [
            fmt_measure_vs_mcl(m, u, mcl)
            for m, u, mcl in zip(
                v_sorted["viol_measure"], v_sorted["unit_of_measure"], v_sorted["state_mcl"]
            )
        ],
        "Health-based": v_sorted["is_health_based_ind"].apply(
            lambda x: "Yes" if _safe_str(x) == "Y" else "No"
        ).values,
        "Urgency": v_sorted["public_notification_tier"].apply(decode_tier).values,
        "RTC date": v_sorted["rtc_date"].apply(_fmt_date).values,
    })

    st.caption("Check a row to open the system detail.")
    _v_rows = min(len(v_display), 100)
    sel_v = st.dataframe(
        v_display.reset_index(drop=True),
        width="stretch",
        height=max(_v_rows * 35 + 38, 200),
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key="violations_table",
        column_config={col: st.column_config.TextColumn(col) for col in v_display.columns},
    )
    rows = (sel_v or {}).get("selection", {}).get("rows", [])
    if rows:
        trigger_system_modal(
            v_display.reset_index(drop=True).iloc[rows[0]]["PWSID"],
            "violations_last",
        )
