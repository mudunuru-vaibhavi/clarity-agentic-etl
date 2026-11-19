"""Warehouse tables, built from the schema registry so they work on SQLite, SQL Server
and Postgres alike.

Three layers:
  stg_<table>      typed staging, emptied and reloaded for every file (no keys, so
                   duplicate rows can be detected instead of silently merged)
  clarity_<table>  current-state core tables, keyed like Clarity, upserted from staging,
                   with lineage columns etl_run_id and etl_loaded_at
  etl_* / dq_*     audit tables: every run, drop, file and data-quality result
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import (
    BigInteger,
    Column,
    Date,
    DateTime,
    Engine,
    Float,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    create_engine,
)
from sqlalchemy.types import TypeEngine

from .schema_registry import LOAD_ORDER, REGISTRY

metadata = MetaData()


def sql_type(col_type: str) -> TypeEngine:
    return {
        "int": BigInteger(),
        "str": String(200),
        "date": Date(),
        "datetime": DateTime(),
        "decimal": Numeric(14, 2),
    }[col_type]


STAGE: dict[str, Table] = {}
CORE: dict[str, Table] = {}
for _name in LOAD_ORDER:
    _spec = REGISTRY[_name]
    STAGE[_name] = Table(
        f"stg_{_name.lower()}", metadata,
        *[Column(c.lower(), sql_type(t)) for c, t in _spec.columns.items()],
    )
    CORE[_name] = Table(
        f"clarity_{_name.lower()}", metadata,
        *[
            Column(c.lower(), sql_type(t), primary_key=c in _spec.primary_key, autoincrement=False)
            for c, t in _spec.columns.items()
        ],
        Column("etl_run_id", Integer),
        Column("etl_loaded_at", DateTime),
    )

etl_run = Table(
    "etl_run", metadata,
    Column("run_id", Integer, primary_key=True, autoincrement=True),
    Column("trigger_type", String(20)),  # schedule | manual | retry | ci
    Column("started_at", DateTime),
    Column("finished_at", DateTime),
    Column("status", String(20)),  # running | success | failed | no_data
    Column("drops_pulled", Integer),
    Column("drops_loaded", Integer),
    Column("rows_loaded", Integer),
    Column("message", Text),
)

etl_drop_log = Table(
    "etl_drop_log", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", Integer, index=True),
    Column("extract_date", Date, index=True),
    Column("status", String(20)),  # success | failed
    Column("started_at", DateTime),
    Column("finished_at", DateTime),
    Column("rows_loaded", Integer),
    Column("failed_check", String(50)),
    Column("failed_table", String(50)),
    Column("message", Text),
)

etl_file_log = Table(
    "etl_file_log", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", Integer, index=True),
    Column("extract_date", Date, index=True),
    Column("table_name", String(50)),
    Column("file_name", String(200)),
    Column("rows_expected", Integer),
    Column("rows_loaded", Integer),
    Column("sha256", String(64)),
    Column("status", String(20)),  # loaded | failed | skipped
    Column("duration_ms", Integer),
    Column("message", Text),
)

dq_result = Table(
    "dq_result", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", Integer, index=True),
    Column("extract_date", Date, index=True),
    Column("check_name", String(50)),
    Column("table_name", String(50)),
    Column("column_name", String(50)),
    Column("status", String(10)),  # pass | warn | fail
    Column("metric", Float),
    Column("threshold", Float),
    Column("detail", Text),
)


etl_incident = Table(
    "etl_incident", metadata,
    Column("incident_id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", Integer, index=True),
    Column("extract_date", Date),
    Column("created_at", DateTime),
    Column("agent_mode", String(20)),  # playbook that handled it (rules)
    Column("failed_check", String(50)),
    Column("table_name", String(50)),
    Column("severity", String(10)),  # low | medium | high
    Column("title", String(200)),
    Column("root_cause", Text),
    Column("impact", Text),
    Column("actions_taken", Text),
    Column("recommended_fix", Text),
    Column("needs_human_approval", Integer),  # 1 = a person must approve the fix
    Column("status", String(20)),  # resolved | awaiting_approval | open
    Column("resolved_at", DateTime),
    Column("report_path", String(300)),
)


def get_engine(url: str) -> Engine:
    if url.startswith("sqlite:///") and url != "sqlite:///:memory:":
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url)
    metadata.create_all(engine)
    return engine
