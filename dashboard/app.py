"""Cedar Valley Health: operations, finance, data and pipeline dashboard.

Reads only the reporting marts (mart_*) and the incident log, so it works on any warehouse
the pipeline loads. Run:  uv run streamlit run dashboard/app.py
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from sqlalchemy import create_engine, inspect

ROOT = Path(__file__).resolve().parents[1]
DB_URL = os.environ.get("CLARITY_ETL_DB_URL", f"sqlite:///{ROOT / 'data' / 'warehouse.db'}")

# Reference palette (validated categorical order) and reserved status colors.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
MUTED = "#b9b8b2"
GOOD, WARNING, CRITICAL = "#0ca30c", "#fab219", "#d03b3b"
INK, INK_2, SURFACE, GRID = "#0b0b0b", "#52514e", "#fcfcfb", "#ecebe7"
FIN_ORDER = ["Commercial", "Medicare", "Medicaid", "Self-Pay", "Workers Comp"]

st.set_page_config(page_title="Cedar Valley Health · Clarity Dashboard", layout="wide")
st.markdown(f"""<style>
  .block-container {{padding-top: 1.6rem; max-width: 1400px;}}
  [data-testid="stMetricValue"] {{font-size: 1.6rem; color: {INK};}}
  [data-testid="stMetricLabel"] p {{color: {INK_2}; font-size: 0.85rem;}}
  h3 {{margin-top: 0.6rem; font-size: 1.05rem !important;}}
</style>""", unsafe_allow_html=True)


# ---- data -----------------------------------------------------------------------------------
@st.cache_data(ttl=300)
def load() -> dict[str, pd.DataFrame]:
    engine = create_engine(DB_URL)
    names = set(inspect(engine).get_table_names())
    if "mart_daily_activity" not in names:
        return {}
    marts = {n: pd.read_sql_table(n, engine) for n in sorted(names) if n.startswith("mart_")}
    marts["incidents"] = pd.read_sql_table("etl_incident", engine) if "etl_incident" in names \
        else pd.DataFrame()
    return marts


def money(x: float) -> str:
    if pd.isna(x):
        return "–"
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(x) >= div:
            return f"${x / div:,.1f}{unit}"
    return f"${x:,.0f}"


def payer(names: pd.Series) -> pd.Series:
    out = names.str.title()
    for a, b in (("Hmo", "HMO"), ("Ppo", "PPO"), ("Part A/B", "Part A/B")):
        out = out.str.replace(a, b)
    return out


def dept(names: pd.Index | pd.Series) -> pd.Index | pd.Series:
    return names.str.title().str.replace("Cvmc", "CVMC").str.replace("Ob/Gyn", "OB/GYN")


def hbar(values: pd.Series, labels, color: str, text: list[str], hover: str) -> go.Figure:
    """Horizontal bar with direct value labels and room on the right so they never clip."""
    fig = go.Figure(go.Bar(x=values, y=labels, orientation="h", marker_color=color, text=text,
                           textposition="outside", textfont_color=INK_2, cliponaxis=False,
                           hovertemplate=hover))
    fig.update_xaxes(range=[0, float(values.max()) * 1.18])
    return style(fig, legend=False)


def pct(x: float, digits: int = 1) -> str:
    return "–" if pd.isna(x) else f"{x * 100:.{digits}f}%"


def style(fig: go.Figure, height: int = 320, legend: bool = True) -> go.Figure:
    fig.update_layout(
        height=height, margin=dict(l=8, r=8, t=8, b=8), plot_bgcolor=SURFACE,
        paper_bgcolor=SURFACE, font=dict(family="Inter, system-ui, sans-serif", size=13, color=INK),
        showlegend=legend, legend=dict(orientation="h", y=1.08, x=0, title=None),
        hoverlabel=dict(bgcolor="white", font_color=INK), barcornerradius=4, bargap=0.25)
    fig.update_xaxes(showgrid=False, linecolor="#d9d8d3", tickfont_color=INK_2)
    fig.update_yaxes(gridcolor=GRID, zeroline=False, tickfont_color=INK_2)
    return fig


def show(fig: go.Figure) -> None:
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})


m = load()
st.title("Cedar Valley Health · Clarity reporting")
if not m:
    st.warning("No reporting marts found. Run `./scripts/demo.sh` (or `clarity-etl backfill`) first.")
    st.stop()

act = m["mart_daily_activity"].copy()
act["contact_date"] = pd.to_datetime(act["contact_date"])
fin = m["mart_finance_daily"].copy()
fin["tx_post_date"] = pd.to_datetime(fin["tx_post_date"])
kpi = m["mart_revenue_cycle_kpis"].iloc[0]
runs = m["mart_pipeline_runs"].copy()
runs["started_at"] = pd.to_datetime(runs["started_at"])
inc = m["incidents"]
as_of = pd.to_datetime(kpi["as_of"])

st.caption(f"Synthetic Epic Clarity data for a fictional health system · data through "
           f"**{as_of:%b %d, %Y}** · last pipeline run {runs['started_at'].max():%b %d %H:%M}")

# Filters: one row above the charts.
f1, f2, _ = st.columns([2, 3, 3])
min_d, max_d = act["contact_date"].min().date(), act["contact_date"].max().date()
default_start = max(min_d, (as_of - pd.Timedelta(days=29)).date())
dates = f1.date_input("Date range", (default_start, max_d), min_value=min_d, max_value=max_d)
start, end = (pd.Timestamp(dates[0]), pd.Timestamp(dates[1])) if len(dates) == 2 \
    else (pd.Timestamp(default_start), pd.Timestamp(max_d))
locations = sorted(act["loc_name"].unique())
picked = f2.multiselect("Locations", locations, default=locations)
span = end - start + pd.Timedelta(days=1)
prev_start, prev_end = start - span, start - pd.Timedelta(days=1)

a_all = act[act["loc_name"].isin(picked)]
a = a_all[a_all["contact_date"].between(start, end)]
a_prev = a_all[a_all["contact_date"].between(prev_start, prev_end)]



def delta(cur: float, prev: float) -> str | None:
    return None if not prev else f"{(cur - prev) / prev * 100:+.1f}% vs prior period"


# ---- executive ------------------------------------------------------------------------------
def render_executive() -> None:
    done, done_prev = a["completed"].sum(), a_prev["completed"].sum()
    sched, sched_prev = a["scheduled"].sum(), a_prev["scheduled"].sum()
    noshow = a["no_shows"].sum() / sched if sched else float("nan")
    noshow_prev = a_prev["no_shows"].sum() / sched_prev if sched_prev else None
    c = st.columns(6)
    c[0].metric("Completed visits", f"{done:,}", delta(done, done_prev))
    c[1].metric("Avg visits / day", f"{done / span.days:,.0f}")
    c[2].metric("No-show rate", pct(noshow),
                None if noshow_prev is None else f"{(noshow - noshow_prev) * 100:+.1f} pts",
                delta_color="inverse")
    ed, ed_prev = a["ed_visits"].sum(), a_prev["ed_visits"].sum()
    adm, adm_prev = a["admissions"].sum(), a_prev["admissions"].sum()
    c[3].metric("ED visits", f"{ed:,}", delta(ed, ed_prev), delta_color="off")
    c[4].metric("Admissions", f"{adm:,}", delta(adm, adm_prev), delta_color="off")
    c[5].metric("Telehealth share", pct(a["telehealth"].sum() / done if done else float("nan")))

    left, right = st.columns([3, 2])
    with left:
        st.subheader("Are visit volumes holding up?")
        daily = a.groupby("contact_date")["completed"].sum()
        roll = daily.rolling(7, min_periods=1).mean()
        fig = go.Figure([
            go.Bar(x=daily.index, y=daily.values, name="Daily completed visits", marker_color="#cde2fb",
                   hovertemplate="%{x|%a %b %d}: %{y:,} visits<extra></extra>"),
            go.Scatter(x=roll.index, y=roll.values, name="7-day average", mode="lines",
                       line=dict(color=SERIES[0], width=2),
                       hovertemplate="7-day avg: %{y:,.0f}<extra></extra>")])
        show(style(fig).update_layout(hovermode="x unified", bargap=0.1))
    with right:
        st.subheader("Which departments carry the load?")
        by_dep = a.groupby("department_name")["completed"].sum().sort_values()
        show(hbar(by_dep, dept(by_dep.index), SERIES[0], [f"{v:,}" for v in by_dep.values],
                  "%{y}: %{x:,} visits<extra></extra>"))

    left, right = st.columns([2, 3])
    with left:
        st.subheader("Where do patients miss appointments?")
        d = a.groupby("department_name")[["no_shows", "scheduled"]].sum()
        d = d[d["scheduled"] >= 30]
        rate = (d["no_shows"] / d["scheduled"]).sort_values()
        show(hbar(rate, dept(rate.index), SERIES[0], [pct(v) for v in rate.values],
                  "%{y}: %{x:.1%} no-show<extra></extra>").update_xaxes(tickformat=".0%"))
    with right:
        st.subheader("Busiest providers")
        pv = m["mart_provider_volume"].dropna(subset=["prov_name"]) \
            .sort_values("completed_visits", ascending=False).head(10)
        st.dataframe(pv.rename(columns={
            "department_name": "Department", "prov_name": "Provider", "prov_type": "Type",
            "completed_visits": "Visits (all time)", "unique_patients": "Patients"}),
            hide_index=True, use_container_width=True)

# ---- finance --------------------------------------------------------------------------------
def render_finance() -> None:
    f = fin[fin["tx_post_date"].between(start, end)]
    fp = fin[fin["tx_post_date"].between(prev_start, prev_end)]
    c = st.columns(7)
    chg, chg_prev = f["charges"].sum(), fp["charges"].sum()
    pay, pay_prev = f["payments"].sum(), fp["payments"].sum()
    c[0].metric("Gross charges", money(chg), delta(chg, chg_prev))
    c[1].metric("Payments", money(pay), delta(pay, pay_prev))
    c[2].metric("Total A/R", money(kpi["total_ar"]))
    c[3].metric("Days in A/R", f"{kpi['days_in_ar']:.1f}")
    c[4].metric("DNFB (not yet billed)", money(kpi["dnfb_amount"]), f"{int(kpi['dnfb_accounts']):,} accounts",
                delta_color="off")
    c[5].metric("First-pass denial rate", pct(kpi["first_pass_denial_rate"]))
    c[6].metric("Avg days to bill", f"{kpi['avg_days_to_bill']:.1f}")
    st.caption("A/R, DNFB, denial rate and days-to-bill are a snapshot as of the latest data; "
               "charges and payments follow the date range.")

    left, right = st.columns(2)
    with left:
        st.subheader("How old is our A/R, and who owes it?")
        ar = m["mart_ar_aging"].groupby(["aging_bucket", "fin_class"], as_index=False)["balance"].sum()
        fig = go.Figure()
        for i, fc in enumerate([x for x in FIN_ORDER if x in set(ar["fin_class"])]):
            s = ar[ar["fin_class"] == fc].set_index("aging_bucket").reindex(
                ["0-30", "31-60", "61-90", "90+"])["balance"].fillna(0)
            fig.add_bar(x=s.index, y=s.values, name=fc, marker_color=SERIES[i],
                        marker_line=dict(color=SURFACE, width=2),
                        hovertemplate=f"{fc} · %{{x}} days: %{{y:$,.0f}}<extra></extra>")
        fig.update_layout(barmode="stack", legend_traceorder="normal")
        show(style(fig).update_yaxes(tickprefix="$", tickformat="~s")
             .update_xaxes(title="Days since discharge"))
    with right:
        st.subheader("Are collections keeping pace with charges?")
        wk = f.set_index("tx_post_date")[["charges", "payments"]].resample("W-MON", label="left",
                                                                          closed="left").sum()
        # keep full weeks only, so partial weeks at the edges don't look like a collapse
        wk = wk[(wk.index >= start) & (wk.index + pd.Timedelta(days=6) <= end)]
        fig = go.Figure()
        for i, (col, label) in enumerate([("charges", "Charges"), ("payments", "Payments")]):
            fig.add_scatter(x=wk.index, y=wk[col], name=label, mode="lines+markers",
                            line=dict(color=SERIES[i], width=2), marker=dict(size=8),
                            hovertemplate=f"{label}: %{{y:$,.0f}}<extra></extra>")
        show(style(fig).update_layout(hovermode="x unified")
             .update_yaxes(tickprefix="$", tickformat="~s", rangemode="tozero")
             .update_xaxes(title="Week starting"))

    left, right = st.columns([3, 2])
    with left:
        st.subheader("Which payers deny most and pay slowest?")
        pp = m["mart_payer_performance"].copy().sort_values("denial_rate", ascending=False)
        pp["payor_name"] = payer(pp["payor_name"])
        st.dataframe(
            pp[["payor_name", "fin_class", "accounts_billed", "denial_rate", "avg_days_to_pay",
                "open_balance"]],
            hide_index=True, use_container_width=True,
            column_config={
                "payor_name": "Payer", "fin_class": "Class", "accounts_billed": "Accounts billed",
                "denial_rate": st.column_config.ProgressColumn(
                    "First-pass denial rate", format="percent", min_value=0, max_value=0.2),
                "avg_days_to_pay": st.column_config.NumberColumn("Avg days to pay", format="%.1f"),
                "open_balance": st.column_config.NumberColumn("Open balance", format="$%,.0f"),
            })
    with right:
        st.subheader("Why are claims denied?")
        den = m["mart_denials"].copy()
        den["remit_date"] = pd.to_datetime(den["remit_date"])
        den = den[den["remit_date"].between(start, end)]
        reasons = den.groupby(["carc_code", "carc_desc"], as_index=False) \
            .agg(denials=("denials", "sum"), amount=("denied_amount", "sum")).sort_values("denials")
        fig = hbar(reasons["denials"], reasons["carc_code"] + "  " + reasons["carc_desc"], SERIES[0],
                   [str(n) for n in reasons["denials"]],
                   "%{y}<br>%{x} denials · %{customdata:$,.0f} billed<extra></extra>")
        fig.update_traces(customdata=reasons["amount"])
        show(fig)

# ---- data quality ---------------------------------------------------------------------------
def render_data_quality() -> None:
    dq = m["mart_dq_daily"].copy()
    dq["extract_date"] = pd.to_datetime(dq["extract_date"])
    by = dq.pivot_table(index="extract_date", columns="status", values="results", aggfunc="sum",
                        fill_value=0)
    for col in ("pass", "warn", "fail"):
        if col not in by:
            by[col] = 0
    total = by.sum().sum()
    fresh = m["mart_table_freshness"].copy()
    fresh["last_extract_date"] = pd.to_datetime(fresh["last_extract_date"])
    latest = fresh["last_extract_date"].max()
    c = st.columns(5)
    c[0].metric("Checks run (all nights)", f"{int(total):,}")
    c[1].metric("Pass rate", pct(by["pass"].sum() / total, 2))
    c[2].metric("Warnings", f"{int(by['warn'].sum()):,}")
    c[3].metric("Blocking failures", f"{int(by['fail'].sum()):,}")
    daily_tables = fresh[fresh["files_loaded"] > 1]
    current = int((daily_tables["last_extract_date"] == latest).sum())
    c[4].metric("Daily tables current", f"{current} / {len(daily_tables)}")

    left, right = st.columns([3, 2])
    with left:
        st.subheader("When did checks warn or block a load?")
        fig = go.Figure([
            go.Bar(x=by.index, y=by["warn"], name="⚠ Warning", marker_color=WARNING,
                   hovertemplate="%{x|%b %d}: %{y} warning(s)<extra></extra>"),
            go.Bar(x=by.index, y=by["fail"], name="✖ Blocking failure", marker_color=CRITICAL,
                   hovertemplate="%{x|%b %d}: %{y} failure(s)<extra></extra>")])
        show(style(fig).update_layout(barmode="stack", bargap=0.15).update_yaxes(dtick=1))
        st.caption("Each night runs ~80 checks: checksum, row count, schema, types, duplicate "
                   "keys, required fields, foreign keys, balance reconciliation, freshness and "
                   "volume. A failure stops the load; a warning is logged and the load continues.")
    with right:
        st.subheader("Is each table fresh?")
        fresh["Status"] = [
            "✔ current" if d == latest or n <= 1 else f"⚠ {(latest - d).days} day(s) behind"
            for d, n in zip(fresh["last_extract_date"], fresh["files_loaded"], strict=True)]
        st.dataframe(fresh.rename(columns={
            "table_name": "Clarity table", "warehouse_rows": "Rows", "last_extract_date": "Last extract",
            "files_loaded": "Files loaded"}), hide_index=True, use_container_width=True,
            column_config={"Last extract": st.column_config.DateColumn(format="MMM D, YYYY")})

    st.subheader("Are blank values creeping up?")
    comp = m["mart_completeness_trend"].copy()
    comp["extract_date"] = pd.to_datetime(comp["extract_date"])
    cols = st.columns(2)
    for slot, ((table, column), g) in zip(cols, comp.groupby(["table_name", "column_name"]), strict=False):
        with slot:
            g = g.sort_values("extract_date")
            avg = g["metric"].rolling(7, min_periods=1).mean()
            fig = go.Figure([
                go.Scatter(x=g["extract_date"], y=g["metric"], mode="markers", name="Nightly file",
                           marker=dict(size=8, color=MUTED),
                           hovertemplate="%{x|%b %d}: %{y:.1%} blank<extra></extra>"),
                go.Scatter(x=g["extract_date"], y=avg, mode="lines", name="7-night average",
                           line=dict(color=SERIES[0], width=2),
                           hovertemplate="7-night avg: %{y:.2%}<extra></extra>"),
                go.Scatter(x=g["extract_date"], y=g["threshold"], mode="lines", name="Warning limit",
                           line=dict(color=INK_2, width=1, dash="dash"), hoverinfo="skip")])
            st.markdown(f"**{table}.{column}**: share of rows left blank")
            show(style(fig, height=240).update_yaxes(tickformat=".0%", rangemode="tozero"))

# ---- IT & pipeline --------------------------------------------------------------------------
def render_it() -> None:
    ok = runs["status"].eq("success")
    healed = inc[(inc["status"] == "resolved") & (inc["needs_human_approval"] == 0)] if len(inc) else inc
    open_inc = inc[inc["status"] != "resolved"] if len(inc) else inc
    c = st.columns(6)
    c[0].metric("Pipeline runs", f"{len(runs):,}")
    c[1].metric("Success rate", pct(ok.mean()))
    c[2].metric("Rows loaded", f"{int(runs['rows_loaded'].sum()):,}")
    c[3].metric("Avg run time", f"{runs.loc[ok & runs['rows_loaded'].gt(0), 'duration_sec'].mean():.1f}s")
    c[4].metric("Incidents auto-healed", f"{len(healed)} of {len(inc)}")
    c[5].metric("Open incidents", f"{len(open_inc)}")

    st.subheader("Did every nightly run land?")
    rr = runs[runs["started_at"].between(start, end + pd.Timedelta(days=2))]
    good, bad = rr[rr["status"] != "failed"], rr[rr["status"] == "failed"]
    hover = ("Run %{customdata[0]} (%{customdata[1]})<br>%{x|%b %d %H:%M}<br>"
             "%{customdata[3]:,} rows<br>%{customdata[2]}<extra></extra>")
    fig = go.Figure([
        go.Bar(x=good["started_at"], y=good["rows_loaded"], name="✔ Run loaded", marker_color=GOOD,
               customdata=good[["run_id", "trigger_type", "message", "rows_loaded"]].fillna(""),
               hovertemplate=hover),
        go.Scatter(x=bad["started_at"], y=[0] * len(bad), mode="markers",
                   name="✖ Run failed (loaded nothing)",
                   marker=dict(symbol="x", size=14, color=CRITICAL, line=dict(width=0)),
                   customdata=bad[["run_id", "trigger_type", "message", "rows_loaded"]].fillna(""),
                   hovertemplate=hover, cliponaxis=False)])
    show(style(fig, height=280).update_layout(bargap=0.3).update_yaxes(title="Rows loaded"))
    st.caption("Runs in the selected date range. Each night loads all-or-nothing; an agent retry "
               "shows as a second run on the same night.")

    st.subheader("What broke, and what did the agent do?")
    if not len(inc):
        st.info("No incidents yet.")
    for _, r in inc.sort_values("incident_id", ascending=False).iterrows():
        icon = "✔" if r["status"] == "resolved" else "⚠"
        who = "Investigation agent"
        with st.expander(f"{icon} #{int(r['incident_id'])} · {r['title']} · {r['severity']} · "
                         f"{r['status'].replace('_', ' ')}"):
            meta = (f"**Detected** {pd.to_datetime(r['created_at']):%b %d %H:%M} · "
                    f"**Investigated by** {who} · **Human approval needed** "
                    f"{'yes' if r['needs_human_approval'] else 'no'}")
            if pd.notna(r["resolved_at"]):
                meta += f" · **Resolved** {pd.to_datetime(r['resolved_at']):%b %d %H:%M}"
            st.markdown(meta)
            st.markdown(f"**What happened.** {r['root_cause']}")
            st.markdown(f"**Impact.** {r['impact']}")
            if r["actions_taken"]:
                steps = "\n".join(f"- {x}" for x in r["actions_taken"].split("\n"))
                st.markdown("**Agent actions.**\n" + steps)
            st.markdown(f"**Recommended fix.** {r['recommended_fix']}")


SECTIONS = {"executive": ("Executive", render_executive), "finance": ("Finance", render_finance),
            "data-quality": ("Data quality", render_data_quality),
            "it": ("IT & pipeline", render_it)}
view = st.query_params.get("view")
if view in SECTIONS:  # ?view=finance shows one section on its own (handy for screenshots)
    SECTIONS[view][1]()
else:
    for tab, (_, render) in zip(st.tabs([label for label, _ in SECTIONS.values()]),
                                SECTIONS.values(), strict=True):
        with tab:
            render()
