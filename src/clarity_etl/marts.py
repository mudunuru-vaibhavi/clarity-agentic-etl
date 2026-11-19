"""Reporting marts: small, dashboard-ready tables built from the warehouse.

Built with pandas so the same code runs on SQLite, SQL Server and Postgres. Rebuilt in
full after every successful run (the data is small; a POC doesn't need incremental marts).

  Executive  mart_daily_activity, mart_provider_volume
  Finance    mart_finance_daily, mart_ar_aging, mart_payer_performance, mart_denials,
             mart_revenue_cycle_kpis
  Data       mart_dq_daily, mart_completeness_trend, mart_table_freshness
  IT         mart_pipeline_runs (incidents come straight from etl_incident)
"""

from __future__ import annotations

import pandas as pd
from sqlalchemy import Engine

from .schema_registry import LOAD_ORDER


def _read(engine: Engine, table: str) -> pd.DataFrame:
    return pd.read_sql_table(table, engine)


def _bucket(days: pd.Series) -> pd.Series:
    return pd.cut(days, bins=[-1, 30, 60, 90, 10_000],
                  labels=["0-30", "31-60", "61-90", "90+"]).astype(str)


def build_marts(engine: Engine) -> dict[str, int]:
    enc = _read(engine, "clarity_pat_enc")
    dep = _read(engine, "clarity_clarity_dep")
    ser = _read(engine, "clarity_clarity_ser")
    acct = _read(engine, "clarity_hsp_account")
    tx = _read(engine, "clarity_hsp_transactions")
    remit = _read(engine, "clarity_cl_remit")
    epm = _read(engine, "clarity_clarity_epm")
    fin = _read(engine, "clarity_zc_fin_class").rename(columns={"name": "fin_class"})
    for df, cols in ((acct, ["tot_chgs", "tot_pmts", "tot_adj", "tot_acct_bal"]),
                     (tx, ["tx_amount"]), (remit, ["billed_amt", "paid_amt"])):
        df[cols] = df[cols].astype(float)
    as_of = pd.to_datetime(enc["contact_date"]).max()
    marts: dict[str, pd.DataFrame] = {}

    # ---- executive --------------------------------------------------------------------------
    e = enc.merge(dep, on="department_id", how="left")
    e["contact_date"] = pd.to_datetime(e["contact_date"])
    e["completed"] = e["appt_status_c"] == 2
    marts["mart_daily_activity"] = (
        e.assign(
            scheduled=e["enc_type_c"].isin([101, 76]) | (e["appt_status_c"] != 2),
            no_show=e["appt_status_c"] == 4, canceled=e["appt_status_c"] == 3,
            ed_visit=(e["department_id"] == 10101) & e["completed"],
            admission=(e["enc_type_c"] == 3) & e["completed"],
            telehealth=(e["enc_type_c"] == 76) & e["completed"],
            los_hours=(pd.to_datetime(e["checkout_time"]) - pd.to_datetime(e["checkin_time"]))
            .dt.total_seconds() / 3600)
        .groupby(["contact_date", "loc_name", "department_name", "specialty"], as_index=False)
        .agg(encounters=("pat_enc_csn_id", "count"), completed=("completed", "sum"),
             scheduled=("scheduled", "sum"), no_shows=("no_show", "sum"),
             canceled=("canceled", "sum"), ed_visits=("ed_visit", "sum"),
             admissions=("admission", "sum"), telehealth=("telehealth", "sum"),
             avg_visit_hours=("los_hours", "mean"))
    )
    marts["mart_provider_volume"] = (
        e[e["completed"]].merge(ser[["prov_id", "prov_name", "prov_type"]],
                                left_on="visit_prov_id", right_on="prov_id", how="left")
        .groupby(["department_name", "prov_name", "prov_type"], as_index=False)
        .agg(completed_visits=("pat_enc_csn_id", "count"), unique_patients=("pat_id", "nunique"))
    )

    # ---- finance ----------------------------------------------------------------------------
    a = acct.merge(fin, left_on="acct_fin_class_c", right_on="fin_class_c", how="left") \
            .merge(epm[["payor_id", "payor_name"]], left_on="primary_payor_id",
                   right_on="payor_id", how="left")
    t = tx.merge(a[["hsp_account_id", "fin_class", "payor_name"]], on="hsp_account_id", how="left")
    t["tx_post_date"] = pd.to_datetime(t["tx_post_date"])
    reason = t["adj_reason"].fillna("")
    t["bucket"] = t["tx_type"].where(t["tx_type"] != "Adjustment",
                                     "Adj: " + reason.str.replace(r" CO-\d+$", "", regex=True))
    fd = t.pivot_table(index=["tx_post_date", "fin_class"], columns="bucket", values="tx_amount",
                       aggfunc="sum", fill_value=0).reset_index()
    fd.columns.name = None
    fd = fd.rename(columns={"Charge": "charges", "Payment": "payments",
                            "Adj: CONTRACTUAL": "contractual_adj",
                            "Adj: DENIAL WRITE-OFF": "denial_writeoff",
                            "Adj: SELF-PAY DISCOUNT": "self_pay_discount"})
    for col in ("payments", "contractual_adj", "denial_writeoff", "self_pay_discount"):
        if col in fd:
            fd[col] = -fd[col]  # report money in as positive numbers
    marts["mart_finance_daily"] = fd

    open_ar = a[a["tot_acct_bal"] > 0.005].copy()
    start = pd.to_datetime(open_ar["disch_date_time"]).fillna(pd.to_datetime(open_ar["adm_date_time"]))
    open_ar["age_days"] = (as_of - start.dt.normalize()).dt.days.clip(lower=0)
    open_ar["aging_bucket"] = _bucket(open_ar["age_days"])
    open_ar["billed"] = open_ar["bill_drop_date"].notna()
    marts["mart_ar_aging"] = (
        open_ar.groupby(["fin_class", "payor_name", "aging_bucket", "billed"], as_index=False)
        .agg(accounts=("hsp_account_id", "count"), balance=("tot_acct_bal", "sum"))
        .assign(as_of=as_of)
    )

    r = remit.merge(epm[["payor_id", "payor_name"]], on="payor_id", how="left")
    r["remit_date"] = pd.to_datetime(r["remit_date"])
    first_remit = r.sort_values("remit_date").drop_duplicates("hsp_account_id")
    lag = first_remit.merge(a[["hsp_account_id", "bill_drop_date"]], on="hsp_account_id")
    lag["days_to_pay"] = (lag["remit_date"] - pd.to_datetime(lag["bill_drop_date"])).dt.days
    billed = a[a["bill_drop_date"].notna()]
    pp = (billed.groupby(["payor_name", "fin_class"], as_index=False)
          .agg(accounts_billed=("hsp_account_id", "count"), charges=("tot_chgs", "sum"),
               payments=("tot_pmts", "sum"), open_balance=("tot_acct_bal", "sum")))
    rem = (first_remit.groupby("payor_name", as_index=False)
           .agg(first_pass_remits=("remit_id", "count"),
                denials=("denied_yn", lambda s: (s == "Y").sum())))
    days = lag.groupby("payor_name", as_index=False).agg(avg_days_to_pay=("days_to_pay", "mean"))
    pp = pp.merge(rem, on="payor_name", how="left").merge(days, on="payor_name", how="left")
    # With a short history no claim may be paid yet: empty groups arrive untyped.
    for col in ("first_pass_remits", "denials", "avg_days_to_pay"):
        pp[col] = pd.to_numeric(pp[col], errors="coerce")
    pp["denial_rate"] = pp["denials"] / pp["first_pass_remits"]
    pp["collection_rate"] = pp["payments"] / pp["charges"]
    marts["mart_payer_performance"] = pp

    d = r[r["denied_yn"] == "Y"]
    marts["mart_denials"] = (
        d.groupby(["remit_date", "payor_name", "carc_code", "carc_desc"], as_index=False)
        .agg(denials=("remit_id", "count"), denied_amount=("billed_amt", "sum"))
    )

    charges = t[t["tx_type"] == "Charge"]
    # Charge lag per account: days from discharge to the first charge posting.
    first_post = charges.groupby("hsp_account_id")["tx_post_date"].min().rename("first_post")
    cl = a.set_index("hsp_account_id").join(first_post, how="inner")
    charge_lag = (cl["first_post"] - pd.to_datetime(cl["disch_date_time"]).dt.normalize()).dt.days
    disch = pd.to_datetime(a["disch_date_time"])
    dnfb = a[disch.notna() & a["bill_drop_date"].isna() & (a["tot_chgs"] > 0)]
    days_to_bill = (pd.to_datetime(billed["bill_drop_date"])
                    - pd.to_datetime(billed["disch_date_time"]).dt.normalize()).dt.days
    last30 = charges[charges["tx_post_date"] > as_of - pd.Timedelta(days=30)]["tx_amount"].sum()
    total_ar = a["tot_acct_bal"].clip(lower=0).sum()
    kpi = {
        "as_of": as_of,
        "total_ar": total_ar,
        "days_in_ar": total_ar / (last30 / 30) if last30 else None,
        "dnfb_accounts": len(dnfb),
        "dnfb_amount": dnfb["tot_chgs"].sum(),
        "avg_charge_lag_days": charge_lag.mean(),
        "late_charge_rate": (charge_lag > 0).mean(),
        "avg_days_to_bill": days_to_bill.mean(),
        "first_pass_denial_rate": (first_remit["denied_yn"] == "Y").mean(),
        "clean_claim_rate": (first_remit["denied_yn"] == "N").mean(),
        "gross_charges": charges["tx_amount"].sum(),
        "payments": -t.loc[t["tx_type"] == "Payment", "tx_amount"].sum(),
    }
    marts["mart_revenue_cycle_kpis"] = pd.DataFrame([kpi])

    # ---- data & IT --------------------------------------------------------------------------
    dq = _read(engine, "dq_result")
    marts["mart_dq_daily"] = (
        dq.groupby(["extract_date", "check_name", "status"], as_index=False)
        .agg(results=("id", "count"))
    )
    comp = dq[dq["check_name"] == "completeness"]
    marts["mart_completeness_trend"] = comp[["extract_date", "table_name", "column_name",
                                             "metric", "threshold", "status"]]

    flog = _read(engine, "etl_file_log")
    loaded = flog[flog["status"] == "loaded"]
    fresh = []
    for name in LOAD_ORDER:
        core = f"clarity_{name.lower()}"
        n = int(pd.read_sql_query(f"SELECT COUNT(*) AS n FROM {core}", engine)["n"].iloc[0])
        last = loaded.loc[loaded["table_name"] == name, "extract_date"].max()
        fresh.append({"table_name": name, "warehouse_rows": n, "last_extract_date": last,
                      "files_loaded": int((loaded["table_name"] == name).sum())})
    marts["mart_table_freshness"] = pd.DataFrame(fresh)

    runs = _read(engine, "etl_run")
    runs["duration_sec"] = (pd.to_datetime(runs["finished_at"])
                            - pd.to_datetime(runs["started_at"])).dt.total_seconds()
    marts["mart_pipeline_runs"] = runs[["run_id", "trigger_type", "started_at", "finished_at",
                                        "status", "drops_loaded", "rows_loaded", "duration_sec",
                                        "message"]]

    counts = {}
    for name, df in marts.items():
        df.to_sql(name, engine, if_exists="replace", index=False, chunksize=1000)
        counts[name] = len(df)
    return counts
