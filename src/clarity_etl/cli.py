"""Command line entry point: `clarity-etl <command>`."""

from __future__ import annotations

import argparse
import random
import shutil
import sys
from collections import Counter
from datetime import date, datetime, time, timedelta
from pathlib import Path

from sqlalchemy import desc, func, select

from .pipeline import RunSummary, Settings, loaded_drops, run_pipeline
from .source.extract import write_drop
from .source.simulator import ClaritySimulator
from .transfer import make_transport
from .warehouse import etl_run, get_engine


def cmd_generate(args: argparse.Namespace) -> None:
    sim = ClaritySimulator(
        date.fromisoformat(args.start), date.fromisoformat(args.end), seed=args.seed,
        base_patients=args.patients,
    )
    totals: Counter[str] = Counter()
    days = 0
    for day in sim.run():
        if args.only_date and day.extract_date.isoformat() != args.only_date:
            continue
        if args.only_date:  # re-send one night's drop, e.g. after the source fixed it
            shutil.rmtree(Path(args.outbox) / args.only_date, ignore_errors=True)
        write_drop(day, Path(args.outbox))
        for table, rows in day.tables.items():
            totals[table] += len(rows)
        days += 1
    print(f"Wrote {days} nightly drops to {args.outbox}")
    for table, n in sorted(totals.items(), key=lambda kv: -kv[1]):
        print(f"  {table:<20} {n:>9,} rows")


def _settings(args: argparse.Namespace) -> Settings:
    return Settings.from_env(db_url=args.db, source=args.source)


def _print_run(s: RunSummary) -> None:
    print(f"run {s.run_id}: {s.status.upper()}  pulled={len(s.drops_pulled)} "
          f"loaded={sum(o.status == 'success' for o in s.outcomes)} rows={s.rows_loaded:,}"
          + (f"  ({s.message})" if s.message else ""))
    for o in s.outcomes:
        warns = [c for c in o.checks if c.status == "warn"]
        line = f"  {o.extract_date}  {o.status:<8} {o.rows_loaded:>6,} rows"
        if warns:
            line += "  warnings: " + "; ".join(f"{c.check_name}({c.table_name or '-'})" for c in warns)
        if o.error:
            line += f"  ERROR {o.error}"
        print(line)


def cmd_run(args: argparse.Namespace) -> None:
    settings = _settings(args)
    s = run_pipeline(settings, as_of=date.fromisoformat(args.as_of) if args.as_of else None,
                     trigger=args.trigger)
    _print_run(s)
    status = s.status
    if status == "failed" and args.with_agent:
        _agent(settings)  # may re-pull and retry; check where that left things
        status = _latest_status(settings)
    elif status != "failed":
        _marts(settings)
    sys.exit(1 if status == "failed" else 0)


def _latest_status(settings: Settings) -> str:
    engine = get_engine(settings.db_url)
    with engine.connect() as conn:
        return conn.execute(select(etl_run.c.status).order_by(desc(etl_run.c.run_id)).limit(1)).scalar_one()


def _agent(settings: Settings, as_of: date | None = None,
           run_time: datetime | None = None) -> None:
    from .agent.runner import investigate

    inc = investigate(settings, as_of=as_of, run_time=run_time)
    if inc is None:
        print("agent: no open failure to investigate")
        return
    print(f"\nagent: [{inc.severity.upper()}] {inc.title}")
    print(f"  status: {inc.status}  human approval needed: {inc.needs_human_approval}")
    for a in inc.actions_taken:
        print(f"  action: {a}")
    print(f"  cause:  {inc.root_cause}")
    print(f"  fix:    {inc.recommended_fix}")
    print("  report: data/incidents/ (incident table: etl_incident)")


def cmd_agent(args: argparse.Namespace) -> None:
    _agent(_settings(args))


def cmd_break(args: argparse.Namespace) -> None:
    from .demo import break_drop

    print(break_drop(_settings(args), args.scenario, args.date))


def _marts(settings: Settings) -> None:
    from .marts import build_marts

    counts = build_marts(get_engine(settings.db_url))
    print(f"marts refreshed: {', '.join(f'{k}={v:,}' for k, v in counts.items())}")


def cmd_marts(args: argparse.Namespace) -> None:
    _marts(_settings(args))


def cmd_site(args: argparse.Namespace) -> None:
    from .site import build_site

    page = build_site(get_engine(_settings(args).db_url), Path(args.out))
    print(f"dashboard written to {page} ({page.stat().st_size / 1024:,.0f} KB); open it in a browser")


def cmd_backfill(args: argparse.Namespace) -> None:
    """Replay history as if the pipeline had run every night at ~2 AM."""
    settings = _settings(args)
    engine = get_engine(settings.db_url)
    transport = make_transport(settings.source)
    try:
        drops = transport.list_drops()
    finally:
        transport.close()
    rng = random.Random(1)
    totals = Counter()
    for drop in drops:
        if drop in loaded_drops(engine):  # re-read: an agent retry may have loaded it
            continue
        if args.until and drop > args.until:
            break
        d = date.fromisoformat(drop)
        run_time = datetime.combine(d + timedelta(days=1), time(2, rng.randint(0, 20), rng.randint(0, 59)))
        with engine.connect() as conn:  # never start before the last recorded run ended
            last = conn.execute(select(func.max(etl_run.c.finished_at))).scalar_one()
        if last and run_time <= last:
            run_time = last.replace(microsecond=0) + timedelta(minutes=rng.randint(20, 50))
        s = run_pipeline(settings, as_of=d, run_time=run_time, trigger="schedule", engine=engine)
        totals[s.status] += 1
        totals["rows"] += s.rows_loaded
        warns = sum(c.status == "warn" for o in s.outcomes for c in o.checks)
        line = f"{drop}  run {s.run_id:>3}  {s.status:<8} {s.rows_loaded:>7,} rows"
        line += f"  {warns} warning(s)" if warns else ""
        line += f"  {s.message}" if s.status == "failed" else ""
        print(line)
        if s.status == "failed":
            if not args.with_agent:
                sys.exit(1)
            _agent(settings, as_of=d, run_time=run_time + timedelta(minutes=4))
            if _latest_status(settings) == "failed":
                print("pipeline is on hold until a person acts on the incident")
                sys.exit(1)
    print(f"\n{totals['success']} nightly runs succeeded, {totals['rows']:,} rows loaded")
    _marts(settings)


def cmd_status(args: argparse.Namespace) -> None:
    engine = get_engine(_settings(args).db_url)
    with engine.connect() as conn:
        runs = conn.execute(select(etl_run).order_by(desc(etl_run.c.run_id)).limit(args.limit)).all()
    for r in runs:
        print(f"run {r.run_id:>4}  {r.started_at:%Y-%m-%d %H:%M}  {r.trigger_type:<8} {r.status:<8} "
              f"drops={r.drops_loaded} rows={r.rows_loaded:,}" + (f"  {r.message}" if r.message else ""))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="clarity-etl")
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="simulate Clarity and write nightly extract drops")
    gen.add_argument("--start", default="2026-07-01")
    gen.add_argument("--end", default="2026-11-17")
    gen.add_argument("--seed", type=int, default=42)
    gen.add_argument("--patients", type=int, default=4000, help="existing patient panel size")
    gen.add_argument("--outbox", default="data/sftp/outbox")
    gen.add_argument("--only-date", help="write just this night's drop (a clean re-send)")
    gen.set_defaults(func=cmd_generate)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--db", help="SQLAlchemy URL (default: env CLARITY_ETL_DB_URL or SQLite)")
        p.add_argument("--source", help="sftp://user@host:port/path or a local outbox folder")

    run = sub.add_parser("run", help="pull and load any new drops")
    common(run)
    run.add_argument("--as-of", help="only consider drops up to this date")
    run.add_argument("--trigger", default="manual")
    run.add_argument("--with-agent", action="store_true", help="investigate failures with the agent")
    run.set_defaults(func=cmd_run)

    back = sub.add_parser("backfill", help="load every available drop as nightly runs")
    common(back)
    back.add_argument("--until", help="stop after this drop date")
    back.add_argument("--with-agent", action="store_true")
    back.set_defaults(func=cmd_backfill)

    agent = sub.add_parser("agent", help="investigate the latest failure and file an incident")
    common(agent)
    agent.set_defaults(func=cmd_agent)

    brk = sub.add_parser("break", help="damage one night's drop to demo failure handling")
    common(brk)
    brk.add_argument("scenario", choices=["transfer_corruption", "schema_drift", "orphan_records",
                                          "bad_values"])
    brk.add_argument("--date", required=True)
    brk.set_defaults(func=cmd_break)

    marts = sub.add_parser("marts", help="rebuild the reporting marts")
    common(marts)
    marts.set_defaults(func=cmd_marts)

    site = sub.add_parser("site", help="build the static dashboard (for GitHub Pages)")
    common(site)
    site.add_argument("--out", default="site")
    site.set_defaults(func=cmd_site)

    status = sub.add_parser("status", help="show recent pipeline runs")
    common(status)
    status.add_argument("--limit", type=int, default=10)
    status.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
