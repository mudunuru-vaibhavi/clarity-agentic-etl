"""Investigate the latest pipeline failure and write an incident report.

The agent works through the same Toolbox a person on call would use (read the audit log, inspect
files, profile columns, query the warehouse read-only) and follows a deterministic playbook per
failure type. It may take only two actions on its own (re-pull a drop, retry the load) and only when
the evidence shows a file was damaged in transit; everything else is escalated for human approval.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import Engine, insert, update

from ..pipeline import Settings
from ..warehouse import etl_incident, get_engine
from .tools import Toolbox

PHI_HINT = re.compile(r"EMAIL|PHONE|SSN|ADDR|STREET|NAME|DOB|BIRTH", re.IGNORECASE)
SEVERITIES = ("low", "medium", "high")


@dataclass
class Incident:
    title: str
    severity: str
    root_cause: str
    impact: str
    recommended_fix: str
    needs_human_approval: bool
    status: str  # resolved | awaiting_approval | open
    actions_taken: list[str]
    run_id: int | None = None
    extract_date: str | None = None
    failed_check: str | None = None
    table_name: str | None = None
    agent_mode: str = "rules"


# ---- rule-based playbook -------------------------------------------------------------------
def diagnose_with_rules(tb: Toolbox, failure: dict) -> Incident:
    check, table, day = failure["failed_check"], failure["failed_table"], failure["extract_date"]
    base = {"run_id": failure["run_id"], "extract_date": day, "failed_check": check,
            "table_name": table, "agent_mode": "rules"}

    if check in ("checksum", "row_count", "file_missing"):
        tb.repull_drop(day)
        retry = tb.retry_load()
        first = next((d for d in retry["drops"] if d["date"] == day), None)
        healed = first is not None and first["status"] == "success"
        if healed:
            return Incident(
                title=f"{table} file for {day} was damaged in transit; re-pulled and loaded",
                severity="low",
                root_cause=(f"The landed copy of the {table} file did not match the manifest "
                            f"({check}). A fresh copy from the source matched, so the damage "
                            "happened during transfer, not in Clarity."),
                impact=(f"The {day} drop was delayed until the agent re-pulled it. "
                        f"The retry loaded {retry['rows_loaded']:,} rows across "
                        f"{len(retry['drops'])} drop(s). No data was lost."),
                recommended_fix="None needed. If this repeats, check SFTP network stability.",
                needs_human_approval=False, status="resolved", actions_taken=list(tb.actions),
                **base)
        return Incident(
            title=f"{table} file for {day} is wrong at the source",
            severity="high",
            root_cause=(f"The {table} file fails the {check} check even after a fresh pull, "
                        "so the extract itself is incomplete or was changed after the manifest "
                        "was written."),
            impact=f"The {day} drop and every later night are on hold. Reports stop at the day before.",
            recommended_fix=(f"Ask the Clarity extract team to re-run and re-send the {day} drop. "
                             "The pipeline will pick it up on the next run."),
            needs_human_approval=True, status="open", actions_taken=list(tb.actions), **base)

    if check == "schema_drift":
        sample = tb.read_file_sample(day, table)
        contract = tb.get_table_contract(table)
        expected = list(contract["columns"])
        added = [c for c in sample["header"] if c not in expected]
        missing = [c for c in expected if c not in sample["header"]]
        breaking = bool(missing)
        guesses = {}
        phi = [c for c in added if PHI_HINT.search(c)]
        for col in added:
            prof = tb.profile_column(day, table, col)
            guesses[col] = {"blank_rows": prof["blank"], "rows": prof["rows"]}
            if col not in phi:  # never copy identifying values into a report
                guesses[col]["examples"] = [v for v, _ in prof["top_values"] if v][:3]
        fix = []
        if phi:
            fix.append(f"{', '.join(phi)} looks like patient-identifying data (PHI). Under HIPAA's "
                       "minimum-necessary rule, confirm a report actually needs it before adding "
                       "it; otherwise ask the Clarity team to drop it from the extract.")
        if added:
            fix.append(f"To accept the change: add {', '.join(added)} to {table} in "
                       "schema_registry.py (as `str`) and to the warehouse table, after the "
                       f"Clarity team confirms it is intended. Column profile: {json.dumps(guesses)}")
        if missing:
            fix.append(f"Columns {missing} were removed from the extract. Ask the Clarity team "
                       "whether this is intended before changing reports that use them.")
        return Incident(
            title=f"Epic extract for {table} changed shape on {day}",
            severity="high" if breaking else "medium",
            root_cause=(f"The {table} file header no longer matches the agreed contract. "
                        f"Added: {added or 'none'}. Removed: {missing or 'none'}. This usually "
                        "follows an Epic upgrade or a change to the extract job."),
            impact=(f"The {day} drop and all later nights are on hold to protect the warehouse. "
                    "Dashboards show data up to the previous day."),
            recommended_fix=" ".join(fix),
            needs_human_approval=True, status="awaiting_approval", actions_taken=list(tb.actions),
            **base)

    if check == "fk_integrity":
        checks = [c for c in failure["non_passing_checks"] if c["check_name"] == "fk_integrity"]
        detail = checks[0]["detail"] if checks else failure["error"]
        column = checks[0]["column_name"] if checks else "?"
        return Incident(
            title=f"{table} rows on {day} point to records that do not exist",
            severity="high",
            root_cause=(f"{detail}. The parent records are in neither this drop nor any earlier "
                        "one, so the extract is missing rows or has rows from another "
                        "environment mixed in."),
            impact=(f"Loading these rows would create diagnoses or orders with no visit behind "
                    f"them and inflate counts. The {day} drop is on hold."),
            recommended_fix=(f"Ask the Clarity team to check the extract filter for {table}.{column} "
                             f"and re-send the {day} drop. Do not force-load orphan rows."),
            needs_human_approval=True, status="open", actions_taken=list(tb.actions), **base)

    if check == "type_cast":
        detail = failure["error"]
        examples = []
        sample = tb.read_file_sample(day, table, rows=0)
        if "header" in sample:
            contract = tb.get_table_contract(table)
            for col, typ in contract["columns"].items():
                if typ in ("int", "decimal", "date", "datetime"):
                    prof = tb.profile_column(day, table, col)
                    if prof.get("values_failing_type"):
                        examples.append({"column": col, "type": typ,
                                         "bad_values": prof["values_failing_type"][:3]})
        return Incident(
            title=f"Invalid values in {table} on {day}",
            severity="medium",
            root_cause=(f"{detail}. Bad values: {json.dumps(examples)}. These look like manual "
                        "entry or upstream formatting errors."),
            impact=f"The {day} drop is on hold; totals for that day are not in reports yet.",
            recommended_fix=("Ask the Clarity team to correct the source records and re-send the "
                             "drop. Do not edit extract files by hand: it breaks the audit trail."),
            needs_human_approval=True, status="open", actions_taken=list(tb.actions), **base)

    return Incident(
        title=f"{check} failed for {table or 'the drop'} on {day}",
        severity="medium",
        root_cause=failure["error"] or check,
        impact=f"The {day} drop and later nights are on hold.",
        recommended_fix="Review the failed check in the data-quality log and re-send the drop.",
        needs_human_approval=True, status="open", actions_taken=list(tb.actions), **base)


# ---- entry point ---------------------------------------------------------------------------
def investigate(settings: Settings, engine: Engine | None = None,
                reports_dir: Path = Path("data/incidents"), as_of: date | None = None,
                run_time: datetime | None = None) -> Incident | None:
    engine = engine or get_engine(settings.db_url)
    tb = Toolbox(settings, engine, as_of=as_of, clock=run_time)
    failure = tb.get_latest_failure()
    if failure.get("failure", "") is None:
        return None
    incident = diagnose_with_rules(tb, failure)
    save_incident(engine, incident, reports_dir, run_time)
    return incident


def save_incident(engine: Engine, inc: Incident, reports_dir: Path,
                  created_at: datetime | None = None) -> Path:
    with engine.begin() as conn:
        row = asdict(inc)
        row["actions_taken"] = "\n".join(inc.actions_taken)
        row["needs_human_approval"] = int(inc.needs_human_approval)
        day = inc.extract_date
        row["extract_date"] = datetime.fromisoformat(day).date() if day else None
        row["created_at"] = created_at or datetime.now().replace(microsecond=0)
        if inc.status == "resolved":
            row["resolved_at"] = row["created_at"]
        incident_id = conn.execute(insert(etl_incident).values(**row)).inserted_primary_key[0]
        reports_dir.mkdir(parents=True, exist_ok=True)
        path = reports_dir / f"incident-{incident_id:04d}-{inc.extract_date}.md"
        path.write_text(render_markdown(incident_id, inc), encoding="utf-8")
        conn.execute(update(etl_incident).where(etl_incident.c.incident_id == incident_id)
                     .values(report_path=str(path)))
    return path


def render_markdown(incident_id: int, inc: Incident) -> str:
    actions = "\n".join(f"- {a}" for a in inc.actions_taken) or "- None (investigation only)"
    approval = "**Yes**: a person must approve or act." if inc.needs_human_approval else "No"
    who = "Investigation agent (rule playbook)"
    return f"""# Incident {incident_id:04d}: {inc.title}

| | |
|---|---|
| Severity | {inc.severity} |
| Status | {inc.status} |
| Extract date | {inc.extract_date} |
| Failed check | `{inc.failed_check}` on `{inc.table_name}` |
| Pipeline run | {inc.run_id} |
| Investigated by | {who} |
| Human approval needed | {approval} |

## What happened
{inc.root_cause}

## Impact
{inc.impact}

## Actions the agent took
{actions}

## Recommended fix
{inc.recommended_fix}
"""
