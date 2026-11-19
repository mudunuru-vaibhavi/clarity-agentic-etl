"""The agent: each break-it scenario and the rule playbook."""

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select

from clarity_etl.agent.runner import investigate
from clarity_etl.agent.tools import Toolbox
from clarity_etl.demo import break_drop
from clarity_etl.pipeline import Settings, run_pipeline
from clarity_etl.source.extract import write_drop
from clarity_etl.source.simulator import ClaritySimulator
from clarity_etl.warehouse import etl_incident, get_engine

START, END = date(2026, 9, 1), date(2026, 9, 6)
BROKEN = "2026-09-04"


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    outbox = tmp_path / "outbox"
    for day in ClaritySimulator(START, END, seed=5, base_patients=200, weekday_visits=40).run():
        write_drop(day, outbox)
    settings = Settings(db_url=f"sqlite:///{tmp_path / 'wh.db'}", source=str(outbox),
                        landing=tmp_path / "landing", archive=tmp_path / "archive")
    engine = get_engine(settings.db_url)
    run_pipeline(settings, engine=engine, as_of=date(2026, 9, 3))
    return settings, engine, tmp_path / "incidents"


def _break_and_run(settings, engine, scenario):
    break_drop(settings, scenario, BROKEN)
    s = run_pipeline(settings, engine=engine)
    assert s.status == "failed"
    return s


def test_transfer_corruption_is_self_healed(env):
    settings, engine, reports = env
    _break_and_run(settings, engine, "transfer_corruption")
    inc = investigate(settings, engine, reports_dir=reports)
    assert inc.failed_check == "checksum"
    assert inc.status == "resolved" and not inc.needs_human_approval
    assert any("Re-pulled" in a for a in inc.actions_taken)
    assert run_pipeline(settings, engine=engine).status == "no_data", "everything is loaded"


def test_schema_drift_needs_approval(env):
    settings, engine, reports = env
    _break_and_run(settings, engine, "schema_drift")
    inc = investigate(settings, engine, reports_dir=reports)
    assert inc.failed_check == "schema_drift" and inc.status == "awaiting_approval"
    assert inc.needs_human_approval
    assert "PAT_EMAIL" in inc.root_cause and "minimum-necessary" in inc.recommended_fix
    assert "@example.org" not in inc.recommended_fix, "PHI values never go into a report"
    report = next(reports.glob("incident-*.md")).read_text()
    assert "Human approval needed | **Yes**" in report


def test_orphans_and_bad_values_are_explained(env):
    settings, engine, reports = env
    _break_and_run(settings, engine, "orphan_records")
    inc = investigate(settings, engine, reports_dir=reports)
    assert inc.failed_check == "fk_integrity" and "99900000" in inc.root_cause

    # the source re-sends a clean drop; the pipeline recovers on its own
    for day in ClaritySimulator(START, END, seed=5, base_patients=200, weekday_visits=40).run():
        if day.extract_date.isoformat() == BROKEN:
            write_drop(day, Path(settings.source))
    import shutil
    shutil.rmtree(settings.landing / BROKEN)
    assert run_pipeline(settings, engine=engine).status == "success"
    assert investigate(settings, engine, reports_dir=reports) is None


def test_bad_values(env):
    settings, engine, reports = env
    _break_and_run(settings, engine, "bad_values")
    inc = investigate(settings, engine, reports_dir=reports)
    assert inc.failed_check == "type_cast" and "TX_AMOUNT" in inc.root_cause
    with engine.connect() as conn:
        assert conn.execute(select(etl_incident.c.status)).scalar_one() == "open"


def test_query_tool_is_read_only(env):
    settings, engine, _ = env
    tb = Toolbox(settings, engine)
    assert "error" in tb.query_warehouse("DELETE FROM clarity_patient")
    assert "error" in tb.query_warehouse("SELECT 1; DROP TABLE clarity_patient")
    assert tb.query_warehouse("SELECT COUNT(*) AS n FROM clarity_patient")["rows"][0]["n"] > 0
