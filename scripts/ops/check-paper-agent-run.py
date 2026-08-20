#!/usr/bin/env python3
"""Read-only post-run checker for the KronosStock paper agent."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
import pwd
import shutil
import sqlite3
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Callable
from urllib.parse import quote
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
TABLES = {
    "nav_snapshots": "paper_nav_snapshots",
    "decisions": "paper_agent_decisions",
    "cycle_commits": "paper_cycle_commits",
    "proposals": "paper_order_proposals",
    "reviews": "paper_risk_reviews",
    "orders": "paper_orders",
    "fills": "paper_fills",
}
RANK = {"PASS": 0, "BLOCKED": 1, "FAILED": 2}


class Report:
    def __init__(self) -> None:
        self.status = "PASS"
        self.lines: list[str] = []

    def add(self, status: str, message: str) -> None:
        if RANK[status] > RANK[self.status]:
            self.status = status
        self.lines.append(f"{status}: {message}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def describe(path: Path) -> str:
    info = path.stat(follow_symlinks=False)
    try:
        owner = pwd.getpwuid(info.st_uid).pw_name
    except KeyError:
        owner = str(info.st_uid)
    return (
        f"path={path} mode={stat.S_IMODE(info.st_mode):04o} "
        f"owner={owner}({info.st_uid}) size={info.st_size} "
        f"mtime={dt.datetime.fromtimestamp(info.st_mtime, KST).isoformat()} sha256={sha256(path)}"
    )


def regular_file(path: Path, label: str, report: Report) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        report.add("BLOCKED", f"{label} missing: {path}")
        return False
    except OSError as exc:
        report.add("BLOCKED", f"cannot stat {label}: {exc}")
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        report.add("FAILED", f"{label} must be a regular non-symlink: {path}")
        return False
    return True


def inspect_database_copy(
    source: Path,
    connector: Callable[..., sqlite3.Connection] = sqlite3.connect,
) -> dict[str, int | None]:
    """Copy through a no-follow fd, then connect only to the private copy."""
    with tempfile.TemporaryDirectory(prefix="kronostock-paper-check-") as temp_dir:
        copied = Path(temp_dir) / "paper.sqlite3"
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        source_fd = os.open(source, flags)
        try:
            if not stat.S_ISREG(os.fstat(source_fd).st_mode):
                raise ValueError("paper DB is not a regular file")
            with os.fdopen(source_fd, "rb", closefd=False) as src, copied.open("xb") as dst:
                os.chmod(copied, 0o600)
                shutil.copyfileobj(src, dst)
        finally:
            os.close(source_fd)
        uri = f"file:{quote(str(copied))}?mode=ro&immutable=1"
        conn = connector(uri, uri=True)
        try:
            existing = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_schema WHERE type='table'"
                )
            }
            return {
                label: (conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
                        if table in existing else None)
                for label, table in TABLES.items()
            }
        finally:
            conn.close()


def require_count(counts: dict[str, int | None], key: str) -> int:
    value = counts[key]
    if value is None:
        raise AssertionError(f"count {key} unexpectedly missing after missing-table gate")
    return value


def command(args: list[str]) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            args, check=False, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    return result.returncode == 0, result.stdout.strip()


def properties(unit: str, names: list[str], report: Report) -> dict[str, str] | None:
    ok, output = command(["systemctl", "show", unit, "--no-pager", *[f"-p{x}" for x in names]])
    if not ok:
        report.add("BLOCKED", f"systemd query failed for {unit}: {output or 'no output'}")
        return None
    values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    report.lines.append(f"INFO: {unit} " + " ".join(f"{name}={values.get(name, '')}" for name in names))
    return values


def check_systemd(day: dt.date, report: Report) -> None:
    paper_timer = properties("kronostock-paper-agent.timer", ["ActiveState", "UnitFileState"], report)
    if paper_timer and (paper_timer.get("ActiveState") != "active" or paper_timer.get("UnitFileState") != "static"):
        report.add("BLOCKED", "paper-agent timer is not active + static")
    service = properties(
        "kronostock-paper-agent.service",
        ["ActiveState", "SubState", "Result", "ExecMainStartTimestamp"], report,
    )
    if service and service.get("Result") not in ("success", ""):
        report.add("FAILED", f"paper-agent service result={service.get('Result')}")
    if service and day.isoformat() not in service.get("ExecMainStartTimestamp", ""):
        report.add("BLOCKED", "paper-agent service has no run timestamp for the session date")
    ksf_service = properties(
        "kronostock-ksf.service",
        ["ActiveState", "SubState", "Result", "ExecMainStartTimestamp"], report,
    )
    if ksf_service and ksf_service.get("Result") not in ("success", ""):
        report.add("FAILED", f"KSF service result={ksf_service.get('Result')}")
    ksf_timer = properties("kronostock-ksf.timer", ["ActiveState", "UnitFileState"], report)
    if ksf_timer and (ksf_timer.get("ActiveState") != "active"
                      or ksf_timer.get("UnitFileState") != "enabled"):
        report.add("BLOCKED", "KSF timer is not active + enabled")
    # The dry-run timer is an existing, separate reporting pipeline. The paper-agent
    # activation contract preserves it unchanged, so active+enabled is expected.
    dry_timer = properties("kronostock-dry-run.timer", ["ActiveState", "UnitFileState"], report)
    if dry_timer and (dry_timer.get("ActiveState") != "active"
                      or dry_timer.get("UnitFileState") != "enabled"):
        report.add("BLOCKED", "dry-run timer is not active + enabled")
    ok, timers = command([
        "systemctl", "list-timers", "kronostock-paper-agent.timer",
        "kronostock-ksf.timer", "kronostock-dry-run.timer", "--no-pager", "--all",
    ])
    if ok:
        compact = " | ".join(line.strip() for line in timers.splitlines() if "kronostock-" in line)
        report.lines.append(f"INFO: next triggers: {compact or 'none listed'}")
    else:
        report.add("BLOCKED", f"timer trigger query failed: {timers or 'no output'}")
    since = f"{day.isoformat()} 16:09:00 +0900"
    until = f"{day.isoformat()} 16:16:00 +0900"
    ok, journal = command([
        "journalctl", "-u", "kronostock-ksf.service", "--since", since,
        "--until", until, "--no-pager", "-o", "cat",
    ])
    if ok:
        entries = sum(1 for line in journal.splitlines() if line.strip())
        report.lines.append(f"INFO: KSF journal window={since}..{until} entries={entries}")
        if entries == 0:
            report.add("BLOCKED", "no KSF journal evidence around 16:10 KST")
    else:
        report.add("BLOCKED", f"KSF journal query failed: {journal or 'no output'}")


def parse_args() -> argparse.Namespace:
    repo_default = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="KST session date (YYYY-MM-DD)")
    parser.add_argument("--repo-root", type=Path, default=repo_default)
    parser.add_argument("--paper-db", type=Path, default=Path("/var/lib/kronostock/paper-trading.sqlite3"))
    parser.add_argument("--bundle-dir", type=Path, default=Path("/var/lib/kronostock/paper-bundles"))
    parser.add_argument("--handoff", type=Path, default=Path("/run/kronostock/paper-agent-cycle.env"))
    parser.add_argument("--skip-systemd", "--no-systemd", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    now = dt.datetime.now(KST)
    try:
        day = dt.date.fromisoformat(args.date) if args.date else now.date()
    except ValueError:
        print("FAILED")
        print(f"FAILED: invalid --date: {args.date!r}")
        return 1
    report = Report()
    report.lines.append(f"INFO: kst_now={now.isoformat()} session_date={day.isoformat()} repo_root={args.repo_root}")
    if args.skip_systemd:
        report.lines.append("INFO: systemd/journal checks skipped")
    else:
        check_systemd(day, report)

    bundle = args.bundle_dir / f"{day.isoformat()}.json"
    if regular_file(bundle, "bundle", report):
        report.lines.append(f"INFO: bundle {describe(bundle)}")

    if regular_file(args.handoff, "handoff", report):
        report.lines.append(f"INFO: handoff {describe(args.handoff)}")
        try:
            lines = args.handoff.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            report.add("BLOCKED", f"cannot read handoff: {exc}")
        else:
            expected_session = f"PAPER_AGENT_SESSION_ID={day.isoformat()}"
            cycle_prefix = f"PAPER_AGENT_CYCLE_AT={day.isoformat()}T"
            if len(lines) != 2:
                report.add("BLOCKED", f"handoff must contain exactly two lines; found {len(lines)}")
            if len(lines) >= 1 and lines[0] != expected_session:
                report.add("BLOCKED", f"handoff session date mismatch: expected {expected_session}")
            if len(lines) >= 2 and (not lines[1].startswith(cycle_prefix) or not lines[1].endswith("+09:00")):
                report.add("BLOCKED", f"handoff cycle_at must use session date {day} and +09:00")
            if len(lines) == 2:
                try:
                    parsed = dt.datetime.fromisoformat(lines[1].split("=", 1)[1])
                    if parsed.date() != day or parsed.utcoffset() != dt.timedelta(hours=9):
                        raise ValueError
                except (IndexError, ValueError):
                    report.add("BLOCKED", "handoff cycle_at is not a valid +09:00 ISO timestamp")

    if regular_file(args.paper_db, "paper DB", report):
        report.lines.append(f"INFO: paper DB {describe(args.paper_db)}")
        sidecars = [Path(str(args.paper_db) + suffix) for suffix in ("-wal", "-shm", "-journal")]
        present = [str(path) for path in sidecars if path.exists() or path.is_symlink()]
        if present:
            report.add("BLOCKED", "paper DB sidecars present: " + ", ".join(present))
        else:
            try:
                counts = inspect_database_copy(args.paper_db)
            except (OSError, sqlite3.Error, ValueError) as exc:
                report.add("BLOCKED", f"cannot inspect private paper DB copy: {exc}")
            else:
                rendered = " ".join(f"{key}={'N/A' if value is None else value}" for key, value in counts.items())
                report.lines.append(f"INFO: counts {rendered}")
                missing_tables = [key for key, value in counts.items() if value is None]
                if missing_tables:
                    report.add("BLOCKED", "expected paper tables missing: " + ", ".join(missing_tables))
                else:
                    orders = require_count(counts, "orders")
                    fills = require_count(counts, "fills")
                    proposals = require_count(counts, "proposals")
                    reviews = require_count(counts, "reviews")
                    if orders > 0 or fills > 0:
                        report.add("FAILED", "economic paper activity detected (orders or fills > 0)")
                    elif proposals > 0 or reviews > 0:
                        report.add("BLOCKED", "proposals/reviews require operator review")

    print(report.status)
    print("\n".join(report.lines))
    return {"PASS": 0, "BLOCKED": 2, "FAILED": 1}[report.status]


if __name__ == "__main__":
    raise SystemExit(main())
