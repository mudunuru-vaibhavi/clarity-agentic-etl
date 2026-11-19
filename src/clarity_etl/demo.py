"""Break-it scenarios: realistic failures to show what the pipeline and the agent do.

Each scenario damages one night's drop the way it happens in real life:
  transfer_corruption  a file is cut short on the way over (landing copy only)
  schema_drift         Epic adds a column to the PATIENT extract (source changed)
  orphan_records       diagnosis rows point at visits that were never sent (source)
  bad_values           a dollar amount typed as "12O.50" with a letter O (source)
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .pipeline import Settings
from .source.extract import MANIFEST, sha256_of
from .transfer import make_transport, pull_new_drops

SCENARIOS = ("transfer_corruption", "schema_drift", "orphan_records", "bad_values")


def _resign(folder: Path, table: str, edit) -> None:
    """Edit one file and rewrite the manifest to match, as the source system would."""
    manifest = json.loads((folder / MANIFEST).read_text())
    entry = next(e for e in manifest["files"] if e["table"] == table)
    path = folder / entry["file"]
    lines = edit(path.read_text().splitlines())
    path.write_text("\n".join(lines) + "\n")
    entry["rows"], entry["sha256"] = len(lines) - 1, sha256_of(path)
    entry["columns"] = lines[0].split("|")
    (folder / MANIFEST).write_text(json.dumps(manifest, indent=2))


def break_drop(settings: Settings, scenario: str, extract_date: str) -> str:
    if scenario == "transfer_corruption":  # works with any source, SFTP included
        transport = make_transport(settings.source)
        try:
            pull_new_drops(transport, settings.landing, set(), date.fromisoformat(extract_date))
        finally:
            transport.close()
        landed = next((settings.landing / extract_date).glob("HSP_TRANSACTIONS_[0-9]*.txt"))
        data = landed.read_bytes()
        landed.write_bytes(data[: len(data) * 2 // 3])
        return f"truncated landed {landed.name} to 2/3 of its size (source copy is fine)"

    # The other scenarios change the source itself, so they need a local outbox folder.
    folder = Path(settings.source) / extract_date
    if settings.source.startswith("sftp://") or not folder.exists():
        raise SystemExit(f"{scenario} edits the source files; it needs a local outbox with drop "
                         f"{extract_date} (source is {settings.source})")

    if scenario == "schema_drift":
        _resign(folder, "PATIENT", lambda ls: [ls[0] + "|PAT_EMAIL"] + [
            ln + f"|patient{i}@example.org" for i, ln in enumerate(ls[1:])])
        return "added PAT_EMAIL column to the PATIENT extract at the source"

    if scenario == "orphan_records":
        _resign(folder, "PAT_ENC_DX", lambda ls: ls + [
            f"{999000000 + i}|1|2002|Y|{extract_date}" for i in range(4)])
        return "added 4 PAT_ENC_DX rows for encounters that do not exist"

    if scenario == "bad_values":
        def typo(ls):
            parts = ls[1].split("|")
            parts[5] = parts[5].replace("0", "O", 1) if "0" in parts[5] else "12O.50"
            return [ls[0], "|".join(parts), *ls[2:]]
        _resign(folder, "HSP_TRANSACTIONS", typo)
        return "typed a letter O into a TX_AMOUNT at the source"

    raise SystemExit(f"unknown scenario {scenario}; choose from {SCENARIOS}")
