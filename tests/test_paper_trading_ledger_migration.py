"""Task 2 — autonomous paper trading ledger migration tests.

대상: paper_trading/migrations/001_autonomous_paper_trading_ledgers.sql + paper_trading/ledger.py

경계 규칙:
- 전용 SQLite 파일 사용 (KSF 연구 ledger 와 분리). 테스트는 tmp_path 만 사용.
- KSF run_id/decision_id/feature_snapshot_sha256/available_data_cutoff 는 외부 참조 값
  (cross-database FK 없음).
- 모든 비즈니스/이벤트 테이블은 UPDATE/DELETE 를 trigger 로 거부한다.
"""
from __future__ import annotations

import ast
import sqlite3
import stat
from pathlib import Path

import pytest

from paper_trading.ledger import (
    LedgerConflictError,
    LedgerError,
    PaperLedger,
    SUPPORTED_SYMBOLS,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION = REPO_ROOT / "paper_trading" / "migrations" / "001_autonomous_paper_trading_ledgers.sql"
LEDGER_MODULE = REPO_ROOT / "paper_trading" / "ledger.py"

TS_EARLY = "2026-08-05T09:00:00+09:00"
TS_CREATE = "2026-08-05T16:05:00+09:00"
TS_CUTOFF = "2026-08-05T15:30:00+09:00"
TS_FILL = "2026-08-06T09:01:00+09:00"
SHA_A = "a" * 64
SHA_B = "b" * 64

APPEND_ONLY_TABLES = [
    "paper_accounts",
    "paper_account_events",
    "paper_agent_decisions",
    "paper_order_proposals",
    "paper_risk_reviews",
    "paper_orders",
    "paper_order_events",
    "paper_fills",
    "paper_cash_events",
    "paper_position_events",
    "paper_nav_snapshots",
    "paper_kill_switch_events",
]

REQUIRED_TABLES = ["paper_schema_versions", *APPEND_ONLY_TABLES]


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "paper_trading.sqlite3"


@pytest.fixture
def ledger(db_path: Path):
    led = PaperLedger(db_path)
    yield led
    led.close()


def seed_account(led: PaperLedger, account_id: str = "acct-1") -> str:
    return led.create_account(
        account_id=account_id,
        initial_cash_krw=10_000_000,
        policy_version="paper-policy-v1",
        created_at=TS_CREATE,
    )


def seed_decision(led: PaperLedger, decision_id: str = "dec-1", *, action: str = "BUY") -> str:
    return led.append_agent_decision(
        decision_id=decision_id,
        account_id="acct-1",
        symbol="005930",
        ksf_run_id="ksf-run-1",
        ksf_decision_id=f"ksf-{decision_id}",
        feature_snapshot_sha256=SHA_A,
        available_data_cutoff=TS_CUTOFF,
        action=action,
        reason_code="FLOW_SIGNAL_POSITIVE",
        rationale_sha256=SHA_B,
        created_at=TS_CREATE,
    )


def seed_proposal(led: PaperLedger, proposal_id: str = "prop-1", decision_id: str = "dec-1", **over) -> str:
    kwargs = dict(
        proposal_id=proposal_id,
        decision_id=decision_id,
        side="BUY",
        quantity=10,
        target_exposure_bp=1_000,
        idempotency_key=f"idem-{proposal_id}",
        model_name="kronos-mini",
        model_version="v1",
        proposed_at=TS_CREATE,
    )
    kwargs.update(over)
    return led.append_order_proposal(**kwargs)


def seed_review(led: PaperLedger, review_id: str = "rev-1", proposal_id: str = "prop-1", **over) -> str:
    kwargs = dict(
        review_id=review_id,
        proposal_id=proposal_id,
        verdict="APPROVE",
        approved_quantity=10,
        policy_sha256=SHA_A,
        reviewed_at=TS_CREATE,
    )
    kwargs.update(over)
    return led.append_risk_review(**kwargs)


def seed_order(led: PaperLedger, order_id: str = "ord-1", review_id: str = "rev-1", **over) -> str:
    kwargs = dict(
        order_id=order_id,
        review_id=review_id,
        idempotency_key=f"ordkey-{order_id}",
        created_at=TS_CREATE,
    )
    kwargs.update(over)
    return led.append_order(**kwargs)


def seed_fill(led: PaperLedger, fill_id: str = "fill-1", order_id: str = "ord-1", **over) -> str:
    kwargs = dict(
        fill_id=fill_id,
        order_id=order_id,
        fill_price_krw=70_000,
        quantity=10,
        fee_krw=105,
        tax_krw=0,
        slippage_krw=350,
        observed_at=TS_FILL,
        observation_sha256=SHA_A,
        filled_at=TS_FILL,
    )
    kwargs.update(over)
    return led.append_fill(**kwargs)


def seed_order_chain(led: PaperLedger, tag: str, *, action: str = "BUY", quantity: int = 10) -> str:
    """decision → proposal → APPROVE review → order 체인을 한 번에 구성."""
    seed_decision(led, f"dec-{tag}", action=action)
    seed_proposal(led, f"prop-{tag}", f"dec-{tag}", side=action, quantity=quantity)
    seed_review(led, f"rev-{tag}", f"prop-{tag}", approved_quantity=quantity)
    seed_order(led, f"ord-{tag}", f"rev-{tag}")
    return f"ord-{tag}"


def raw_insert_fill(conn: sqlite3.Connection, **over) -> None:
    row = dict(
        fill_id="fill-raw",
        order_id="ord-raw",
        account_id="acct-1",
        symbol="005930",
        side="BUY",
        fill_price_krw=70_000,
        quantity=10,
        fee_krw=105,
        tax_krw=0,
        slippage_krw=350,
        observed_at=TS_FILL,
        observation_sha256=SHA_A,
        filled_at=TS_FILL,
    )
    row.update(over)
    conn.execute(
        """
        INSERT INTO paper_fills
        (fill_id, order_id, account_id, symbol, side, fill_price_krw, quantity,
         fee_krw, tax_krw, slippage_krw, observed_at, observation_sha256, filled_at)
        VALUES (:fill_id, :order_id, :account_id, :symbol, :side, :fill_price_krw, :quantity,
                :fee_krw, :tax_krw, :slippage_krw, :observed_at, :observation_sha256, :filled_at)
        """,
        row,
    )


def raw_insert_order(conn: sqlite3.Connection, **over) -> None:
    row = dict(
        order_id="ord-mi",
        review_id="rev-mi",
        proposal_id="prop-mi",
        account_id="acct-1",
        symbol="005930",
        side="BUY",
        quantity=10,
        created_at=TS_CREATE,
    )
    row.update(over)
    row.setdefault("idempotency_key", f"ordkey-{row['order_id']}")
    conn.execute(
        """
        INSERT INTO paper_orders
        (order_id, review_id, proposal_id, account_id, symbol, side, quantity,
         idempotency_key, created_at)
        VALUES (:order_id, :review_id, :proposal_id, :account_id, :symbol, :side,
                :quantity, :idempotency_key, :created_at)
        """,
        row,
    )


@pytest.fixture
def seeded(ledger: PaperLedger) -> PaperLedger:
    """모든 append-only 테이블에 최소 1행씩 채운 ledger."""
    seed_account(ledger)
    seed_decision(ledger)
    seed_proposal(ledger)
    seed_review(ledger)
    seed_order(ledger)
    seed_fill(ledger)
    ledger.append_nav_snapshot(
        nav_snapshot_id="nav-1",
        account_id="acct-1",
        session_date="2026-08-06",
        cash_krw=9_299_895,
        position_value_krw=700_000,
        nav_krw=9_999_895,
        snapshot_at=TS_FILL,
    )
    ledger.append_kill_switch_event(
        kill_switch_event_id="ks-1",
        account_id="acct-1",
        event_type="ENGAGED",
        reason_code="STARTUP_DEFAULT",
        event_at=TS_CREATE,
    )
    return ledger


def table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {r[0] for r in rows}


# ---------------------------------------------------------------------------
# 마이그레이션 자체
# ---------------------------------------------------------------------------


def test_fresh_migration_creates_required_tables(ledger: PaperLedger) -> None:
    names = table_names(ledger.conn)
    missing = [t for t in REQUIRED_TABLES if t not in names]
    assert not missing, f"missing tables: {missing}"


def test_migration_is_rerunnable(db_path: Path) -> None:
    first = PaperLedger(db_path)
    seed_account(first)
    first.close()
    # 같은 파일에 재적용해도 실패/데이터 손실이 없어야 한다.
    second = PaperLedger(db_path)
    try:
        rows = second.conn.execute("SELECT COUNT(*) FROM paper_accounts").fetchone()[0]
        assert rows == 1
        versions = second.conn.execute("SELECT COUNT(*) FROM paper_schema_versions WHERE version = 1").fetchone()[0]
        assert versions == 1
    finally:
        second.close()


def test_migration_script_rerun_raw(ledger: PaperLedger) -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    ledger.conn.executescript(sql)  # 두 번째 적용이 예외 없이 통과해야 함
    assert "paper_orders" in table_names(ledger.conn)


def test_integrity_and_foreign_key_checks_pass(seeded: PaperLedger) -> None:
    assert seeded.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert seeded.conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_schema_version_recorded(ledger: PaperLedger) -> None:
    row = ledger.conn.execute(
        "SELECT name FROM paper_schema_versions WHERE version = 1"
    ).fetchone()
    assert row is not None
    assert row[0] == "001_autonomous_paper_trading_ledgers"


def test_supported_symbols_constant() -> None:
    assert SUPPORTED_SYMBOLS == frozenset({"005930", "000660"})


# ---------------------------------------------------------------------------
# 파일/디렉터리 권한
# ---------------------------------------------------------------------------


def test_file_modes(ledger: PaperLedger, db_path: Path) -> None:
    parent_mode = stat.S_IMODE(db_path.parent.stat().st_mode)
    file_mode = stat.S_IMODE(db_path.stat().st_mode)
    assert parent_mode == 0o700
    assert file_mode == 0o600


# ---------------------------------------------------------------------------
# append-only: 모든 비즈니스/이벤트 테이블은 UPDATE/DELETE 거부
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table", APPEND_ONLY_TABLES)
def test_update_rejected(seeded: PaperLedger, table: str) -> None:
    assert seeded.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] >= 1
    col = seeded.conn.execute(f"PRAGMA table_info({table})").fetchall()[0][1]
    with pytest.raises(sqlite3.IntegrityError):
        seeded.conn.execute(f"UPDATE {table} SET {col} = {col}")


@pytest.mark.parametrize("table", APPEND_ONLY_TABLES)
def test_delete_rejected(seeded: PaperLedger, table: str) -> None:
    assert seeded.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] >= 1
    with pytest.raises(sqlite3.IntegrityError):
        seeded.conn.execute(f"DELETE FROM {table}")


# ---------------------------------------------------------------------------
# 내부 FK 강제 (paper ledger 안에서만; KSF 참조는 FK 없이 값 복사)
# ---------------------------------------------------------------------------


def test_decision_requires_existing_account(ledger: PaperLedger) -> None:
    with pytest.raises((sqlite3.IntegrityError, LedgerError)):
        seed_decision(ledger)  # acct-1 없음


def test_proposal_requires_existing_decision_fk(seeded: PaperLedger) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        seeded.conn.execute(
            """
            INSERT INTO paper_order_proposals
            (proposal_id, decision_id, account_id, symbol, side, quantity,
             target_exposure_bp, idempotency_key, model_name, model_version, proposed_at)
            VALUES ('prop-x', 'no-such-decision', 'acct-1', '005930', 'BUY', 1,
                    100, 'idem-x', 'kronos-mini', 'v1', ?)
            """,
            (TS_CREATE,),
        )


def test_fill_requires_existing_order_fk(seeded: PaperLedger) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        seeded.conn.execute(
            """
            INSERT INTO paper_fills
            (fill_id, order_id, account_id, symbol, side, fill_price_krw, quantity,
             fee_krw, tax_krw, slippage_krw, observed_at, observation_sha256, filled_at)
            VALUES ('fill-x', 'no-such-order', 'acct-1', '005930', 'BUY', 70000, 1,
                    0, 0, 0, ?, ?, ?)
            """,
            (TS_FILL, SHA_A, TS_FILL),
        )


def test_ksf_lineage_columns_have_no_cross_db_fk(ledger: PaperLedger) -> None:
    fks = ledger.conn.execute("PRAGMA foreign_key_list(paper_agent_decisions)").fetchall()
    fk_from_cols = {fk[3] for fk in fks}
    assert "ksf_run_id" not in fk_from_cols
    assert "ksf_decision_id" not in fk_from_cols


# ---------------------------------------------------------------------------
# CHECK 제약 (raw insert 로 SQL 레벨 검증)
# ---------------------------------------------------------------------------


def raw_insert_account(conn: sqlite3.Connection, **over) -> None:
    row = dict(
        account_id="acct-raw",
        currency="KRW",
        initial_cash_krw=1_000_000,
        policy_version="v1",
        created_at=TS_CREATE,
    )
    row.update(over)
    conn.execute(
        """
        INSERT INTO paper_accounts (account_id, currency, initial_cash_krw, policy_version, created_at)
        VALUES (:account_id, :currency, :initial_cash_krw, :policy_version, :created_at)
        """,
        row,
    )


def test_check_currency_must_be_krw(ledger: PaperLedger) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        raw_insert_account(ledger.conn, currency="USD")


def test_check_initial_cash_positive(ledger: PaperLedger) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        raw_insert_account(ledger.conn, initial_cash_krw=0)


def test_check_timestamp_must_be_timezone_aware(ledger: PaperLedger) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        raw_insert_account(ledger.conn, created_at="2026-08-05T16:05:00")  # naive


def raw_insert_decision(conn: sqlite3.Connection, **over) -> None:
    row = dict(
        decision_id="dec-raw",
        account_id="acct-1",
        symbol="005930",
        ksf_run_id="ksf-run-1",
        ksf_decision_id="ksf-dec-raw",
        feature_snapshot_sha256=SHA_A,
        available_data_cutoff=TS_CUTOFF,
        action="BUY",
        reason_code="FLOW_SIGNAL_POSITIVE",
        rationale_sha256=SHA_B,
        created_at=TS_CREATE,
    )
    row.update(over)
    conn.execute(
        """
        INSERT INTO paper_agent_decisions
        (decision_id, account_id, symbol, ksf_run_id, ksf_decision_id,
         feature_snapshot_sha256, available_data_cutoff, action, reason_code,
         rationale_sha256, created_at)
        VALUES (:decision_id, :account_id, :symbol, :ksf_run_id, :ksf_decision_id,
                :feature_snapshot_sha256, :available_data_cutoff, :action, :reason_code,
                :rationale_sha256, :created_at)
        """,
        row,
    )


def test_check_symbol_allowlist(seeded: PaperLedger) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        raw_insert_decision(seeded.conn, symbol="035420")  # phase 밖 종목


def test_check_agent_action_allowlist(seeded: PaperLedger) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        raw_insert_decision(seeded.conn, action="SHORT")


def test_check_cutoff_not_after_decision_time(seeded: PaperLedger) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        raw_insert_decision(seeded.conn, available_data_cutoff="2026-08-05T17:00:00+09:00")


def test_check_rationale_hash_shape(seeded: PaperLedger) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        raw_insert_decision(seeded.conn, rationale_sha256="not-a-hash")


def test_check_reason_code_allowlisted(seeded: PaperLedger) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        raw_insert_decision(seeded.conn, reason_code="free text rationale goes here")


def test_check_proposal_side_buy_sell_only(seeded: PaperLedger) -> None:
    raw_insert_decision(seeded.conn)  # 신규 dec-raw 로 UNIQUE 충돌 없이 side 만 검증
    with pytest.raises(sqlite3.IntegrityError):
        seeded.conn.execute(
            """
            INSERT INTO paper_order_proposals
            (proposal_id, decision_id, account_id, symbol, side, quantity,
             target_exposure_bp, idempotency_key, model_name, model_version, proposed_at)
            VALUES ('prop-h', 'dec-raw', 'acct-1', '005930', 'HOLD', 1,
                    100, 'idem-h', 'kronos-mini', 'v1', ?)
            """,
            (TS_CREATE,),
        )


def test_check_fill_price_positive_and_quantity_positive(seeded: PaperLedger) -> None:
    base = """
        INSERT INTO paper_fills
        (fill_id, order_id, account_id, symbol, side, fill_price_krw, quantity,
         fee_krw, tax_krw, slippage_krw, observed_at, observation_sha256, filled_at)
        VALUES ('fill-bad', 'ord-1', 'acct-1', '005930', 'BUY', ?, ?, 0, 0, 0, ?, ?, ?)
    """
    with pytest.raises(sqlite3.IntegrityError):
        seeded.conn.execute(base, (0, 1, TS_FILL, SHA_A, TS_FILL))
    with pytest.raises(sqlite3.IntegrityError):
        seeded.conn.execute(base, (70_000, -1, TS_FILL, SHA_A, TS_FILL))


def test_check_no_float_money_storage(seeded: PaperLedger) -> None:
    """금액/수량 컬럼은 INTEGER 선언 + 실수 저장 거부."""
    for table, cols in {
        "paper_accounts": ["initial_cash_krw"],
        "paper_fills": ["fill_price_krw", "quantity", "fee_krw", "tax_krw", "slippage_krw"],
        "paper_cash_events": ["amount_krw"],
        "paper_position_events": ["quantity_delta"],
        "paper_nav_snapshots": ["cash_krw", "position_value_krw", "nav_krw"],
        "paper_orders": ["quantity"],
        "paper_order_proposals": ["quantity"],
    }.items():
        info = {r[1]: r[2].upper() for r in seeded.conn.execute(f"PRAGMA table_info({table})")}
        for col in cols:
            assert info[col] == "INTEGER", f"{table}.{col} must be INTEGER, got {info[col]}"
    with pytest.raises(sqlite3.IntegrityError):
        raw_insert_account(seeded.conn, initial_cash_krw=1_000_000.5)


def test_check_nav_snapshot_consistency(seeded: PaperLedger) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        seeded.conn.execute(
            """
            INSERT INTO paper_nav_snapshots
            (nav_snapshot_id, account_id, session_date, cash_krw, position_value_krw, nav_krw, snapshot_at)
            VALUES ('nav-bad', 'acct-1', '2026-08-07', 100, 100, 999, ?)
            """,
            (TS_FILL,),
        )


# ---------------------------------------------------------------------------
# 이벤트 소싱 불변식
# ---------------------------------------------------------------------------


def test_order_requires_non_reject_review(ledger: PaperLedger) -> None:
    seed_account(ledger)
    seed_decision(ledger, "dec-r", action="SELL")
    seed_proposal(ledger, "prop-r", "dec-r", side="SELL")
    seed_review(
        ledger, "rev-r", "prop-r",
        verdict="REJECT", approved_quantity=None, reject_reason_code="POSITION_LIMIT",
    )
    with pytest.raises((sqlite3.IntegrityError, LedgerError)):
        seed_order(ledger, "ord-r", "rev-r")


def test_fill_requires_accepted_order_event(seeded: PaperLedger) -> None:
    # 차단점 1 이후 ACCEPTED 없는 주문은 구성 자체가 불가(raw insert 도 ACCEPTED 자동 발행).
    # 같은 트리거의 나머지 분기인 "터미널 주문에는 체결 불가"를 검증한다.
    seed_decision(seeded, "dec-2")
    seed_proposal(seeded, "prop-2", "dec-2", idempotency_key="idem-prop-2")
    seed_review(seeded, "rev-2", "prop-2")
    seeded.conn.execute(
        """
        INSERT INTO paper_orders
        (order_id, review_id, proposal_id, account_id, symbol, side, quantity,
         idempotency_key, created_at)
        VALUES ('ord-noevt', 'rev-2', 'prop-2', 'acct-1', '005930', 'BUY', 10,
                'ordkey-noevt', ?)
        """,
        (TS_CREATE,),
    )
    accepted = seeded.conn.execute(
        "SELECT COUNT(*) FROM paper_order_events WHERE order_id='ord-noevt' AND event_type='ACCEPTED'"
    ).fetchone()[0]
    assert accepted == 1  # ACCEPTED 없는 주문은 더 이상 존재할 수 없다
    seeded.append_order_event(
        order_event_id="oe-noevt-cxl", order_id="ord-noevt",
        event_type="CANCELLED", event_at=TS_FILL,
    )
    with pytest.raises(sqlite3.IntegrityError):
        seeded.conn.execute(
            """
            INSERT INTO paper_fills
            (fill_id, order_id, account_id, symbol, side, fill_price_krw, quantity,
             fee_krw, tax_krw, slippage_krw, observed_at, observation_sha256, filled_at)
            VALUES ('fill-noevt', 'ord-noevt', 'acct-1', '005930', 'BUY', 70000, 10,
                    0, 0, 0, ?, ?, ?)
            """,
            (TS_FILL, SHA_A, TS_FILL),
        )


def test_no_order_event_after_terminal_state(seeded: PaperLedger) -> None:
    # seeded 의 ord-1 은 FILLED(terminal) 상태
    with pytest.raises((sqlite3.IntegrityError, LedgerError)):
        seeded.append_order_event(
            order_event_id="oe-late",
            order_id="ord-1",
            event_type="CANCELLED",
            event_at=TS_FILL,
        )


def test_order_state_is_derived_not_stored(seeded: PaperLedger) -> None:
    """주문/계좌/kill-switch 상태는 mutable 컬럼이 아니라 이벤트에서 파생."""
    for table in ("paper_orders", "paper_accounts"):
        cols = {r[1] for r in seeded.conn.execute(f"PRAGMA table_info({table})")}
        for banned in ("status", "state", "enabled", "cash_balance", "position_quantity"):
            assert banned not in cols, f"{table}.{banned} must not exist (event-sourced)"


# ---------------------------------------------------------------------------
# 멱등성 / 중복 재생
# ---------------------------------------------------------------------------


def test_proposal_replay_identical_returns_existing(seeded: PaperLedger) -> None:
    returned = seed_proposal(seeded)  # seeded 와 동일 인자 재호출
    assert returned == "prop-1"
    count = seeded.conn.execute("SELECT COUNT(*) FROM paper_order_proposals").fetchone()[0]
    assert count == 1


def test_proposal_replay_conflicting_rejected(seeded: PaperLedger) -> None:
    with pytest.raises(LedgerConflictError):
        seed_proposal(seeded, quantity=99)  # 같은 proposal_id, 다른 내용
    with pytest.raises((LedgerConflictError, sqlite3.IntegrityError)):
        seed_proposal(seeded, proposal_id="prop-dup", idempotency_key="idem-prop-1")
    count = seeded.conn.execute("SELECT COUNT(*) FROM paper_order_proposals").fetchone()[0]
    assert count == 1


def test_order_replay_identical_returns_existing(seeded: PaperLedger) -> None:
    returned = seed_order(seeded)
    assert returned == "ord-1"
    orders = seeded.conn.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0]
    accepted = seeded.conn.execute(
        "SELECT COUNT(*) FROM paper_order_events WHERE order_id='ord-1' AND event_type='ACCEPTED'"
    ).fetchone()[0]
    assert (orders, accepted) == (1, 1)


def test_fill_replay_identical_no_duplicate_economic_events(seeded: PaperLedger) -> None:
    before_cash = seeded.conn.execute("SELECT COUNT(*) FROM paper_cash_events").fetchone()[0]
    before_pos = seeded.conn.execute("SELECT COUNT(*) FROM paper_position_events").fetchone()[0]
    returned = seed_fill(seeded)  # 동일 인자 재생
    assert returned == "fill-1"
    after_cash = seeded.conn.execute("SELECT COUNT(*) FROM paper_cash_events").fetchone()[0]
    after_pos = seeded.conn.execute("SELECT COUNT(*) FROM paper_position_events").fetchone()[0]
    assert (after_cash, after_pos) == (before_cash, before_pos)
    assert seeded.cash_balance("acct-1") == 10_000_000 - 700_000 - 105


def test_fill_replay_conflicting_rejected(seeded: PaperLedger) -> None:
    with pytest.raises(LedgerConflictError):
        seed_fill(seeded, fill_price_krw=71_000)  # 같은 fill_id, 다른 가격
    with pytest.raises((LedgerConflictError, sqlite3.IntegrityError)):
        seed_fill(seeded, fill_id="fill-other")  # 같은 주문에 두 번째 체결
    fills = seeded.conn.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0]
    assert fills == 1


def test_account_replay_identical_and_conflicting(seeded: PaperLedger) -> None:
    assert seed_account(seeded) == "acct-1"
    deposits = seeded.conn.execute(
        "SELECT COUNT(*) FROM paper_cash_events WHERE event_type='INITIAL_DEPOSIT'"
    ).fetchone()[0]
    assert deposits == 1
    with pytest.raises(LedgerConflictError):
        seeded.create_account(
            account_id="acct-1",
            initial_cash_krw=99,
            policy_version="paper-policy-v1",
            created_at=TS_CREATE,
        )


# ---------------------------------------------------------------------------
# 트랜잭션 원자성
# ---------------------------------------------------------------------------


def test_failed_fill_append_leaves_no_partial_rows(seeded: PaperLedger) -> None:
    """멀티 스테이트먼트 append 중간 실패 시 전체 롤백.

    차단점 2 이후 fill 불일치 현금 이벤트는 위조 자체가 불가하므로,
    canonical FILLED 이벤트 ID 를 다른 주문의 합법 이벤트로 선점해
    fill 행 insert 이후의 자식 발행 단계에서 실패하도록 유도한다."""
    seed_order_chain(seeded, "3")
    seed_order_chain(seeded, "3b")
    seeded.append_order_event(
        order_event_id="ord-3:evt:filled",
        order_id="ord-3b",
        event_type="CANCELLED",
        event_at=TS_FILL,
    )
    with pytest.raises((LedgerConflictError, sqlite3.IntegrityError)):
        seed_fill(seeded, fill_id="fill-3", order_id="ord-3")
    fill_rows = seeded.conn.execute(
        "SELECT COUNT(*) FROM paper_fills WHERE fill_id='fill-3'"
    ).fetchone()[0]
    pos_rows = seeded.conn.execute(
        "SELECT COUNT(*) FROM paper_position_events WHERE fill_id='fill-3'"
    ).fetchone()[0]
    filled_events = seeded.conn.execute(
        "SELECT COUNT(*) FROM paper_order_events WHERE order_id='ord-3' AND event_type='FILLED'"
    ).fetchone()[0]
    assert (fill_rows, pos_rows, filled_events) == (0, 0, 0)
    assert seeded.order_state("ord-3") == "ACCEPTED"


# ---------------------------------------------------------------------------
# 파생 조회 (read-only)
# ---------------------------------------------------------------------------


def test_derived_cash_balance(seeded: PaperLedger) -> None:
    # 초기 입금 10,000,000 - 매수 700,000 - 수수료 105
    assert seeded.cash_balance("acct-1") == 9_299_895


def test_derived_positions(seeded: PaperLedger) -> None:
    assert seeded.positions("acct-1") == {"005930": 10}


def test_derived_latest_nav(seeded: PaperLedger) -> None:
    seeded.append_nav_snapshot(
        nav_snapshot_id="nav-2",
        account_id="acct-1",
        session_date="2026-08-07",
        cash_krw=9_299_895,
        position_value_krw=710_000,
        nav_krw=10_009_895,
        snapshot_at="2026-08-07T16:00:00+09:00",
    )
    latest = seeded.latest_nav("acct-1")
    assert latest is not None
    assert latest["session_date"] == "2026-08-07"
    assert latest["nav_krw"] == 10_009_895


def test_derived_order_state(seeded: PaperLedger) -> None:
    assert seeded.order_state("ord-1") == "FILLED"
    assert seeded.order_state("no-such-order") is None


def test_derived_kill_switch_state(ledger: PaperLedger) -> None:
    seed_account(ledger)
    # 이벤트가 없으면 fail-closed: engaged 로 간주
    assert ledger.kill_switch_engaged("acct-1") is True
    ledger.append_kill_switch_event(
        kill_switch_event_id="ks-a",
        account_id="acct-1",
        event_type="RELEASED",
        reason_code="MANUAL",
        event_at=TS_CREATE,
    )
    assert ledger.kill_switch_engaged("acct-1") is False
    ledger.append_kill_switch_event(
        kill_switch_event_id="ks-b",
        account_id="acct-1",
        event_type="ENGAGED",
        reason_code="DRAWDOWN_STOP",
        event_at=TS_FILL,
    )
    assert ledger.kill_switch_engaged("acct-1") is True


def test_derived_account_enabled_defaults_false(ledger: PaperLedger) -> None:
    seed_account(ledger)
    assert ledger.account_enabled("acct-1") is False  # CREATED 만으로는 비활성
    ledger.append_account_event(
        account_event_id="ae-en",
        account_id="acct-1",
        event_type="ENABLED",
        event_at=TS_FILL,
    )
    assert ledger.account_enabled("acct-1") is True


# ---------------------------------------------------------------------------
# 금지 항목: 브로커/자격증명/원문 프롬프트 필드 및 import 부재
# ---------------------------------------------------------------------------

FORBIDDEN_COLUMN_TOKENS = (
    "prompt", "appkey", "appsecret", "api_key", "secret", "token",
    "password", "credential", "broker", "url",
)

FORBIDDEN_IMPORT_ROOTS = {
    "redis", "requests", "httpx", "aiohttp", "urllib", "socket", "http",
    "pykis", "kis", "python_kis", "telegram", "dotenv", "websocket",
    "common", "inference", "ksf", "strategy", "bot", "dashboard",
}


def test_schema_has_no_forbidden_columns(seeded: PaperLedger) -> None:
    for table in REQUIRED_TABLES:
        cols = [r[1].lower() for r in seeded.conn.execute(f"PRAGMA table_info({table})")]
        for col in cols:
            for tok in FORBIDDEN_COLUMN_TOKENS:
                assert tok not in col, f"{table}.{col} looks like a forbidden field"


def test_ledger_module_has_no_forbidden_imports() -> None:
    tree = ast.parse(LEDGER_MODULE.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    bad = roots & FORBIDDEN_IMPORT_ROOTS
    assert not bad, f"forbidden imports in ledger.py: {sorted(bad)}"


def test_migration_sql_has_no_credential_material() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    for tok in ("appkey", "appsecret", "api_key", "access_token", "password", "authorization"):
        assert tok not in sql


# ---------------------------------------------------------------------------
# 차단점 1 — 부모 INSERT 가 canonical 자식 이벤트를 스키마 trigger 로 원자 발행
# (repository 를 우회한 direct SQL 도 CREATED/INITIAL_DEPOSIT/ACCEPTED/
#  cash·position·FILLED 이벤트 없이 부모 행을 만들 수 없어야 한다)
# ---------------------------------------------------------------------------


def test_raw_account_insert_emits_created_and_initial_deposit(ledger: PaperLedger) -> None:
    raw_insert_account(ledger.conn)  # acct-raw, direct SQL
    events = ledger.conn.execute(
        "SELECT seq, event_type, reason_code, event_at FROM paper_account_events WHERE account_id='acct-raw'"
    ).fetchall()
    assert [tuple(r) for r in events] == [(1, "CREATED", "INITIAL", TS_CREATE)]
    cash = ledger.conn.execute(
        "SELECT event_type, amount_krw, fill_id, event_at FROM paper_cash_events WHERE account_id='acct-raw'"
    ).fetchall()
    assert [tuple(r) for r in cash] == [("INITIAL_DEPOSIT", 1_000_000, None, TS_CREATE)]


def test_raw_order_insert_emits_accepted(seeded: PaperLedger) -> None:
    seed_decision(seeded, "dec-oe")
    seed_proposal(seeded, "prop-oe", "dec-oe")
    seed_review(seeded, "rev-oe", "prop-oe")
    seeded.conn.execute(
        """
        INSERT INTO paper_orders
        (order_id, review_id, proposal_id, account_id, symbol, side, quantity, idempotency_key, created_at)
        VALUES ('ord-oe', 'rev-oe', 'prop-oe', 'acct-1', '005930', 'BUY', 10, 'ordkey-oe', ?)
        """,
        (TS_CREATE,),
    )
    events = seeded.conn.execute(
        "SELECT seq, event_type, event_at FROM paper_order_events WHERE order_id='ord-oe'"
    ).fetchall()
    assert [tuple(r) for r in events] == [(1, "ACCEPTED", TS_CREATE)]


def test_raw_order_insert_must_match_proposal_identity(seeded: PaperLedger) -> None:
    """direct SQL 로 승인된 BUY 주문을 SELL/타계좌/타종목으로 바꿔치기하면 스키마가 거부."""
    seed_account(seeded, "acct-2")
    seed_decision(seeded, "dec-mi")           # acct-1, 005930, BUY
    seed_proposal(seeded, "prop-mi", "dec-mi")
    seed_review(seeded, "rev-mi", "prop-mi")  # APPROVE 10
    for order_id, over in (
        ("ord-mi-side", {"side": "SELL"}),
        ("ord-mi-acct", {"account_id": "acct-2"}),
        ("ord-mi-sym", {"symbol": "000660"}),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            raw_insert_order(seeded.conn, order_id=order_id, **over)
    forged = seeded.conn.execute(
        "SELECT COUNT(*) FROM paper_orders WHERE proposal_id='prop-mi'"
    ).fetchone()[0]
    assert forged == 0
    # 위조 시도 이후에도 proposal 과 일치하는 canonical raw insert 는 그대로 허용
    raw_insert_order(seeded.conn, order_id="ord-mi")
    assert seeded.order_state("ord-mi") == "ACCEPTED"


def test_raw_fill_insert_emits_cash_position_and_filled(seeded: PaperLedger) -> None:
    seed_order_chain(seeded, "rf")
    raw_insert_fill(seeded.conn, fill_id="fill-rf", order_id="ord-rf")
    cash = {
        r[0]: r[1]
        for r in seeded.conn.execute(
            "SELECT event_type, amount_krw FROM paper_cash_events WHERE fill_id='fill-rf'"
        )
    }
    # slippage_krw(350) 는 정보성 필드: fill_price_krw 가 이미 체결가이므로 현금에 재차감 금지
    assert cash == {"FILL_DEBIT": -700_000, "FEE": -105}
    pos = seeded.conn.execute(
        "SELECT account_id, symbol, quantity_delta, event_at FROM paper_position_events WHERE fill_id='fill-rf'"
    ).fetchall()
    assert [tuple(r) for r in pos] == [("acct-1", "005930", 10, TS_FILL)]
    assert seeded.order_state("ord-rf") == "FILLED"


def test_failed_child_emission_rolls_back_raw_fill_insert(seeded: PaperLedger) -> None:
    """자식 이벤트 발행 실패는 direct SQL 부모 insert 까지 전체 롤백."""
    seed_order_chain(seeded, "c1")
    seed_order_chain(seeded, "c2")
    # ord-c1 의 canonical FILLED 이벤트 ID 를 다른 주문의 합법 이벤트로 선점 → 자식 PK 충돌 유도
    seeded.append_order_event(
        order_event_id="ord-c1:evt:filled",
        order_id="ord-c2",
        event_type="CANCELLED",
        event_at=TS_FILL,
    )
    with pytest.raises(sqlite3.IntegrityError):
        raw_insert_fill(seeded.conn, fill_id="fill-c1", order_id="ord-c1")
    counts = [
        seeded.conn.execute(q).fetchone()[0]
        for q in (
            "SELECT COUNT(*) FROM paper_fills WHERE fill_id='fill-c1'",
            "SELECT COUNT(*) FROM paper_cash_events WHERE fill_id='fill-c1'",
            "SELECT COUNT(*) FROM paper_position_events WHERE fill_id='fill-c1'",
        )
    ]
    assert counts == [0, 0, 0]
    assert seeded.order_state("ord-c1") == "ACCEPTED"


# ---------------------------------------------------------------------------
# 차단점 2 — fill 연동 cash/position 이벤트는 참조 fill 과 정확히 일치해야 한다
# ---------------------------------------------------------------------------


def test_forged_fill_linked_cash_event_rejected(seeded: PaperLedger) -> None:
    # fill-1: BUY, tax_krw=0 → TAX 이벤트는 존재 불가, FILL_CREDIT 은 side 불일치
    forged = [
        ("forge:tax", "acct-1", "fill-1", "TAX", -1),
        ("forge:credit", "acct-1", "fill-1", "FILL_CREDIT", 700_000),
        ("forge:debit", "acct-1", "fill-1", "FILL_DEBIT", -1),  # 금액 불일치(+중복)
    ]
    for row in forged:
        with pytest.raises(sqlite3.IntegrityError):
            seeded.conn.execute(
                """
                INSERT INTO paper_cash_events
                (cash_event_id, account_id, fill_id, event_type, amount_krw, event_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (*row, TS_FILL),
            )


def test_forged_fill_linked_position_event_rejected(seeded: PaperLedger) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        seeded.conn.execute(
            """
            INSERT INTO paper_position_events
            (position_event_id, account_id, symbol, fill_id, quantity_delta, event_at)
            VALUES ('forge:pos', 'acct-1', '005930', 'fill-1', 5, ?)
            """,
            (TS_FILL,),
        )


# ---------------------------------------------------------------------------
# 차단점 3 — 리스크 수량 의미론을 스키마에서 강제
# ---------------------------------------------------------------------------


def test_approve_quantity_must_equal_proposal_quantity(seeded: PaperLedger) -> None:
    seed_decision(seeded, "dec-q")
    seed_proposal(seeded, "prop-q", "dec-q")  # quantity=10
    with pytest.raises(sqlite3.IntegrityError):
        seed_review(seeded, "rev-q-bad", "prop-q", approved_quantity=9)
    seed_review(seeded, "rev-q", "prop-q", approved_quantity=10)  # 정상 경로 유지 확인


def test_resize_quantity_positive_and_strictly_smaller(seeded: PaperLedger) -> None:
    seed_decision(seeded, "dec-z")
    seed_proposal(seeded, "prop-z", "dec-z")  # quantity=10
    for bad_qty, rid in ((10, "rev-z-eq"), (11, "rev-z-gt")):
        with pytest.raises(sqlite3.IntegrityError):
            seed_review(seeded, rid, "prop-z", verdict="RESIZE", approved_quantity=bad_qty)
    with pytest.raises(sqlite3.IntegrityError):  # 0 이하는 CHECK(approved_quantity > 0)
        seeded.conn.execute(
            """
            INSERT INTO paper_risk_reviews
            (review_id, proposal_id, verdict, reject_reason_code, approved_quantity, policy_sha256, reviewed_at)
            VALUES ('rev-z-0', 'prop-z', 'RESIZE', NULL, 0, ?, ?)
            """,
            (SHA_A, TS_CREATE),
        )
    seed_review(seeded, "rev-z", "prop-z", verdict="RESIZE", approved_quantity=5)  # 축소는 허용


def test_reject_review_schema_constraints(seeded: PaperLedger) -> None:
    seed_decision(seeded, "dec-j")
    seed_proposal(seeded, "prop-j", "dec-j")
    base = """
        INSERT INTO paper_risk_reviews
        (review_id, proposal_id, verdict, reject_reason_code, approved_quantity, policy_sha256, reviewed_at)
        VALUES (?, 'prop-j', 'REJECT', ?, ?, ?, ?)
    """
    with pytest.raises(sqlite3.IntegrityError):  # 사유 allowlist 밖
        seeded.conn.execute(base, ("rev-j-1", "free text reason", None, SHA_A, TS_CREATE))
    with pytest.raises(sqlite3.IntegrityError):  # REJECT 는 approved_quantity 불가
        seeded.conn.execute(base, ("rev-j-2", "STALE_DATA", 5, SHA_A, TS_CREATE))
    with pytest.raises(sqlite3.IntegrityError):  # REJECT 는 사유 필수
        seeded.conn.execute(base, ("rev-j-3", None, None, SHA_A, TS_CREATE))


# ---------------------------------------------------------------------------
# 차단점 4 — 인과 타임스탬프 단조성 (스키마 강제; KRX 다음 세션 규칙은 broker 과제)
# ---------------------------------------------------------------------------


def test_causal_time_proposal_not_before_decision(seeded: PaperLedger) -> None:
    seed_decision(seeded, "dec-t1")
    with pytest.raises(sqlite3.IntegrityError):
        seed_proposal(seeded, "prop-t1", "dec-t1", proposed_at=TS_EARLY)


def test_causal_time_review_not_before_proposal(seeded: PaperLedger) -> None:
    seed_decision(seeded, "dec-t2")
    seed_proposal(seeded, "prop-t2", "dec-t2")
    with pytest.raises(sqlite3.IntegrityError):
        seed_review(seeded, "rev-t2", "prop-t2", reviewed_at=TS_EARLY)


def test_causal_time_order_not_before_review(seeded: PaperLedger) -> None:
    seed_decision(seeded, "dec-t3")
    seed_proposal(seeded, "prop-t3", "dec-t3")
    seed_review(seeded, "rev-t3", "prop-t3")
    with pytest.raises(sqlite3.IntegrityError):
        seed_order(seeded, "ord-t3", "rev-t3", created_at=TS_EARLY)


def test_causal_time_fill_observed_strictly_after_order_created(seeded: PaperLedger) -> None:
    seed_order_chain(seeded, "t4")  # order created_at = TS_CREATE
    with pytest.raises(sqlite3.IntegrityError):  # 같은 시각도 금지 (strictly after)
        seed_fill(seeded, fill_id="fill-t4", order_id="ord-t4", observed_at=TS_CREATE, filled_at=TS_CREATE)
    with pytest.raises(sqlite3.IntegrityError):
        seed_fill(seeded, fill_id="fill-t4b", order_id="ord-t4", observed_at=TS_EARLY, filled_at=TS_FILL)


def test_causal_time_account_events_monotonic(seeded: PaperLedger) -> None:
    with pytest.raises(sqlite3.IntegrityError):  # 계좌 생성 이전 시각
        seeded.append_account_event(
            account_event_id="ae-early", account_id="acct-1", event_type="ENABLED", event_at=TS_EARLY
        )
    seeded.append_account_event(
        account_event_id="ae-on", account_id="acct-1", event_type="ENABLED", event_at=TS_FILL
    )
    with pytest.raises(sqlite3.IntegrityError):  # 직전 이벤트보다 과거로 후퇴
        seeded.append_account_event(
            account_event_id="ae-back", account_id="acct-1", event_type="DISABLED", event_at=TS_CREATE
        )


def test_causal_time_kill_switch_events_monotonic(seeded: PaperLedger) -> None:
    with pytest.raises(sqlite3.IntegrityError):  # 계좌 생성 이전 + 직전 이벤트(ks-1) 이전
        seeded.append_kill_switch_event(
            kill_switch_event_id="ks-early", account_id="acct-1",
            event_type="RELEASED", reason_code="MANUAL", event_at=TS_EARLY,
        )


def test_causal_time_order_events_monotonic(seeded: PaperLedger) -> None:
    seed_order_chain(seeded, "t5")
    with pytest.raises(sqlite3.IntegrityError):  # 주문 생성 이전 시각
        seeded.append_order_event(
            order_event_id="oe-t5-early", order_id="ord-t5", event_type="CANCELLED", event_at=TS_EARLY
        )


# ---------------------------------------------------------------------------
# 차단점 5 — fill INSERT 시점의 파생 잔고/보유 검증 (리스크 체크와 의도적 중복: 최종 fail-safe)
# ---------------------------------------------------------------------------


def test_buy_fill_requires_sufficient_derived_cash(seeded: PaperLedger) -> None:
    seed_order_chain(seeded, "big", quantity=200)  # 200주 × 70,000 = 14,000,000 > 잔고 9,299,895
    with pytest.raises(sqlite3.IntegrityError):
        seed_fill(seeded, fill_id="fill-big", order_id="ord-big", quantity=200)
    assert seeded.cash_balance("acct-1") == 9_299_895


def test_sell_fill_requires_sufficient_derived_shares(seeded: PaperLedger) -> None:
    seed_order_chain(seeded, "over", action="SELL", quantity=11)  # 보유는 10주뿐
    with pytest.raises(sqlite3.IntegrityError):
        seed_fill(seeded, fill_id="fill-over", order_id="ord-over", quantity=11)
    assert seeded.positions("acct-1") == {"005930": 10}


def test_sell_fill_within_held_shares_succeeds(seeded: PaperLedger) -> None:
    seed_order_chain(seeded, "sok", action="SELL", quantity=10)
    seed_fill(seeded, fill_id="fill-sok", order_id="ord-sok", quantity=10, fee_krw=105, tax_krw=1_260)
    assert seeded.positions("acct-1") == {}
    assert seeded.cash_balance("acct-1") == 9_299_895 + 700_000 - 105 - 1_260


# ---------------------------------------------------------------------------
# 차단점 6 — DB/WAL/SHM(존재 시) group/world 접근 불가
# ---------------------------------------------------------------------------


def test_db_wal_shm_files_never_group_world_readable(db_path: Path) -> None:
    led = PaperLedger(db_path)
    try:
        led.conn.execute("PRAGMA journal_mode=WAL").fetchone()
        seed_account(led)  # WAL/SHM sidecar 를 실제로 생성시키는 쓰기
        wal = Path(f"{db_path}-wal")
        shm = Path(f"{db_path}-shm")
        assert wal.exists(), "WAL sidecar must exist after a write in WAL mode"
        sidecars = [p for p in (db_path, wal, shm, Path(f"{db_path}-journal")) if p.exists()]
        for path in sidecars:
            mode = stat.S_IMODE(path.stat().st_mode)
            assert mode & 0o077 == 0, f"{path.name} mode {oct(mode)} is group/world accessible"
        assert stat.S_IMODE(db_path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
    finally:
        led.close()
