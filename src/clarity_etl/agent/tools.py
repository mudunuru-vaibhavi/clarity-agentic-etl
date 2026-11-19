"""What the agent can see and do.

Read-only investigation tools, plus two safe actions:
  * re-pull a drop from the source (fixes files damaged in transit)
  * retry the load (which still only loads drops that pass every check)

Anything that changes the warehouse schema or the extract contract is only *proposed*;
a person approves it. Every tool returns plain JSON-able data, so the same functions serve
the agent's playbook, the CLI and the tests.
"""

from __future__ import annotations

import json
import re
import shutil
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import Engine, desc, select, text

from ..pipeline import Settings, run_pipeline
from ..schema_registry import REGISTRY
from ..source.extract import DELIMITER, MANIFEST
from ..transfer import make_transport
from ..validate import _cast
from ..warehouse import dq_result, etl_drop_log, etl_file_log

READ_ONLY_SQL = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
FORBIDDEN_SQL = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|merge|grant|exec|attach|pragma)\b",
    re.IGNORECASE)


def _rows(result, limit: int) -> list[dict]:
    out = []
    for row in result.mappings().all():
        out.append({k: (v.isoformat() if hasattr(v, "isoformat") else
                        float(v) if v.__class__.__name__ == "Decimal" else v)
                    for k, v in row.items()})
        if len(out) >= limit:
            break
    return out


@dataclass
class Toolbox:
    settings: Settings
    engine: Engine
    as_of: date | None = None  # during a backfill: the night being replayed
    clock: datetime | None = None  # during a backfill: the simulated time of the retry
    actions: list[str] = field(default_factory=list)  # audit trail of what the agent did

    # ---- investigation ------------------------------------------------------------------
    def get_latest_failure(self) -> dict:
        with self.engine.connect() as conn:
            drop = conn.execute(select(etl_drop_log).where(etl_drop_log.c.status == "failed")
                                .order_by(desc(etl_drop_log.c.id)).limit(1)).mappings().all()
            drop = drop[0] if drop else None
            if not drop:
                return {"failure": None, "message": "no failed drops"}
            fixed = conn.execute(select(etl_drop_log.c.id).where(
                etl_drop_log.c.extract_date == drop["extract_date"],
                etl_drop_log.c.status == "success", etl_drop_log.c.id > drop["id"])).first()
            if fixed:
                return {"failure": None, "message": f"{drop['extract_date']} has since loaded"}
            # Read each result fully before the next query: SQL Server allows one open result
            # per connection.
            files = _rows(conn.execute(select(etl_file_log).where(
                etl_file_log.c.run_id == drop["run_id"],
                etl_file_log.c.extract_date == drop["extract_date"])), 50)
            checks = _rows(conn.execute(select(dq_result).where(
                dq_result.c.run_id == drop["run_id"], dq_result.c.status != "pass")), 20)
            return {
                "run_id": drop["run_id"],
                "extract_date": drop["extract_date"].isoformat(),
                "failed_check": drop["failed_check"],
                "failed_table": drop["failed_table"],
                "error": drop["message"],
                "files": files,
                "non_passing_checks": checks,
                "drop_location": self._where(drop["extract_date"].isoformat()),
            }

    def _where(self, extract_date: str) -> str:
        return "landing" if (self.settings.landing / extract_date).exists() else "not in landing"

    def _drop_dir(self, extract_date: str) -> Path:
        path = self.settings.landing / extract_date
        if not path.exists():
            raise FileNotFoundError(f"drop {extract_date} is not in the landing zone")
        return path

    def read_manifest(self, extract_date: str) -> dict:
        manifest = json.loads((self._drop_dir(extract_date) / MANIFEST).read_text())
        for f in manifest.get("files", []):
            f.pop("columns", None)  # keep the answer short; headers come from read_file_sample
        return manifest

    def read_file_sample(self, extract_date: str, table: str, rows: int = 3) -> dict:
        folder = self._drop_dir(extract_date)
        matches = sorted(folder.glob(f"{table}_[0-9]*.txt"))
        if not matches:
            return {"error": f"no {table} file in drop {extract_date}",
                    "files_present": sorted(p.name for p in folder.iterdir())}
        path = matches[0]
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return {"file": path.name, "size_bytes": path.stat().st_size, "line_count": len(lines),
                "header": lines[0].split(DELIMITER) if lines else [],
                "first_rows": [ln.split(DELIMITER) for ln in lines[1:1 + rows]],
                "last_line": lines[-1] if lines else ""}

    def get_table_contract(self, table: str) -> dict:
        spec = REGISTRY.get(table)
        if not spec:
            return {"error": f"{table} is not in the schema registry", "known": list(REGISTRY)}
        return {"table": table, "kind": spec.kind, "primary_key": list(spec.primary_key),
                "columns": spec.columns, "required": list(spec.required),
                "foreign_keys": spec.foreign_keys, "description": spec.description}

    def profile_column(self, extract_date: str, table: str, column: str) -> dict:
        folder = self._drop_dir(extract_date)
        path = sorted(folder.glob(f"{table}_[0-9]*.txt"))[0]
        lines = path.read_text(encoding="utf-8").splitlines()
        header = lines[0].split(DELIMITER)
        if column not in header:
            return {"error": f"{column} not in file header", "header": header}
        i = header.index(column)
        values = [ln.split(DELIMITER)[i] if len(ln.split(DELIMITER)) > i else None
                  for ln in lines[1:]]
        col_type = REGISTRY[table].columns.get(column)
        bad = []
        if col_type:
            for n, v in enumerate(values, start=2):
                try:
                    _cast(v or "", col_type)
                except Exception:
                    bad.append({"line": n, "value": v})
        return {"file": path.name, "column": column, "registry_type": col_type,
                "rows": len(values), "blank": sum(1 for v in values if not v),
                "distinct": len(set(values)), "top_values": Counter(values).most_common(5),
                "values_failing_type": bad[:10]}

    def file_history(self, table: str, days: int = 14) -> list[dict]:
        with self.engine.connect() as conn:
            res = conn.execute(select(
                etl_file_log.c.extract_date, etl_file_log.c.rows_loaded, etl_file_log.c.status)
                .where(etl_file_log.c.table_name == table)
                .order_by(desc(etl_file_log.c.extract_date)).limit(days))
            return _rows(res, days)

    def query_warehouse(self, sql: str) -> dict:
        if not READ_ONLY_SQL.match(sql) or FORBIDDEN_SQL.search(sql) or ";" in sql.strip().rstrip(";"):
            return {"error": "only a single read-only SELECT is allowed"}
        with self.engine.connect() as conn:
            try:
                res = conn.execute(text(sql))
                rows = _rows(res, 50)
            except Exception as e:  # report SQL errors back to the agent
                return {"error": f"{type(e).__name__}: {str(e).splitlines()[0]}"}
            finally:
                conn.rollback()
        return {"row_count": len(rows), "rows": rows}

    # ---- actions --------------------------------------------------------------------------
    def repull_drop(self, extract_date: str) -> dict:
        """Delete the landed copy and download the drop again from the source."""
        dest = self.settings.landing / extract_date
        transport = make_transport(self.settings.source)
        try:
            if extract_date not in transport.list_drops():
                return {"error": f"source no longer has drop {extract_date}"}
            shutil.rmtree(dest, ignore_errors=True)
            partial = self.settings.landing / f".partial-{extract_date}"
            shutil.rmtree(partial, ignore_errors=True)
            transport.fetch(extract_date, partial)
            partial.rename(dest)
        finally:
            transport.close()
        self.actions.append(f"Re-pulled drop {extract_date} from the source")
        return {"repulled": extract_date}

    def retry_load(self) -> dict:
        """Run the pipeline again (loads only drops that pass every check)."""
        s = run_pipeline(self.settings, as_of=self.as_of, run_time=self.clock,
                         trigger="agent_retry", engine=self.engine)
        self.actions.append(f"Retried the pipeline (run {s.run_id}): {s.status}")
        return {"run_id": s.run_id, "status": s.status, "rows_loaded": s.rows_loaded,
                "drops": [{"date": o.extract_date.isoformat(), "status": o.status,
                           "error": str(o.error) if o.error else None} for o in s.outcomes]}
