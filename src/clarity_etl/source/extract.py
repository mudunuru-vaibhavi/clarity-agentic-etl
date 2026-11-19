"""Write simulated days as nightly Clarity extract drops.

Layout of one drop (what lands in the SFTP outbox):

    outbox/2026-09-01/
        PATIENT_20260901.txt        pipe-delimited, header row, UTF-8
        PAT_ENC_20260901.txt
        ...
        manifest.json               written last; its presence means the drop is complete

The manifest lists each file's table, row count, SHA-256 checksum and column list, which
the loader uses to prove nothing was lost or truncated in transit.
"""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path

from ..schema_registry import LOAD_ORDER, REGISTRY
from .simulator import DayExtract

DELIMITER = "|"
MANIFEST = "manifest.json"


def format_value(value: object, col_type: str) -> str:
    if value is None or value == "":
        return ""
    if col_type == "datetime" and isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if col_type == "date" and isinstance(value, date):
        return value.isoformat()
    if col_type == "decimal":
        return f"{float(value):.2f}"
    return str(value)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def write_drop(day: DayExtract, outbox: Path) -> Path:
    """Write one day's files and manifest. Returns the drop folder."""
    folder = outbox / day.extract_date.isoformat()
    folder.mkdir(parents=True, exist_ok=True)
    stamp = day.extract_date.strftime("%Y%m%d")
    files = []
    for table in LOAD_ORDER:
        rows = day.tables.get(table)
        if not rows:
            continue
        spec = REGISTRY[table]
        path = folder / f"{table}_{stamp}.txt"
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f, delimiter=DELIMITER, lineterminator="\n")
            writer.writerow(spec.column_names)
            for row in rows:
                writer.writerow([format_value(row.get(c), t) for c, t in spec.columns.items()])
        files.append({
            "table": table, "file": path.name, "rows": len(rows),
            "sha256": sha256_of(path), "columns": spec.column_names,
        })
    manifest = {
        "source": "Clarity (simulated)",
        "extract_date": day.extract_date.isoformat(),
        "extract_type": "full+incremental" if "CLARITY_DEP" in day.tables else "incremental",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "files": files,
    }
    (folder / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return folder
