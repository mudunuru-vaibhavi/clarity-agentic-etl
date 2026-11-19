import csv
import json
from datetime import date

import pytest

from clarity_etl.schema_registry import REGISTRY
from clarity_etl.source.extract import MANIFEST, sha256_of, write_drop
from clarity_etl.source.simulator import ClaritySimulator

START, END = date(2026, 9, 1), date(2026, 10, 15)


@pytest.fixture(scope="module")
def days():
    return list(ClaritySimulator(START, END, seed=7, base_patients=500, weekday_visits=60).run())


def _all(days, table):
    return [row for d in days for row in d.tables.get(table, [])]


def test_one_drop_per_day_and_full_load_first(days):
    assert len(days) == (END - START).days + 1
    assert "CLARITY_DEP" in days[0].tables
    assert all("CLARITY_DEP" not in d.tables for d in days[1:])


def test_deterministic_for_same_seed():
    a = ClaritySimulator(START, START, seed=3, base_patients=50).run()
    b = ClaritySimulator(START, START, seed=3, base_patients=50).run()
    ta, tb = next(a).tables, next(b).tables
    for t in ("PATIENT", "PAT_ENC", "HSP_TRANSACTIONS"):
        strip = lambda rows: [{k: v for k, v in r.items() if k != "UPDATE_DATE"} for r in rows]  # noqa: E731
        assert strip(ta[t]) == strip(tb[t])


def test_rows_match_registry_columns(days):
    for d in days:
        for table, rows in d.tables.items():
            cols = set(REGISTRY[table].column_names)
            for row in rows:
                assert set(row) == cols, table


def test_referential_integrity(days):
    patients = {r["PAT_ID"] for r in _all(days, "PATIENT")}
    encounters = {r["PAT_ENC_CSN_ID"] for r in _all(days, "PAT_ENC")}
    accounts = {r["HSP_ACCOUNT_ID"] for r in _all(days, "HSP_ACCOUNT")}
    assert {r["PAT_ID"] for r in _all(days, "PAT_ENC")} <= patients
    assert {r["PAT_ENC_CSN_ID"] for r in _all(days, "PAT_ENC_DX")} <= encounters
    assert {r["PAT_ENC_CSN_ID"] for r in _all(days, "ORDER_PROC")} <= encounters
    assert {r["HSP_ACCOUNT_ID"] for r in _all(days, "HSP_TRANSACTIONS")} <= accounts
    assert {r["HSP_ACCOUNT_ID"] for r in _all(days, "CL_REMIT")} <= accounts


def test_account_balances_tie_to_transactions(days):
    latest = {}
    for row in _all(days, "HSP_ACCOUNT"):
        latest[row["HSP_ACCOUNT_ID"]] = row
    tx_sum = {}
    for tx in _all(days, "HSP_TRANSACTIONS"):
        tx_sum[tx["HSP_ACCOUNT_ID"]] = tx_sum.get(tx["HSP_ACCOUNT_ID"], 0) + tx["TX_AMOUNT"]
    for har, acct in latest.items():
        assert acct["TOT_ACCT_BAL"] == pytest.approx(tx_sum.get(har, 0), abs=0.05)


def test_revenue_cycle_produces_payments_and_denials(days):
    remits = _all(days, "CL_REMIT")
    assert any(r["DENIED_YN"] == "Y" for r in remits)
    assert any(r["DENIED_YN"] == "N" and r["PAID_AMT"] > 0 for r in remits)


def test_drop_files_match_manifest(days, tmp_path):
    folder = write_drop(days[0], tmp_path)
    manifest = json.loads((folder / MANIFEST).read_text())
    assert manifest["extract_date"] == "2026-09-01"
    for entry in manifest["files"]:
        path = folder / entry["file"]
        with path.open(newline="") as f:
            rows = list(csv.reader(f, delimiter="|"))
        assert rows[0] == entry["columns"]
        assert len(rows) - 1 == entry["rows"]
        assert sha256_of(path) == entry["sha256"]


def test_no_future_discharges_and_discharges_are_resent(days):
    for d in days:
        for row in d.tables.get("PAT_ENC", []):
            if row["CHECKOUT_TIME"] is not None:
                assert row["CHECKOUT_TIME"].date() <= d.extract_date
    csns = [r["PAT_ENC_CSN_ID"] for r in _all(days, "PAT_ENC")]
    assert len(csns) > len(set(csns)), "inpatient stays should be re-sent at discharge"


def test_primary_keys_unique_within_each_file(days):
    for d in days:
        for table, rows in d.tables.items():
            pk = REGISTRY[table].primary_key
            keys = [tuple(r[c] for c in pk) for r in rows]
            assert len(keys) == len(set(keys)), (table, d.extract_date)
