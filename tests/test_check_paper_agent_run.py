from __future__ import annotations

import importlib.util
import sqlite3
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "scripts" / "ops" / "check-paper-agent-run.py"
DAY = "2026-08-20"


def fixture(tmp_path: Path, *, orders: int = 0, fills: int = 0):
    bundles = tmp_path / "bundles"
    bundles.mkdir()
    (bundles / f"{DAY}.json").write_text("{}\n", encoding="utf-8")
    handoff = tmp_path / "handoff.env"
    handoff.write_text(
        f"PAPER_AGENT_SESSION_ID={DAY}\nPAPER_AGENT_CYCLE_AT={DAY}T16:10:02+09:00\n",
        encoding="utf-8",
    )
    db = tmp_path / "paper.sqlite3"
    conn = sqlite3.connect(db)
    for table in (
        "paper_nav_snapshots", "paper_agent_decisions", "paper_cycle_commits",
        "paper_order_proposals", "paper_risk_reviews", "paper_orders", "paper_fills",
    ):
        conn.execute(f'CREATE TABLE "{table}" (value INTEGER)')
    conn.executemany("INSERT INTO paper_orders VALUES (?)", [(1,)] * orders)
    conn.executemany("INSERT INTO paper_fills VALUES (?)", [(1,)] * fills)
    conn.commit()
    conn.close()
    return bundles, handoff, db


def run_checker(tmp_path: Path, bundles: Path, handoff: Path, db: Path):
    return subprocess.run(
        [sys.executable, str(CHECKER), "--skip-systemd", "--date", DAY,
         "--repo-root", str(ROOT), "--bundle-dir", str(bundles),
         "--handoff", str(handoff), "--paper-db", str(db)],
        text=True, capture_output=True, check=False,
    )


def test_pass_fixture(tmp_path):
    result = run_checker(tmp_path, *fixture(tmp_path))
    assert result.returncode == 0
    assert result.stdout.splitlines()[0] == "PASS"
    assert "orders=0 fills=0" in result.stdout


@pytest.mark.parametrize(("orders", "fills"), [(1, 0), (0, 1)])
def test_orders_or_fills_fail(tmp_path, orders, fills):
    result = run_checker(tmp_path, *fixture(tmp_path, orders=orders, fills=fills))
    assert result.returncode == 1
    assert result.stdout.startswith("FAILED\n")
    assert "economic paper activity detected" in result.stdout


def test_missing_bundle_is_blocked(tmp_path):
    bundles, handoff, db = fixture(tmp_path)
    (bundles / f"{DAY}.json").unlink()
    result = run_checker(tmp_path, bundles, handoff, db)
    assert result.returncode == 2
    assert result.stdout.startswith("BLOCKED\n")
    assert "bundle missing" in result.stdout


def test_handoff_date_mismatch_is_blocked(tmp_path):
    bundles, handoff, db = fixture(tmp_path)
    handoff.write_text(
        "PAPER_AGENT_SESSION_ID=2026-08-19\nPAPER_AGENT_CYCLE_AT=2026-08-19T16:10:00+09:00\n"
    )
    result = run_checker(tmp_path, bundles, handoff, db)
    assert result.returncode == 2
    assert "handoff session date mismatch" in result.stdout


def test_missing_expected_paper_table_is_blocked(tmp_path):
    bundles, handoff, db = fixture(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute("DROP TABLE paper_orders")
    conn.commit()
    conn.close()
    result = run_checker(tmp_path, bundles, handoff, db)
    assert result.returncode == 2
    assert "expected paper tables missing: orders" in result.stdout


def load_checker_module():
    spec = importlib.util.spec_from_file_location("paper_run_checker", CHECKER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_production_defaults_match_activation_paths(monkeypatch):
    module = load_checker_module()
    monkeypatch.setattr(sys, "argv", [str(CHECKER)])
    args = module.parse_args()
    assert args.paper_db == Path("/var/lib/kronostock/paper-trading.sqlite3")
    assert args.bundle_dir == Path("/var/lib/kronostock/paper-bundles")
    assert args.handoff == Path("/run/kronostock/paper-agent-cycle.env")


def test_active_enabled_dry_run_timer_is_expected(monkeypatch):
    module = load_checker_module()
    report = module.Report()

    def fake_properties(unit, names, report):
        values = {"ActiveState": "active", "UnitFileState": "enabled", "Result": "success", "SubState": "dead", "ExecMainStartTimestamp": "Thu 2026-08-20 16:10:16 KST"}
        if unit == "kronostock-paper-agent.timer":
            values["UnitFileState"] = "static"
        report.lines.append(f"INFO: {unit} fake")
        return values

    def fake_command(args):
        return True, "Fri 2026-08-21 16:10:00 KST kronostock-paper-agent.timer kronostock-paper-agent.service\nksf ok"

    monkeypatch.setattr(module, "properties", fake_properties)
    monkeypatch.setattr(module, "command", fake_command)
    module.check_systemd(module.dt.date.fromisoformat(DAY), report)
    assert report.status == "PASS"


def test_database_is_copied_before_connect(tmp_path):
    _, _, source = fixture(tmp_path)
    module = load_checker_module()
    connected_paths = []

    def tracking_connect(database, **kwargs):
        assert kwargs == {"uri": True}
        copied = Path(unquote(urlparse(database).path))
        assert copied != source
        assert copied.read_bytes() == source.read_bytes()
        connected_paths.append(copied)
        return sqlite3.connect(database, **kwargs)

    counts = module.inspect_database_copy(source, connector=tracking_connect)
    assert connected_paths and counts["orders"] == 0
