"""File-level checks that run before anything touches the database.

Every failure raises DropFailed with a machine-readable `check` name. The pipeline stops
there, and in M3 the agent reads these details to diagnose the problem.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .schema_registry import REGISTRY, TableSpec
from .source.extract import DELIMITER, MANIFEST, sha256_of


@dataclass
class DQResult:
    check_name: str
    table_name: str | None
    status: str  # pass | warn | fail
    metric: float | None = None
    threshold: float | None = None
    detail: str = ""
    column_name: str | None = None


class DropFailed(Exception):
    def __init__(self, check: str, message: str, table: str | None = None,
                 file: str | None = None, details: dict | None = None) -> None:
        super().__init__(message)
        self.check, self.message, self.table, self.file = check, message, table, file
        self.details = details or {}

    def __str__(self) -> str:
        where = f" [{self.table}]" if self.table else ""
        return f"{self.check}{where}: {self.message}"


@dataclass
class ParsedFile:
    table: str
    file: str
    rows: list[dict]
    sha256: str
    rows_expected: int
    checks: list[DQResult] = field(default_factory=list)


def read_manifest(drop_dir: Path) -> dict:
    path = drop_dir / MANIFEST
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise DropFailed("manifest", f"{MANIFEST} missing in {drop_dir.name}") from e
    except json.JSONDecodeError as e:
        raise DropFailed("manifest", f"{MANIFEST} is not valid JSON: {e}") from e
    if manifest.get("extract_date") != drop_dir.name:
        raise DropFailed("manifest", f"manifest extract_date {manifest.get('extract_date')!r} "
                                     f"does not match folder {drop_dir.name}")
    for entry in manifest.get("files", []):
        if entry.get("table") not in REGISTRY:
            raise DropFailed("unknown_table", f"manifest lists table {entry.get('table')!r} that is "
                             "not in the schema registry", table=entry.get("table"),
                             file=entry.get("file"))
    return manifest


def _cast(value: str, col_type: str):
    if value == "":
        return None
    if col_type == "int":
        return int(value)
    if col_type == "decimal":
        return Decimal(value)
    if col_type == "date":
        return date.fromisoformat(value)
    if col_type == "datetime":
        return datetime.fromisoformat(value)
    return value


def parse_file(drop_dir: Path, entry: dict) -> ParsedFile:
    table, name = entry["table"], entry["file"]
    spec: TableSpec = REGISTRY[table]
    path = drop_dir / name
    ctx = {"table": table, "file": name}
    if not path.exists():
        raise DropFailed("file_missing", f"{name} is listed in the manifest but was not delivered", **ctx)

    checks: list[DQResult] = []
    digest = sha256_of(path)
    if digest != entry.get("sha256"):
        raise DropFailed("checksum", f"{name} checksum does not match the manifest: the file "
                         "changed or was truncated in transit", details={
                             "expected": entry.get("sha256"), "actual": digest}, **ctx)
    checks.append(DQResult("checksum", table, "pass"))

    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.reader(f, delimiter=DELIMITER)
        header = next(reader, None)
        raw_rows = list(reader)

    expected = spec.column_names
    if header != expected:
        header = header or []
        added = [c for c in header if c not in expected]
        missing = [c for c in expected if c not in header]
        kind = "added" if added and not missing else "removed" if missing and not added else "changed"
        raise DropFailed(
            "schema_drift",
            f"{name} columns differ from the registry ({kind}): added={added} missing={missing}",
            details={"added": added, "missing": missing, "file_header": header,
                     "registry_columns": expected}, **ctx)
    checks.append(DQResult("schema_match", table, "pass"))

    if len(raw_rows) != entry.get("rows"):
        raise DropFailed("row_count", f"{name} has {len(raw_rows)} data rows but the manifest "
                         f"says {entry.get('rows')}", details={
                             "file_rows": len(raw_rows), "manifest_rows": entry.get("rows")}, **ctx)
    checks.append(DQResult("row_count", table, "pass", metric=len(raw_rows)))

    rows, errors = [], []
    cols = list(spec.columns.items())
    for line_no, raw in enumerate(raw_rows, start=2):
        if len(raw) != len(cols):
            errors.append({"line": line_no, "problem": f"{len(raw)} fields, expected {len(cols)}"})
            continue
        row = {}
        for (col, col_type), value in zip(cols, raw, strict=True):
            try:
                row[col.lower()] = _cast(value, col_type)
            except (ValueError, InvalidOperation):
                errors.append({"line": line_no, "column": col, "value": value[:50],
                               "expected_type": col_type})
        rows.append(row)
        if len(errors) >= 25:
            break
    if errors:
        raise DropFailed("type_cast", f"{name} has values that do not match the registry types "
                         f"({len(errors)}+ problems, first at line {errors[0]['line']})",
                         details={"examples": errors[:5]}, **ctx)
    checks.append(DQResult("type_cast", table, "pass"))
    return ParsedFile(table, name, rows, digest, entry["rows"], checks)
