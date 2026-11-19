"""Load one nightly drop into the warehouse, all or nothing.

For each file, in foreign-key order: validate the file, load it into typed staging, run
SQL data-quality checks on staging, then upsert into the core table (delete matching
keys, insert from staging). The whole drop runs in a single transaction, so a failure
leaves the warehouse exactly as it was.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import (
    Connection,
    DateTime,
    Engine,
    Integer,
    and_,
    delete,
    exists,
    func,
    insert,
    literal,
    or_,
    select,
)

from .schema_registry import REGISTRY
from .validate import DQResult, DropFailed, parse_file, read_manifest
from .warehouse import CORE, STAGE, etl_drop_log, etl_file_log

# Columns that are allowed to be blank sometimes. A warning needs both a blank rate above
# the limit and at least MIN_BLANKS rows, so a small day (1 blank of 16) doesn't cry wolf.
# The measured rate is recorded every day either way, for trending.
COMPLETENESS_WARN = {
    ("PATIENT", "ZIP"): 0.05,
    ("PAT_ENC", "VISIT_PROV_ID"): 0.03,
}
MIN_BLANKS = 5
VOLUME_TABLE = "PAT_ENC"
VOLUME_BAND = (0.5, 1.8)  # warn when encounter volume is outside 50%-180% of normal
BATCH = 1000


@dataclass
class FileResult:
    table: str
    file: str
    rows_expected: int
    rows_loaded: int
    sha256: str | None
    status: str
    duration_ms: int
    message: str = ""


@dataclass
class DropOutcome:
    extract_date: date
    status: str = "success"
    rows_loaded: int = 0
    files: list[FileResult] = field(default_factory=list)
    checks: list[DQResult] = field(default_factory=list)
    error: DropFailed | None = None


# ---- SQL data-quality checks on staging --------------------------------------------------
def _count(conn: Connection, stmt) -> int:
    return int(conn.execute(stmt).scalar_one())


def sql_checks(conn: Connection, table: str, row_count: int) -> list[DQResult]:
    spec = REGISTRY[table]
    stg = STAGE[table]
    results: list[DQResult] = []

    pk_cols = [stg.c[c.lower()] for c in spec.primary_key]
    dupes = select(*pk_cols).group_by(*pk_cols).having(func.count() > 1).subquery()
    n = _count(conn, select(func.count()).select_from(dupes))
    results.append(DQResult("pk_unique", table, "fail" if n else "pass", n, 0,
                            f"{n} duplicate primary keys ({', '.join(spec.primary_key)})" if n else ""))

    for col in spec.required:
        n = _count(conn, select(func.count()).select_from(stg).where(stg.c[col.lower()].is_(None)))
        results.append(DQResult("required_not_null", table, "fail" if n else "pass", n, 0,
                                f"{n} rows with blank {col}" if n else "", column_name=col))

    for col, target in spec.foreign_keys.items():
        parent_name, parent_col = target.split(".")
        parent = CORE[parent_name]
        child = stg.c[col.lower()]
        orphan = and_(child.is_not(None),
                      ~exists().where(parent.c[parent_col.lower()] == child))
        n = _count(conn, select(func.count()).select_from(stg).where(orphan))
        detail = ""
        if n:
            sample = conn.execute(select(child).where(orphan).distinct().limit(5)).scalars().all()
            detail = f"{n} rows reference a missing {target}; e.g. {sample}"
        results.append(DQResult("fk_integrity", table, "fail" if n else "pass", n, 0, detail,
                                column_name=col))

    for (t, col), limit in COMPLETENESS_WARN.items():
        if t == table and row_count:
            n = _count(conn, select(func.count()).select_from(stg).where(stg.c[col.lower()].is_(None)))
            rate = n / row_count
            bad = rate > limit and n >= MIN_BLANKS
            results.append(DQResult("completeness", table, "warn" if bad else "pass",
                                    round(rate, 4), limit, f"{n} of {row_count} blank", column_name=col))

    if table == "HSP_ACCOUNT":
        off = func.abs(stg.c.tot_chgs - stg.c.tot_pmts - stg.c.tot_adj - stg.c.tot_acct_bal) > 0.01
        n = _count(conn, select(func.count()).select_from(stg).where(off))
        results.append(DQResult("balance_reconciliation", table, "fail" if n else "pass", n, 0,
                                f"{n} accounts where charges - payments - adjustments != balance"
                                if n else ""))
    return results


def drop_checks(conn: Connection, extract_date: date, files: list[FileResult]) -> list[DQResult]:
    results = []
    last = conn.execute(
        select(func.max(etl_drop_log.c.extract_date))
        .where(etl_drop_log.c.status == "success", etl_drop_log.c.extract_date < extract_date)
    ).scalar_one()
    if last is not None:
        gap = (extract_date - last).days
        results.append(DQResult("freshness_gap", None, "warn" if gap > 1 else "pass", gap, 1,
                                f"{gap - 1} missing day(s) before this drop" if gap > 1 else ""))

    todays = next((f.rows_loaded for f in files if f.table == VOLUME_TABLE), 0)
    same_weekday = [extract_date - timedelta(weeks=w) for w in range(1, 5)]
    history = conn.execute(
        select(func.avg(etl_file_log.c.rows_loaded)).where(
            etl_file_log.c.table_name == VOLUME_TABLE, etl_file_log.c.status == "loaded",
            or_(*[etl_file_log.c.extract_date == d for d in same_weekday]))
    ).scalar_one()
    if history:
        ratio = todays / float(history)
        lo, hi = VOLUME_BAND
        bad = not lo <= ratio <= hi
        results.append(DQResult("volume_anomaly", VOLUME_TABLE, "warn" if bad else "pass",
                                round(ratio, 3), lo if ratio < lo else hi,
                                f"{todays} encounters vs {float(history):.0f} on the same weekday "
                                "over the last 4 weeks"))
    return results


# ---- load ---------------------------------------------------------------------------------
def _merge(conn: Connection, table: str, run_id: int, loaded_at: datetime) -> None:
    spec = REGISTRY[table]
    stg, core = STAGE[table], CORE[table]
    keys = [c.lower() for c in spec.primary_key]
    conn.execute(delete(core).where(exists().where(and_(*[stg.c[k] == core.c[k] for k in keys]))))
    cols = [c.lower() for c in spec.column_names]
    conn.execute(insert(core).from_select(
        [*cols, "etl_run_id", "etl_loaded_at"],
        select(*[stg.c[c] for c in cols],
               literal(run_id, Integer).label("etl_run_id"),
               literal(loaded_at, DateTime).label("etl_loaded_at")),
    ))


def load_drop(engine: Engine, drop_dir: Path, run_id: int, loaded_at: datetime) -> DropOutcome:
    outcome = DropOutcome(date.fromisoformat(drop_dir.name))
    current: tuple[str, str, int] | None = None
    try:
        manifest = read_manifest(drop_dir)
        listed = {e["file"] for e in manifest["files"]}
        extra = sorted(p.name for p in drop_dir.iterdir() if p.is_file()
                       and p.name != "manifest.json" and p.name not in listed)
        if extra:
            outcome.checks.append(DQResult("unexpected_file", None, "warn", len(extra), 0,
                                           f"files not in manifest were ignored: {extra}"))
        entries = sorted(manifest["files"], key=lambda e: list(REGISTRY).index(e["table"]))
        with engine.begin() as conn:
            for entry in entries:
                t0 = time.perf_counter()
                current = (entry["table"], entry["file"], entry.get("rows") or 0)
                parsed = parse_file(drop_dir, entry)
                outcome.checks.extend(parsed.checks)
                stg = STAGE[parsed.table]
                conn.execute(delete(stg))
                for i in range(0, len(parsed.rows), BATCH):
                    conn.execute(insert(stg), parsed.rows[i:i + BATCH])
                checks = sql_checks(conn, parsed.table, len(parsed.rows))
                outcome.checks.extend(checks)
                failed = next((c for c in checks if c.status == "fail"), None)
                if failed:
                    raise DropFailed(failed.check_name, failed.detail, table=parsed.table,
                                     file=parsed.file, details={"column": failed.column_name,
                                                                "count": failed.metric})
                _merge(conn, parsed.table, run_id, loaded_at)
                conn.execute(delete(stg))
                outcome.files.append(FileResult(
                    parsed.table, parsed.file, parsed.rows_expected, len(parsed.rows),
                    parsed.sha256, "loaded", int((time.perf_counter() - t0) * 1000)))
                outcome.rows_loaded += len(parsed.rows)
                current = None
            outcome.checks.extend(drop_checks(conn, outcome.extract_date, outcome.files))
    except DropFailed as e:
        outcome.status, outcome.error, outcome.rows_loaded = "failed", e, 0
        if not any(c.status == "fail" and c.check_name == e.check for c in outcome.checks):
            outcome.checks.append(DQResult(e.check, e.table, "fail", detail=str(e)))
        # The transaction rolled back, so nothing from this drop is in the warehouse.
        for f in outcome.files:
            f.status, f.rows_loaded, f.message = "rolled_back", 0, "drop failed later"
        if current:
            outcome.files.append(FileResult(current[0], current[1], current[2], 0, None,
                                            "failed", 0, e.message))
    return outcome
