"""One pipeline run: pull new drops, load them in date order, record everything.

Drops load oldest first and the run stops at the first failed drop, because an
incremental extract can't be applied before the one it builds on. A failed drop stays in
the landing zone so it can be fixed and retried; loaded drops move to the archive.
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import Engine, insert, select, update

from .loader import DropOutcome, load_drop
from .transfer import landed_drops, make_transport, pull_new_drops
from .warehouse import dq_result, etl_drop_log, etl_file_log, etl_incident, etl_run, get_engine


@dataclass
class Settings:
    db_url: str = "sqlite:///data/warehouse.db"
    source: str = "data/sftp/outbox"
    landing: Path = Path("data/landing")
    archive: Path = Path("data/archive")

    @classmethod
    def from_env(cls, **overrides) -> Settings:
        s = cls(
            db_url=os.environ.get("CLARITY_ETL_DB_URL", cls.db_url),
            source=os.environ.get("CLARITY_ETL_SOURCE", cls.source),
            landing=Path(os.environ.get("CLARITY_ETL_LANDING", cls.landing)),
            archive=Path(os.environ.get("CLARITY_ETL_ARCHIVE", cls.archive)),
        )
        for k, v in overrides.items():
            if v is not None:
                setattr(s, k, Path(v) if k in ("landing", "archive") else v)
        return s


@dataclass
class RunSummary:
    run_id: int
    status: str
    drops_pulled: list[str] = field(default_factory=list)
    outcomes: list[DropOutcome] = field(default_factory=list)
    message: str = ""

    @property
    def rows_loaded(self) -> int:
        return sum(o.rows_loaded for o in self.outcomes)


class _Clock:
    """Real elapsed time, optionally anchored to a simulated start (for backfills)."""

    def __init__(self, start: datetime | None) -> None:
        self.start = start or datetime.now().replace(microsecond=0)
        self._t0 = time.perf_counter()

    def now(self) -> datetime:
        return self.start + timedelta(seconds=time.perf_counter() - self._t0)


def loaded_drops(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(select(etl_drop_log.c.extract_date)
                            .where(etl_drop_log.c.status == "success")).scalars()
        return {d.isoformat() for d in rows}


def _record_drop(engine: Engine, run_id: int, o: DropOutcome, started: datetime,
                 finished: datetime) -> None:
    err = o.error
    with engine.begin() as conn:
        conn.execute(insert(etl_drop_log).values(
            run_id=run_id, extract_date=o.extract_date, status=o.status, started_at=started,
            finished_at=finished, rows_loaded=o.rows_loaded,
            failed_check=err.check if err else None, failed_table=err.table if err else None,
            message=str(err) if err else None))
        if o.files:
            conn.execute(insert(etl_file_log), [
                {"run_id": run_id, "extract_date": o.extract_date, "table_name": f.table,
                 "file_name": f.file, "rows_expected": f.rows_expected,
                 "rows_loaded": f.rows_loaded, "sha256": f.sha256, "status": f.status,
                 "duration_ms": f.duration_ms, "message": f.message or None}
                for f in o.files])
        if o.checks:
            conn.execute(insert(dq_result), [
                {"run_id": run_id, "extract_date": o.extract_date, "check_name": c.check_name,
                 "table_name": c.table_name, "column_name": c.column_name, "status": c.status,
                 "metric": c.metric, "threshold": c.threshold, "detail": c.detail or None}
                for c in o.checks])


def run_pipeline(settings: Settings, as_of: date | None = None, run_time: datetime | None = None,
                 trigger: str = "manual", engine: Engine | None = None) -> RunSummary:
    engine = engine or get_engine(settings.db_url)
    clock = _Clock(run_time)
    with engine.begin() as conn:
        run_id = conn.execute(insert(etl_run).values(
            trigger_type=trigger, started_at=clock.now(), status="running",
            drops_pulled=0, drops_loaded=0, rows_loaded=0)).inserted_primary_key[0]
    summary = RunSummary(run_id, "running")

    try:
        done = loaded_drops(engine)
        transport = make_transport(settings.source)
        try:
            summary.drops_pulled = pull_new_drops(transport, settings.landing, done, as_of)
        finally:
            transport.close()

        pending = [d for d in landed_drops(settings.landing)
                   if d not in done and (as_of is None or date.fromisoformat(d) <= as_of)]
        for drop in pending:
            started = clock.now()
            outcome = load_drop(engine, settings.landing / drop, run_id, started)
            _record_drop(engine, run_id, outcome, started, clock.now())
            summary.outcomes.append(outcome)
            if outcome.status != "success":
                summary.status = "failed"
                summary.message = f"{drop}: {outcome.error}"
                break
            with engine.begin() as conn:  # a drop that now loads closes its open incidents
                conn.execute(update(etl_incident).where(
                    etl_incident.c.extract_date == outcome.extract_date,
                    etl_incident.c.status != "resolved").values(
                        status="resolved", resolved_at=clock.now()))
            settings.archive.mkdir(parents=True, exist_ok=True)
            shutil.rmtree(settings.archive / drop, ignore_errors=True)
            shutil.move(str(settings.landing / drop), str(settings.archive / drop))
        else:
            summary.status = "success" if pending else "no_data"
            if not pending:
                summary.message = "no new drops"
    except Exception as e:  # transport or database errors: record, then re-raise
        summary.status, summary.message = "failed", f"{type(e).__name__}: {e}"
        _finish(engine, summary, clock)
        raise
    _finish(engine, summary, clock)
    return summary


def _finish(engine: Engine, s: RunSummary, clock: _Clock) -> None:
    with engine.begin() as conn:
        conn.execute(update(etl_run).where(etl_run.c.run_id == s.run_id).values(
            finished_at=clock.now(), status=s.status, drops_pulled=len(s.drops_pulled),
            drops_loaded=sum(o.status == "success" for o in s.outcomes),
            rows_loaded=s.rows_loaded, message=s.message or None))
