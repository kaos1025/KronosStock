from __future__ import annotations

import ast
import base64
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dashboard.app import _paper_time, app
from strategy.paper_web_api import _connection, get_account
from paper_trading.ledger import PaperLedger


TOKEN = "paper-read-secret"
USER = "paper-viewer"
PASSWORD = "basic-read-secret"
SHA_A, SHA_B, SHA_C = "a" * 64, "b" * 64, "c" * 64
T0 = "2026-08-08T08:00:00+09:00"
CUTOFF = "2026-08-08T07:59:00+09:00"


def _decision(ledger: PaperLedger, tag: str, symbol: str, action: str, reason: str, at: str,
              *, account_id: str = "acct") -> None:
    ledger.append_agent_decision(decision_id=f"d-{tag}", account_id=account_id, symbol=symbol,
        ksf_run_id=f"run-{tag}", ksf_decision_id=f"ksf-{tag}", feature_snapshot_sha256=SHA_A,
        available_data_cutoff=CUTOFF, action=action, reason_code=reason,
        rationale_sha256=SHA_B, created_at=at)


def _proposal(ledger: PaperLedger, tag: str, *, reject: bool = False,
              order: bool = False, fill: bool = False) -> None:
    ledger.append_order_proposal(proposal_id=f"p-{tag}", decision_id=f"d-{tag}", side="BUY",
        target_exposure_bp=1000, idempotency_key=f"pi-{tag}", model_name="private-model",
        model_version="private-version", proposed_at="2026-08-08T09:00:00+09:00")
    if reject:
        ledger.append_risk_review(review_id=f"r-{tag}", proposal_id=f"p-{tag}", verdict="REJECT",
            reject_reason_code="POSITION_LIMIT", policy_sha256=SHA_C,
            reviewed_at="2026-08-08T09:01:00+09:00")
    elif order:
        ledger.append_risk_review(review_id=f"r-{tag}", proposal_id=f"p-{tag}", verdict="APPROVE",
            approved_quantity=10, reference_price_krw=70_000, policy_sha256=SHA_C,
            reviewed_at="2026-08-08T09:01:00+09:00")
        ledger.append_order(order_id=f"o-{tag}", review_id=f"r-{tag}", idempotency_key=f"oi-{tag}",
            created_at="2026-08-08T09:02:00+09:00")
        if fill:
            ledger.append_fill(fill_id=f"f-{tag}", order_id=f"o-{tag}", fill_price_krw=70_000,
                quantity=10, fee_krw=100, tax_krw=0, slippage_krw=0,
                observed_at="2026-08-08T09:03:00+09:00", observation_sha256=SHA_A,
                filled_at="2026-08-08T09:03:00+09:00")


def _seed(path: Path) -> None:
    with PaperLedger(path) as ledger:
        ledger.create_account(account_id="acct", initial_cash_krw=10_000_000,
            policy_version="private-policy", created_at=T0)
        ledger.append_account_event(account_event_id="enabled", account_id="acct", event_type="ENABLED",
            reason_code="MANUAL", event_at="2026-08-08T08:01:00+09:00")
        ledger.append_kill_switch_event(kill_switch_event_id="engaged", account_id="acct",
            event_type="ENGAGED", reason_code="STARTUP_DEFAULT", event_at="2026-08-08T08:02:00+09:00")
        ledger.append_kill_switch_event(kill_switch_event_id="released", account_id="acct",
            event_type="RELEASED", reason_code="MANUAL", event_at="2026-08-08T08:03:00+09:00")
        rows = (("abstain", "005930", "ABSTAIN", "NO_EDGE"),
                ("hold", "005930", "HOLD", "NO_EDGE"),
                ("proposed", "005930", "ENTER", "FLOW_SIGNAL_POSITIVE"),
                ("rejected", "000660", "ENTER", "FLOW_SIGNAL_POSITIVE"),
                ("queued", "000660", "ENTER", "FLOW_SIGNAL_POSITIVE"),
                ("terminal-a", "000660", "ENTER", "FLOW_SIGNAL_POSITIVE"),
                ("terminal-b", "000660", "ENTER", "FLOW_SIGNAL_POSITIVE"),
                ("filled", "005930", "ENTER", "FLOW_SIGNAL_POSITIVE"))
        for i, row in enumerate(rows):
            _decision(ledger, *row, f"2026-08-08T08:{10+i:02d}:00+09:00")
        _proposal(ledger, "proposed")
        _proposal(ledger, "rejected", reject=True)
        _proposal(ledger, "queued", order=True)
        _proposal(ledger, "terminal-a", order=True)
        ledger.append_order_event(order_event_id="oe-terminal-a", order_id="o-terminal-a",
            event_type="CANCELLED", event_at="2026-08-08T09:03:00+09:00")
        _proposal(ledger, "terminal-b", order=True)
        ledger.append_order_event(order_event_id="oe-terminal-b", order_id="o-terminal-b",
            event_type="EXPIRED", event_at="2026-08-08T09:03:00+09:00")
        _proposal(ledger, "filled", order=True, fill=True)
        ledger.append_nav_snapshot(nav_snapshot_id="peak", account_id="acct", session_date="2026-08-08",
            cash_krw=9_299_900, position_value_krw=800_000, nav_krw=10_099_900,
            position_marks={"005930": {"quantity": 10, "price_krw": 80_000,
                "feature_snapshot_sha256": SHA_A, "observed_at": "2026-08-08T09:04:00+09:00"}},
            snapshot_at="2026-08-08T09:04:00+09:00")
        # Same timestamp: the Task 6 cycle NAV must beat a non-cycle snapshot.
        ledger.append_nav_snapshot(nav_snapshot_id="paper-fill-safe:nav", account_id="acct", session_date="2026-08-08",
            cash_krw=9_299_900, position_value_krw=690_000, nav_krw=9_989_900,
            position_marks={"005930": {"quantity": 10, "price_krw": 69_000,
                "feature_snapshot_sha256": SHA_A, "observed_at": "2026-08-08T10:00:00+09:00"}},
            snapshot_at="2026-08-08T10:00:00+09:00")
        ledger.append_nav_snapshot(nav_snapshot_id="paper-cycle-safe:nav", account_id="acct", session_date="2026-08-08",
            cash_krw=9_299_900, position_value_krw=700_000, nav_krw=9_999_900,
            position_marks={"005930": {"quantity": 10, "price_krw": 70_000,
                "feature_snapshot_sha256": SHA_A, "observed_at": "2026-08-08T10:00:00+09:00"}},
            snapshot_at="2026-08-08T10:00:00+09:00")
        ledger.create_account(account_id="other", initial_cash_krw=999_999_999,
            policy_version="do-not-leak", created_at=T0)
        _decision(ledger, "other-secret", "005930", "ENTER", "FLOW_SIGNAL_POSITIVE",
                  "2026-08-08T12:00:00+09:00", account_id="other")
        ledger.append_order_proposal(proposal_id="p-other-secret", decision_id="d-other-secret", side="BUY",
            target_exposure_bp=1000, idempotency_key="pi-other-secret", model_name="other-private-model",
            model_version="other-private-version", proposed_at="2026-08-08T12:01:00+09:00")
        ledger.append_risk_review(review_id="r-other-secret", proposal_id="p-other-secret", verdict="APPROVE",
            approved_quantity=1, reference_price_krw=1, policy_sha256=SHA_C,
            reviewed_at="2026-08-08T12:02:00+09:00")
        ledger.append_order(order_id="o-other-secret", review_id="r-other-secret",
            idempotency_key="oi-other-secret", created_at="2026-08-08T12:03:00+09:00")
        ledger.append_kill_switch_event(kill_switch_event_id="engaged-after-nav", account_id="acct",
            event_type="ENGAGED", reason_code="MANUAL",
            event_at="2026-08-08T10:01:00+09:00")


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, Path]:
    path = tmp_path / "paper.sqlite3"
    _seed(path)
    monkeypatch.setenv("PAPER_LEDGER_DB_PATH", str(path))
    monkeypatch.setenv("PAPER_ACCOUNT_ID", "acct")
    monkeypatch.setenv("KSF_READ_TOKEN", TOKEN)
    monkeypatch.setenv("KSF_READ_USERNAME", USER)
    monkeypatch.setenv("KSF_READ_PASSWORD", PASSWORD)
    return TestClient(app), path


def _bearer() -> dict[str, str]: return {"Authorization": f"Bearer {TOKEN}"}
def _basic() -> dict[str, str]:
    value = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
    return {"Authorization": f"Basic {value}"}


def _drop_guards(conn: sqlite3.Connection, table: str) -> None:
    names = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,))]
    for name in names:
        conn.execute(f'DROP TRIGGER "{name}"')


def test_account_calculation_cycle_priority_and_html(client):
    http, _ = client
    response = http.get("/paper", headers=_bearer())
    assert response.status_code == 200
    body = response.text
    for value in ("9,999,900", "9,299,900", "700,000", "-100", "99", "작동 또는 확인 불가", "005930"):
        assert value in body
    statuses = {x["lifecycle_status"] for s in ("005930", "000660")
                for x in http.get(f"/paper/decisions/{s}", headers=_bearer()).json()["decisions"]}
    assert statuses == {"ai_proposed", "risk_rejected", "queued", "not_filled", "filled", "abstained", "no_action"}
    assert "other-secret" not in body and "MANUAL" not in body
    lower = body.lower()
    for forbidden in ("<form", "<button", "method=post", "<script", "http://", "https://"):
        assert forbidden not in lower
    assert 'name="viewport"' in lower and "@media" in lower and "min-height:44px" in lower
    assert "<h1" in lower and "<nav" in lower and "overflow-x:hidden" in lower
    assert ".paper-tag{display:inline-block;padding:.2rem .45rem;background:#e8eef8;color:#17202a;" in lower
    assert "@media(min-width:48rem){.paper-grid{grid-template-columns:repeat(3,1fr)}" in lower
    assert "@media(max-width:47.99rem)" in lower
    assert ".paper-table thead{display:none}" in lower
    assert ".paper-table,.paper-table tbody,.paper-table tr,.paper-table td{display:block}" in lower
    assert ".paper-table td::before{content:attr(data-label)" in lower
    for label in ("종목", "수량", "기준가", "평가액", "판단", "상태", "시각"):
        assert f'data-label="{label}"' in body
    assert 'data-label="안내" colspan="4"' not in body
    assert "2026-08-08 08:10 kst" in lower
    assert "t08:" not in lower


def test_json_exact_shapes_lifecycle_and_nondisclosure(client):
    http, _ = client
    orders = http.get("/paper/orders", headers=_bearer()).json()
    assert set(orders) == {"status", "orders"}
    assert [x["lifecycle_status"] for x in orders["orders"]] == [
        "filled", "not_filled", "not_filled", "queued", "risk_rejected", "ai_proposed"]
    assert [x["lifecycle_label"] for x in orders["orders"] if x["lifecycle_status"] == "not_filled"] == ["미체결 종료"] * 2
    order_keys = {"decision_id", "symbol", "decided_at", "action", "action_label", "reason",
        "reason_label", "lineage", "proposal", "risk", "order", "fill", "lifecycle_status", "lifecycle_label"}
    assert all(set(x) == order_keys for x in orders["orders"])
    assert set(orders["orders"][0]["lineage"]) == {"ksf_run_id", "ksf_decision_id", "feature_snapshot_sha256", "available_data_cutoff"}
    assert set(orders["orders"][0]["proposal"]) == {"side", "target_exposure_bps", "proposed_at"}
    assert set(orders["orders"][0]["risk"]) == {"verdict", "verdict_label", "reason", "reason_label", "reviewed_at"}
    assert set(orders["orders"][0]["order"]) == {"quantity", "created_at"}
    assert set(orders["orders"][0]["fill"]) == {"price_krw", "quantity", "fee_krw", "tax_krw", "filled_at"}
    text = json.dumps(orders, ensure_ascii=False).lower()
    for forbidden in ("rationale_sha256", "policy_sha256", "model_name", "model_version",
                      "idempotency", "private-model", "private-policy", "paper.sqlite3", str(client[1]).lower(),
                      "cancelled", "expired", "other-secret"):
        assert forbidden not in text


@pytest.mark.parametrize("event_type", ["CANCELLED", "EXPIRED"])
def test_terminal_order_events_are_generalized_without_raw_values(client, event_type):
    http, _ = client
    response = http.get("/paper/orders", headers=_bearer())
    assert any(row["lifecycle_status"] == "not_filled" for row in response.json()["orders"])
    assert event_type not in response.text


@pytest.mark.parametrize("corruption", ["filled_without_fill", "fill_without_filled", "missing", "unknown"])
def test_contradictory_or_unknown_order_state_is_unavailable(client, corruption):
    http, path = client
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA ignore_check_constraints=ON")
    _drop_guards(conn, "paper_order_events")
    if corruption == "filled_without_fill":
        conn.execute("UPDATE paper_order_events SET event_type='FILLED' WHERE order_id='o-queued'")
        tag = "queued"
    elif corruption == "fill_without_filled":
        conn.execute("UPDATE paper_order_events SET event_type='CANCELLED' WHERE order_id='o-filled' AND seq=(SELECT MAX(seq) FROM paper_order_events WHERE order_id='o-filled')")
        tag = "filled"
    elif corruption == "missing":
        conn.execute("DELETE FROM paper_order_events WHERE order_id='o-queued'")
        tag = "queued"
    else:
        conn.execute("UPDATE paper_order_events SET event_type='SECRET_EVENT' WHERE order_id='o-queued'")
        tag = "queued"
    conn.commit(); conn.close()
    response = http.get("/paper/orders", headers=_bearer())
    row = next(row for row in response.json()["orders"] if row["decision_id"] == f"d-{tag}")
    assert row["lifecycle_status"] == "unavailable"
    assert "SECRET_EVENT" not in response.text


@pytest.mark.parametrize("corruption", ["marks_mismatch", "unknown_nested", "cash_mismatch"])
def test_malformed_trusted_ledger_account_fails_closed_without_leaks(client, corruption):
    http, path = client
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA ignore_check_constraints=ON")
    _drop_guards(conn, "paper_nav_snapshots")
    if corruption == "marks_mismatch":
        value = json.dumps({"SECRET_SYMBOL": {"quantity": 10, "price_krw": 70_000,
            "feature_snapshot_sha256": SHA_A, "observed_at": CUTOFF}})
        conn.execute("UPDATE paper_nav_snapshots SET position_marks_json=? WHERE nav_snapshot_id='paper-cycle-safe:nav'", (value,))
    elif corruption == "unknown_nested":
        value = json.dumps({"005930": {"quantity": 10, "price_krw": 70_000,
            "feature_snapshot_sha256": SHA_A, "observed_at": CUTOFF, "SECRET_FIELD": "/secret/path"}})
        conn.execute("UPDATE paper_nav_snapshots SET position_marks_json=? WHERE nav_snapshot_id='paper-cycle-safe:nav'", (value,))
    else:
        conn.execute("UPDATE paper_nav_snapshots SET cash_krw=123 WHERE nav_snapshot_id='paper-cycle-safe:nav'")
    conn.commit(); conn.close()
    assert get_account() == {"status": "unavailable"}
    response = http.get("/paper", headers=_bearer())
    assert "데이터 없음" in response.text
    assert 'data-label="안내" colspan="4">데이터 없음</td>' in response.text
    assert "SECRET_SYMBOL" not in response.text and "SECRET_FIELD" not in response.text
    assert "/secret/path" not in response.text and str(path) not in response.text


def test_connection_body_sqlite_error_propagates_once_and_connection_closes(client):
    _, _ = client
    with pytest.raises(sqlite3.OperationalError, match="body failure"):
        with _connection() as conn:
            assert conn is not None
            raise sqlite3.OperationalError("body failure")
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_dashboard_sorts_valid_timestamps_newest_first_and_unavailable_last(client):
    http, path = client
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA ignore_check_constraints=ON")
    _drop_guards(conn, "paper_agent_decisions")
    conn.execute("UPDATE paper_agent_decisions SET created_at='SECRET_SORT_VALUE' WHERE decision_id='d-filled'")
    conn.commit(); conn.close()
    body = http.get("/paper", headers=_bearer()).text
    assert "SECRET_SORT_VALUE" not in body
    assert body.index("2026-08-08 08:10 KST") < body.index('data-label="시각">—</td>')


def test_dashboard_timestamp_formatter_converts_to_kst_and_fails_closed():
    assert _paper_time("2026-08-07T23:10:00+00:00") == "2026-08-08 08:10 KST"
    for value in (None, "", "2026-08-08T08:10:00", "SECRET_MALFORMED_TIME"):
        assert _paper_time(value) == "—"


def test_unknown_internal_enums_and_text_are_generalized(client):
    http, path = client
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA ignore_check_constraints=ON")
    conn.execute("""INSERT INTO paper_agent_decisions
        (decision_id,account_id,symbol,ksf_run_id,ksf_decision_id,feature_snapshot_sha256,
         available_data_cutoff,action,reason_code,rationale_sha256,created_at)
        VALUES ('d-unknown','acct','005930','safe-run','safe-decision',?,?,'SECRET_ACTION',
                'SECRET_REASON',?,'2026-08-08T11:00:00+09:00')""", (SHA_A, CUTOFF, SHA_B))
    conn.commit(); conn.close()
    response = http.get("/paper/decisions/005930", headers=_bearer())
    row = response.json()["decisions"][0]
    assert row["action"] == row["reason"] == row["lifecycle_status"] == "unavailable"
    combined = response.text + http.get("/paper", headers=_bearer()).text
    assert "SECRET_ACTION" not in combined and "SECRET_REASON" not in combined


@pytest.mark.parametrize("path", ["/paper", "/paper/orders", "/paper/decisions/005930"])
def test_auth_bearer_basic_and_failures(client, path):
    http, _ = client
    assert http.get(path, headers=_bearer()).status_code == 200
    assert http.get(path, headers=_basic()).status_code == 200
    for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic !!!"}):
        response = http.get(path, headers=headers)
        assert response.status_code == 401
        assert "paper.sqlite3" not in response.text.lower()


def test_unsupported_account_isolation_and_read_only_methods(client):
    http, path = client
    assert http.get("/paper/decisions/035420", headers=_bearer()).status_code == 404
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    before_counts = sqlite3.connect(path).execute("SELECT sum(cnt) FROM (SELECT count(*) cnt FROM paper_accounts UNION ALL SELECT count(*) FROM paper_agent_decisions UNION ALL SELECT count(*) FROM paper_orders)").fetchone()[0]
    for route in ("/paper", "/paper/orders", "/paper/decisions/005930"):
        http.get(route, headers=_bearer())
        for method in ("post", "put", "patch", "delete"):
            assert getattr(http, method)(route, headers=_bearer()).status_code in (404, 405)
    after_counts = sqlite3.connect(path).execute("SELECT sum(cnt) FROM (SELECT count(*) cnt FROM paper_accounts UNION ALL SELECT count(*) FROM paper_agent_decisions UNION ALL SELECT count(*) FROM paper_orders)").fetchone()[0]
    assert before_counts == after_counts and before == hashlib.sha256(path.read_bytes()).hexdigest()
    rules = [r for r in app.routes if getattr(r, "path", "").startswith("/paper") and r.path != "/paper/portfolio"]
    assert all(set(r.methods) <= {"GET", "HEAD"} for r in rules)


def test_missing_config_or_file_is_safe_and_does_not_create(tmp_path, monkeypatch):
    missing = tmp_path / "must-not-exist.sqlite3"
    monkeypatch.setenv("KSF_READ_TOKEN", TOKEN)
    monkeypatch.setenv("PAPER_ACCOUNT_ID", "acct")
    monkeypatch.setenv("PAPER_LEDGER_DB_PATH", str(missing))
    http = TestClient(app)
    assert http.get("/paper", headers=_bearer()).status_code == 200
    assert "데이터 없음" in http.get("/paper", headers=_bearer()).text
    assert http.get("/paper/orders", headers=_bearer()).json() == {"status": "no_data", "orders": []}
    assert not missing.exists() and str(missing) not in http.get("/paper", headers=_bearer()).text


def test_legacy_route_unchanged_and_static_dependency_boundary():
    route = next(r for r in app.routes if getattr(r, "path", None) == "/paper/portfolio")
    assert route.endpoint.__name__ == "paper_portfolio" and "_require_ksf_auth" not in repr(route.dependant.dependencies)
    source = Path("strategy/paper_web_api.py")
    tree = ast.parse(source.read_text(encoding="utf-8"))
    banned = {"requests", "httpx", "urllib", "socket", "subprocess", "kis", "broker", "paper_broker"}
    imports = {node.names[0].name.split(".")[0] for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom)) and node.names}
    calls = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert not (imports | calls) & banned
