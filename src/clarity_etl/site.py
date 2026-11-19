"""Build the static dashboard: one index.html with the reporting marts embedded as JSON.

It needs no server, so GitHub Pages (or any static host, or a double-click) can show it.
The CI workflow rebuilds and publishes it after every pipeline run.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

import pandas as pd
from sqlalchemy import Engine


def _records(df: pd.DataFrame) -> list[dict]:
    df = df.copy()
    for col in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[col]):
            df[col] = df[col].dt.strftime("%Y-%m-%dT%H:%M")
        elif pd.api.types.is_float_dtype(df[col]):
            df[col] = df[col].round(4)
    return json.loads(df.to_json(orient="records"))


def snapshot(engine: Engine) -> dict:
    read = lambda name: pd.read_sql_table(name, engine)  # noqa: E731
    act = read("mart_daily_activity")
    act["contact_date"] = pd.to_datetime(act["contact_date"]).dt.strftime("%Y-%m-%d")
    fin = read("mart_finance_daily")
    fin["tx_post_date"] = pd.to_datetime(fin["tx_post_date"]).dt.strftime("%Y-%m-%d")
    den = read("mart_denials")
    den["remit_date"] = pd.to_datetime(den["remit_date"]).dt.strftime("%Y-%m-%d")
    dq = read("mart_dq_daily")
    dq["extract_date"] = pd.to_datetime(dq["extract_date"]).dt.strftime("%Y-%m-%d")
    dq_daily = dq.pivot_table(index="extract_date", columns="status", values="results",
                              aggfunc="sum", fill_value=0).reset_index()
    for s in ("pass", "warn", "fail"):
        if s not in dq_daily:
            dq_daily[s] = 0
    comp = read("mart_completeness_trend")
    comp["extract_date"] = pd.to_datetime(comp["extract_date"]).dt.strftime("%Y-%m-%d")
    fresh = read("mart_table_freshness")
    fresh["last_extract_date"] = pd.to_datetime(fresh["last_extract_date"]).dt.strftime("%Y-%m-%d")
    kpi = read("mart_revenue_cycle_kpis")
    inc = read("etl_incident").drop(columns=["report_path"], errors="ignore")
    return {
        "meta": {"as_of": pd.to_datetime(kpi["as_of"].iloc[0]).strftime("%Y-%m-%d"),
                 "built_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")},
        "activity": _records(act.rename(columns={"contact_date": "date", "loc_name": "location",
                                                 "department_name": "department"})
                             [["date", "location", "department", "completed", "scheduled", "no_shows",
                               "ed_visits", "admissions", "telehealth"]]),
        "finance": _records(fin.rename(columns={"tx_post_date": "date"})
                            .reindex(columns=["date", "fin_class", "charges", "payments",
                                              "contractual_adj", "denial_writeoff"]).fillna(0)),
        "ar": _records(read("mart_ar_aging").groupby(["fin_class", "aging_bucket"], as_index=False)
                       .agg(accounts=("accounts", "sum"), balance=("balance", "sum"))),
        "payers": _records(read("mart_payer_performance")),
        "denials": _records(den),
        "kpis": _records(kpi.drop(columns=["as_of"]))[0],
        "providers": _records(read("mart_provider_volume").dropna(subset=["prov_name"])
                              .sort_values("completed_visits", ascending=False).head(10)),
        "dq": _records(dq_daily[["extract_date", "pass", "warn", "fail"]].rename(
            columns={"extract_date": "date"})),
        "completeness": _records(comp),
        "freshness": _records(fresh),
        "runs": _records(read("mart_pipeline_runs").fillna({"message": ""})),
        "incidents": _records(inc),
    }


def build_site(engine: Engine, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    template = files("clarity_etl").joinpath("site_template.html").read_text(encoding="utf-8")
    data = json.dumps(snapshot(engine), separators=(",", ":")).replace("</", "<\\/")
    page = out_dir / "index.html"
    page.write_text(template.replace("__DATA__", data), encoding="utf-8")
    (out_dir / ".nojekyll").write_text("")
    return page
