"""End-to-end pipeline tests on SQLite: generate drops, load them, then break things."""

import json
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import func, select

from clarity_etl.pipeline import Settings, run_pipeline
from clarity_etl.source.extract import MANIFEST, sha256_of, write_drop
from clarity_etl.source.simulator import ClaritySimulator
from clarity_etl.warehouse import CORE, dq_result, etl_drop_log, etl_run, get_engine

START, END = date(2026, 9, 1), date(2026, 9, 10)


@pytest.fixture
def env(tmp_path: Path):
    outbox = tmp_path / "outbox"
    for day in ClaritySimulator(START, END, seed=11, base_patients=300, weekday_visits=50).run():
        write_drop(day, outbox)
    settings = Settings(db_url=f"sqlite:///{tmp_path / 'wh.db'}", source=str(outbox),
                        landing=tmp_path / "landing", archive=tmp_path / "archive")
    return settings, get_engine(settings.db_url), outbox


def _count(engine, table, *where):
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table).where(*where)).scalar_one()


def _rewrite(outbox: Path, drop: str, table: str, edit) -> None:
    """Edit one extract file with `edit(lines) -> lines` and re-sign the manifest."""
    folder = outbox / drop
    manifest = json.loads((folder / MANIFEST).read_text())
    entry = next(e for e in manifest["files"] if e["table"] == table)
    path = folder / entry["file"]
    lines = edit(path.read_text().splitlines())
    path.write_text("\n".join(lines) + "\n")
    entry["rows"], entry["sha256"] = len(lines) - 1, sha256_of(path)
    (folder / MANIFEST).write_text(json.dumps(manifest))


def test_full_load_then_idempotent(env):
    settings, engine, _ = env
    first = run_pipeline(settings, engine=engine)
    assert first.status == "success"
    assert len(first.outcomes) == (END - START).days + 1
    assert _count(engine, CORE["CLARITY_DEP"]) == 12
    assert _count(engine, CORE["PAT_ENC"]) > 300
    assert not list(settings.landing.glob("20*")), "loaded drops move to the archive"

    again = run_pipeline(settings, engine=engine)
    assert again.status == "no_data" and again.rows_loaded == 0


def test_upserts_keep_latest_account_state(env):
    settings, engine, _ = env
    run_pipeline(settings, engine=engine)
    acct, tx = CORE["HSP_ACCOUNT"], CORE["HSP_TRANSACTIONS"]
    with engine.connect() as conn:
        balances = dict(conn.execute(select(acct.c.hsp_account_id, acct.c.tot_acct_bal)).all())
        sums = dict(conn.execute(select(tx.c.hsp_account_id, func.sum(tx.c.tx_amount))
                                 .group_by(tx.c.hsp_account_id)).all())
    assert balances
    for har, bal in balances.items():
        assert float(bal) == pytest.approx(float(sums.get(har, 0)), abs=0.05)


def test_one_drop_per_nightly_run(env):
    settings, engine, _ = env
    for day in range(1, 4):
        s = run_pipeline(settings, engine=engine, as_of=date(2026, 9, day), trigger="schedule")
        assert [o.extract_date.day for o in s.outcomes] == [day]


def test_checksum_mismatch_fails_and_rolls_back(env):
    settings, engine, outbox = env
    path = next((outbox / "2026-09-04").glob("PAT_ENC_2*.txt"))
    path.write_text(path.read_text()[:-200])  # truncated in transit
    s = run_pipeline(settings, engine=engine)
    assert s.status == "failed"
    assert [o.status for o in s.outcomes] == ["success"] * 3 + ["failed"]
    assert s.outcomes[-1].error.check == "checksum"
    # nothing from the failed drop or later drops reached the warehouse
    pe = CORE["PAT_ENC"]
    assert _count(engine, pe, pe.c.contact_date >= date(2026, 9, 4)) == 0
    assert (settings.landing / "2026-09-04").exists(), "failed drop stays in landing for retry"
    assert _count(engine, etl_run, etl_run.c.status == "failed") == 1


def test_schema_drift_is_reported(env):
    settings, engine, outbox = env

    def add_column(lines):
        return [lines[0] + "|PAT_EMAIL"] + [ln + "|" for ln in lines[1:]]

    _rewrite(outbox, "2026-09-02", "PATIENT", add_column)
    s = run_pipeline(settings, engine=engine)
    err = s.outcomes[-1].error
    assert err.check == "schema_drift" and err.table == "PATIENT"
    assert err.details["added"] == ["PAT_EMAIL"] and err.details["missing"] == []
    with engine.connect() as conn:
        failed = conn.execute(select(etl_drop_log).where(etl_drop_log.c.status == "failed")).one()
    assert failed.failed_check == "schema_drift"


def test_orphan_foreign_key_fails(env):
    settings, engine, outbox = env

    def orphan_dx(lines):
        return lines + ["999999999|1|2001|Y|2026-09-03"]

    _rewrite(outbox, "2026-09-03", "PAT_ENC_DX", orphan_dx)
    s = run_pipeline(settings, engine=engine)
    err = s.outcomes[-1].error
    assert err.check == "fk_integrity" and err.table == "PAT_ENC_DX"
    assert "999999999" in err.message


def test_bad_value_type_fails(env):
    settings, engine, outbox = env

    def bad_amount(lines):
        parts = lines[1].split("|")
        parts[5] = "12O.50"  # letter O instead of zero
        return [lines[0], "|".join(parts), *lines[2:]]

    _rewrite(outbox, "2026-09-05", "HSP_TRANSACTIONS", bad_amount)
    s = run_pipeline(settings, engine=engine)
    err = s.outcomes[-1].error
    assert err.check == "type_cast"
    assert err.details["examples"][0]["column"] == "TX_AMOUNT"


def test_dq_results_are_recorded(env):
    settings, engine, _ = env
    run_pipeline(settings, engine=engine)
    assert _count(engine, dq_result, dq_result.c.status == "fail") == 0
    assert _count(engine, dq_result, dq_result.c.check_name == "fk_integrity") > 50


def test_static_dashboard_builds(env, tmp_path):
    from clarity_etl.marts import build_marts
    from clarity_etl.site import build_site

    settings, engine, _ = env
    run_pipeline(settings, engine=engine)
    build_marts(engine)
    page = build_site(engine, tmp_path / "site")
    html = page.read_text()
    assert "__DATA__" not in html
    data = json.loads(html.split('<script id="data" type="application/json">')[1].split("</script>")[0])
    assert data["meta"]["as_of"] == "2026-09-10"
    assert data["activity"] and data["runs"] and "total_ar" in data["kpis"]
    assert (tmp_path / "site" / ".nojekyll").exists()
