"""Append-only paper trading ledger repository (Task 2).

전용 SQLite 파일 기반 event-sourced 원장. KSF 연구 ledger 와 파일 자체가 분리되며,
KSF lineage 값(ksf_run_id, ksf_decision_id, feature_snapshot_sha256,
available_data_cutoff)은 cross-database FK 없이 불변 외부 참조 값으로만 복사한다.

경계 규칙:
- 실제/모의 증권사 주문 코드, 네트워크, Redis, 환경파일 접근 금지 (표준 라이브러리만 사용).
- 잔고/포지션/주문/kill-switch 상태는 append-only 이벤트에서 파생 조회만 제공.
- 금액은 정수 KRW, 수량은 정수 주식 수만 허용.
"""
from __future__ import annotations

import os
import json
import sqlite3
import threading
from functools import wraps
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Mapping

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
MIGRATION_001 = MIGRATIONS_DIR / "001_autonomous_paper_trading_ledgers.sql"
_MIGRATION_LOCK = threading.Lock()

SUPPORTED_SYMBOLS = frozenset({"005930", "000660"})
AGENT_ACTIONS = frozenset({"ENTER", "HOLD", "REDUCE", "EXIT", "ABSTAIN"})
PROPOSAL_SIDES = frozenset({"BUY", "SELL"})
RISK_VERDICTS = frozenset({"APPROVE", "REJECT", "RESIZE"})
ORDER_TERMINAL_EVENT_TYPES = frozenset({"FILLED", "CANCELLED", "EXPIRED"})

DECISION_REASON_CODES = frozenset({
    "FLOW_SIGNAL_POSITIVE", "FLOW_SIGNAL_NEGATIVE", "NO_EDGE",
    "DATA_STALE", "DATA_MISSING", "DATA_RESTRICTED",
    "MODEL_UNAVAILABLE", "MODEL_OUTPUT_INVALID",
    "KILL_SWITCH_ACTIVE", "RISK_LIMIT_REACHED", "MARKET_CLOSED",
})
RISK_REJECT_REASON_CODES = frozenset({
    "TRADING_DISABLED", "KILL_SWITCH_ACTIVE", "DUPLICATE_SESSION", "STALE_DATA",
    "INVALID_ACTION", "INSUFFICIENT_CASH", "INSUFFICIENT_POSITION",
    "POSITION_LIMIT", "EXPOSURE_LIMIT", "DAILY_FILL_LIMIT",
    "DAILY_LOSS_STOP", "DRAWDOWN_STOP", "MIN_NOTIONAL",
})
KILL_SWITCH_REASON_CODES = frozenset({
    "MANUAL", "STARTUP_DEFAULT", "DAILY_LOSS_STOP", "DRAWDOWN_STOP",
    "DATA_INTEGRITY", "OPERATOR_STOP",
})
ACCOUNT_EVENT_REASON_CODES = frozenset({"INITIAL", "MANUAL", "KILL_SWITCH", "POLICY"})


class LedgerError(ValueError):
    """paper ledger 입력/불변식 위반."""


class LedgerConflictError(LedgerError):
    """동일 식별자/멱등 키 재생 시 payload 가 기존 기록과 다른 경우."""


def _write_transaction(method):
    """Put every mutation read/replay check behind the SQLite write lock."""
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self.transaction_immediate():
            return method(self, *args, **kwargs)
    return locked


def _require_tz_iso(name: str, value: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise LedgerError(f"{name}: ISO-8601 timestamp required, got {value!r}") from exc
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise LedgerError(f"{name}: timezone-aware timestamp required, got {value!r}")
    return str(value)


def _require_int(name: str, value: Any, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise LedgerError(f"{name}: integer required, got {value!r}")
    if minimum is not None and value < minimum:
        raise LedgerError(f"{name}: must be >= {minimum}, got {value}")
    return value


def _require_kst_iso(name: str, value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value) if type(value) is str else None
    except ValueError:
        parsed = None
    if (parsed is None or parsed.tzinfo is None
            or parsed.utcoffset() is None
            or parsed.utcoffset().total_seconds() != 9 * 3600
            or not value.endswith("+09:00")):
        raise LedgerError(f"{name}: timestamp with explicit +09:00 offset required")
    return value


def _require_sha256(name: str, value: str) -> str:
    text = str(value)
    if len(text) != 64 or text != text.lower() or any(c not in "0123456789abcdef" for c in text):
        raise LedgerError(f"{name}: lowercase sha256 hex digest required")
    return text


def _require_choice(name: str, value: str, allowed: frozenset[str]) -> str:
    if value not in allowed:
        raise LedgerError(f"{name}: {value!r} not in allowlist {sorted(allowed)}")
    return value


class PaperLedger:
    """paper_trading.sqlite3 저장소 — 열기/마이그레이션 + append + 파생 조회."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        parent = self.db_path.parent
        parent.mkdir(parents=True, exist_ok=True)
        os.chmod(parent, 0o700)
        self.conn = sqlite3.connect(self.db_path)
        os.chmod(self.db_path, 0o600)
        self.conn.row_factory = sqlite3.Row
        self.conn.isolation_level = None  # 명시적 BEGIN IMMEDIATE 로만 트랜잭션 제어
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        # The canonical migration recreates triggers. Serialize that DDL among
        # same-process workers before execution transactions contend normally.
        with _MIGRATION_LOCK:
            self.conn.executescript(MIGRATION_001.read_text(encoding="utf-8"))
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._savepoint_seq = 0

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "PaperLedger":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 내부 헬퍼
    # ------------------------------------------------------------------

    @contextmanager
    def transaction_immediate(self) -> Iterator[None]:
        """Serialize authoritative reads and writes; nest safely via SAVEPOINT."""
        if self.conn.in_transaction:
            with self._savepoint():
                yield
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    @contextmanager
    def _savepoint(self) -> Iterator[None]:
        self._savepoint_seq += 1
        name = f"paper_ledger_{self._savepoint_seq}"
        self.conn.execute(f"SAVEPOINT {name}")
        try:
            yield
        except BaseException:
            self.conn.execute(f"ROLLBACK TO {name}")
            self.conn.execute(f"RELEASE {name}")
            raise
        self.conn.execute(f"RELEASE {name}")

    @contextmanager
    def _txn(self) -> Iterator[None]:
        with self.transaction_immediate():
            yield

    def _fetch_row(self, table: str, key_col: str, key: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            f"SELECT * FROM {table} WHERE {key_col} = ?", (key,)
        ).fetchone()
        return dict(row) if row is not None else None

    def _is_replay(
        self, table: str, key_col: str, row: dict[str, Any], *, ignore: tuple[str, ...] = ()
    ) -> bool:
        """True → 동일 payload 재생(기존 행 유지). 내용이 다르면 conflict."""
        existing = self._fetch_row(table, key_col, row[key_col])
        if existing is None:
            return False
        left = {k: v for k, v in existing.items() if k not in ignore}
        right = {k: v for k, v in row.items() if k not in ignore}
        if left == right:
            return True
        raise LedgerConflictError(
            f"{table}: {key_col}={row[key_col]!r} already recorded with different content"
        )

    def _insert(self, table: str, row: dict[str, Any]) -> None:
        cols = ", ".join(row)
        marks = ", ".join(f":{c}" for c in row)
        self.conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", row)

    def _next_seq(self, table: str, key_col: str, key: str) -> int:
        return self.conn.execute(
            f"SELECT COALESCE(MAX(seq), 0) + 1 FROM {table} WHERE {key_col} = ?", (key,)
        ).fetchone()[0]

    def _guard_idempotency_key(self, table: str, pk_col: str, pk: str, idempotency_key: str) -> None:
        row = self.conn.execute(
            f"SELECT {pk_col} FROM {table} WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        if row is not None and row[0] != pk:
            raise LedgerConflictError(
                f"{table}: idempotency_key {idempotency_key!r} already bound to {row[0]!r}"
            )

    # ------------------------------------------------------------------
    # append (모두 트랜잭션 안전 + 결정적 중복 처리)
    # ------------------------------------------------------------------

    @_write_transaction
    def create_account(
        self, *, account_id: str, initial_cash_krw: int, policy_version: str, created_at: str
    ) -> str:
        created_at = _require_tz_iso("created_at", created_at)
        _require_int("initial_cash_krw", initial_cash_krw, minimum=1)
        row = {
            "account_id": account_id,
            "currency": "KRW",
            "initial_cash_krw": initial_cash_krw,
            "policy_version": policy_version,
            "created_at": created_at,
        }
        if self._is_replay("paper_accounts", "account_id", row):
            return account_id
        with self._txn():
            # CREATED + INITIAL_DEPOSIT 자식 이벤트는 스키마 AFTER INSERT trigger 가 원자 발행
            self._insert("paper_accounts", row)
        return account_id

    @_write_transaction
    def append_account_event(
        self, *, account_event_id: str, account_id: str, event_type: str,
        event_at: str, reason_code: str | None = None,
    ) -> str:
        _require_choice("event_type", event_type, frozenset({"ENABLED", "DISABLED"}))
        event_at = _require_kst_iso("event_at", event_at)
        if reason_code is not None:
            _require_choice("reason_code", reason_code, ACCOUNT_EVENT_REASON_CODES)
        row = {
            "account_event_id": account_event_id,
            "account_id": account_id,
            "seq": None,  # 트랜잭션 안에서 채번
            "event_type": event_type,
            "reason_code": reason_code,
            "event_at": event_at,
        }
        if self._is_replay("paper_account_events", "account_event_id", row, ignore=("seq",)):
            return account_event_id
        with self._txn():
            row["seq"] = self._next_seq("paper_account_events", "account_id", account_id)
            self._insert("paper_account_events", row)
        return account_event_id

    @_write_transaction
    def append_agent_decision(
        self, *, decision_id: str, account_id: str, symbol: str,
        ksf_run_id: str, ksf_decision_id: str, feature_snapshot_sha256: str,
        available_data_cutoff: str, action: str, reason_code: str,
        rationale_sha256: str, created_at: str,
    ) -> str:
        _require_choice("symbol", symbol, SUPPORTED_SYMBOLS)
        _require_choice("action", action, AGENT_ACTIONS)
        _require_choice("reason_code", reason_code, DECISION_REASON_CODES)
        row = {
            "decision_id": decision_id,
            "account_id": account_id,
            "symbol": symbol,
            "ksf_run_id": ksf_run_id,
            "ksf_decision_id": ksf_decision_id,
            "feature_snapshot_sha256": _require_sha256("feature_snapshot_sha256", feature_snapshot_sha256),
            "available_data_cutoff": _require_tz_iso("available_data_cutoff", available_data_cutoff),
            "action": action,
            "reason_code": reason_code,
            "rationale_sha256": _require_sha256("rationale_sha256", rationale_sha256),
            "created_at": _require_tz_iso("created_at", created_at),
        }
        if datetime.fromisoformat(row["available_data_cutoff"]) > datetime.fromisoformat(row["created_at"]):
            raise LedgerError("available_data_cutoff must not be after decision created_at")
        if self._is_replay("paper_agent_decisions", "decision_id", row):
            return decision_id
        with self._txn():
            self._insert("paper_agent_decisions", row)
        return decision_id

    @_write_transaction
    def append_order_proposal(
        self, *, proposal_id: str, decision_id: str, side: str,
        target_exposure_bp: int, idempotency_key: str,
        model_name: str, model_version: str, proposed_at: str,
    ) -> str:
        _require_choice("side", side, PROPOSAL_SIDES)
        _require_int("target_exposure_bp", target_exposure_bp, minimum=0)
        if target_exposure_bp > 10_000:
            raise LedgerError("target_exposure_bp: must be <= 10000")
        decision = self._fetch_row("paper_agent_decisions", "decision_id", decision_id)
        if decision is None:
            raise LedgerError(f"unknown decision_id {decision_id!r}")
        expected = {"ENTER": ("BUY", True), "REDUCE": ("SELL", True), "EXIT": ("SELL", False)}
        binding = expected.get(decision["action"])
        if binding is None or binding[0] != side or binding[1] != (target_exposure_bp > 0):
            raise LedgerError(
                f"proposal side/target must exactly match decision action {decision['action']!r}"
            )
        row = {
            "proposal_id": proposal_id,
            "decision_id": decision_id,
            "account_id": decision["account_id"],
            "symbol": decision["symbol"],
            "side": side,
            "target_exposure_bp": target_exposure_bp,
            "idempotency_key": idempotency_key,
            "model_name": model_name,
            "model_version": model_version,
            "proposed_at": _require_tz_iso("proposed_at", proposed_at),
        }
        if self._is_replay("paper_order_proposals", "proposal_id", row):
            return proposal_id
        self._guard_idempotency_key("paper_order_proposals", "proposal_id", proposal_id, idempotency_key)
        with self._txn():
            self._insert("paper_order_proposals", row)
        return proposal_id

    @_write_transaction
    def append_risk_review(
        self, *, review_id: str, proposal_id: str, verdict: str, policy_sha256: str,
        reviewed_at: str, approved_quantity: int | None = None,
        reject_reason_code: str | None = None, reference_price_krw: int | None = None,
    ) -> str:
        _require_choice("verdict", verdict, RISK_VERDICTS)
        if verdict == "REJECT":
            if reject_reason_code is None or approved_quantity is not None:
                raise LedgerError("REJECT requires reject_reason_code and no approved_quantity")
            _require_choice("reject_reason_code", reject_reason_code, RISK_REJECT_REASON_CODES)
        else:
            if reject_reason_code is not None:
                raise LedgerError(f"{verdict} must not carry a reject_reason_code")
            _require_int("approved_quantity", approved_quantity, minimum=1)
            _require_int("reference_price_krw", reference_price_krw, minimum=1)
        if verdict == "REJECT" and reference_price_krw is not None:
            raise LedgerError("REJECT must not carry reference_price_krw")
        row = {
            "review_id": review_id,
            "proposal_id": proposal_id,
            "verdict": verdict,
            "reject_reason_code": reject_reason_code,
            "approved_quantity": approved_quantity,
            "reference_price_krw": reference_price_krw,
            "policy_sha256": _require_sha256("policy_sha256", policy_sha256),
            "reviewed_at": _require_tz_iso("reviewed_at", reviewed_at),
        }
        if self._is_replay("paper_risk_reviews", "review_id", row):
            return review_id
        with self._txn():
            self._insert("paper_risk_reviews", row)
        return review_id

    @_write_transaction
    def append_order(
        self, *, order_id: str, review_id: str, idempotency_key: str, created_at: str
    ) -> str:
        created_at = _require_tz_iso("created_at", created_at)
        ctx = self.conn.execute(
            """
            SELECT r.verdict, r.approved_quantity,
                   p.proposal_id, p.account_id, p.symbol, p.side
            FROM paper_risk_reviews r
            JOIN paper_order_proposals p ON p.proposal_id = r.proposal_id
            WHERE r.review_id = ?
            """,
            (review_id,),
        ).fetchone()
        if ctx is None:
            raise LedgerError(f"unknown review_id {review_id!r}")
        if ctx["verdict"] not in ("APPROVE", "RESIZE"):
            raise LedgerError("order requires an APPROVE/RESIZE risk review")
        row = {
            "order_id": order_id,
            "review_id": review_id,
            "proposal_id": ctx["proposal_id"],
            "account_id": ctx["account_id"],
            "symbol": ctx["symbol"],
            "side": ctx["side"],
            "quantity": ctx["approved_quantity"],
            "idempotency_key": idempotency_key,
            "created_at": created_at,
        }
        if self._is_replay("paper_orders", "order_id", row):
            return order_id
        self._guard_idempotency_key("paper_orders", "order_id", order_id, idempotency_key)
        with self._txn():
            # ACCEPTED 자식 이벤트는 스키마 AFTER INSERT trigger 가 원자 발행
            self._insert("paper_orders", row)
        return order_id

    @_write_transaction
    def append_order_event(
        self, *, order_event_id: str, order_id: str, event_type: str, event_at: str
    ) -> str:
        _require_choice("event_type", event_type, frozenset({"CANCELLED", "EXPIRED"}))
        event_at = _require_tz_iso("event_at", event_at)
        row = {
            "order_event_id": order_event_id,
            "order_id": order_id,
            "seq": None,
            "event_type": event_type,
            "event_at": event_at,
        }
        if self._is_replay("paper_order_events", "order_event_id", row, ignore=("seq",)):
            return order_event_id
        with self._txn():
            row["seq"] = self._next_seq("paper_order_events", "order_id", order_id)
            self._insert("paper_order_events", row)
        return order_event_id

    @_write_transaction
    def append_fill(
        self, *, fill_id: str, order_id: str, fill_price_krw: int, quantity: int,
        fee_krw: int, tax_krw: int, slippage_krw: int,
        observed_at: str, observation_sha256: str, filled_at: str,
        raw_reference_price_krw: int | None = None, fee_bps: int = 0,
        tax_bps: int = 0, slippage_bps: int = 0,
        execution_policy_sha256: str | None = None,
    ) -> str:
        if raw_reference_price_krw is None:
            raw_reference_price_krw = fill_price_krw
        if execution_policy_sha256 is None:
            execution_policy_sha256 = observation_sha256
        _require_int("raw_reference_price_krw", raw_reference_price_krw, minimum=1)
        _require_int("fill_price_krw", fill_price_krw, minimum=1)
        _require_int("quantity", quantity, minimum=1)
        _require_int("fee_krw", fee_krw, minimum=0)
        _require_int("tax_krw", tax_krw, minimum=0)
        _require_int("slippage_krw", slippage_krw, minimum=0)
        _require_int("fee_bps", fee_bps, minimum=0)
        _require_int("tax_bps", tax_bps, minimum=0)
        _require_int("slippage_bps", slippage_bps, minimum=0)
        order = self._fetch_row("paper_orders", "order_id", order_id)
        if order is None:
            raise LedgerError(f"unknown order_id {order_id!r}")
        if quantity != order["quantity"]:
            raise LedgerError("partial fills are not supported in this phase")
        row = {
            "fill_id": fill_id,
            "order_id": order_id,
            "account_id": order["account_id"],
            "symbol": order["symbol"],
            "side": order["side"],
            "raw_reference_price_krw": raw_reference_price_krw,
            "fill_price_krw": fill_price_krw,
            "quantity": quantity,
            "fee_krw": fee_krw,
            "tax_krw": tax_krw,
            "slippage_krw": slippage_krw,
            "fee_bps": fee_bps,
            "tax_bps": tax_bps,
            "slippage_bps": slippage_bps,
            "execution_policy_sha256": _require_sha256(
                "execution_policy_sha256", execution_policy_sha256),
            "observed_at": _require_tz_iso("observed_at", observed_at),
            "observation_sha256": _require_sha256("observation_sha256", observation_sha256),
            "filled_at": _require_tz_iso("filled_at", filled_at),
        }
        if self._is_replay("paper_fills", "fill_id", row):
            return fill_id
        existing_fill = self.conn.execute(
            "SELECT fill_id FROM paper_fills WHERE order_id = ?", (order_id,)
        ).fetchone()
        if existing_fill is not None:
            raise LedgerConflictError(
                f"order {order_id!r} already filled by {existing_fill[0]!r}"
            )
        with self._txn():
            # cash/position/FILLED 자식 이벤트는 스키마 AFTER INSERT trigger 가 원자 발행
            # (slippage_krw 는 정보성: fill_price_krw 가 체결가이므로 현금 재차감 없음)
            self._insert("paper_fills", row)
        return fill_id

    @_write_transaction
    def append_nav_snapshot(
        self, *, nav_snapshot_id: str, account_id: str, session_date: str,
        cash_krw: int, position_value_krw: int, nav_krw: int,
        position_marks: Mapping[str, Mapping[str, Any]], snapshot_at: str,
    ) -> str:
        _require_int("cash_krw", cash_krw, minimum=0)
        _require_int("position_value_krw", position_value_krw, minimum=0)
        _require_int("nav_krw", nav_krw)
        if nav_krw != cash_krw + position_value_krw:
            raise LedgerError("nav_krw must equal cash_krw + position_value_krw")
        if type(position_marks) is not dict:
            raise LedgerError("position marks must be an exact mapping")
        normalized: dict[str, dict[str, Any]] = {}
        for symbol, mark in position_marks.items():
            if type(symbol) is not str or symbol not in SUPPORTED_SYMBOLS or type(mark) is not dict \
                    or set(mark) != {"quantity", "price_krw", "feature_snapshot_sha256", "observed_at"}:
                raise LedgerError("position marks have invalid schema")
            quantity = _require_int("position mark quantity", mark["quantity"], minimum=1)
            price = _require_int("position mark price_krw", mark["price_krw"], minimum=1)
            observed_at = _require_tz_iso("position mark observed_at", mark["observed_at"])
            if datetime.fromisoformat(observed_at).utcoffset().total_seconds() != 9 * 3600:
                raise LedgerError("position mark observed_at must use explicit +09:00")
            normalized[symbol] = {"quantity": quantity, "price_krw": price,
                "feature_snapshot_sha256": _require_sha256(
                    "position mark feature_snapshot_sha256", mark["feature_snapshot_sha256"]),
                "observed_at": observed_at}
        snapshot_at = _require_tz_iso("snapshot_at", snapshot_at)
        snapshot_time = datetime.fromisoformat(snapshot_at)
        if snapshot_time.utcoffset().total_seconds() != 9 * 3600 or snapshot_time.date().isoformat() != session_date:
            raise LedgerError("snapshot_at must use +09:00 and its date must equal session_date")
        if any(datetime.fromisoformat(mark["observed_at"]) > snapshot_time for mark in normalized.values()):
            raise LedgerError("position mark observed_at must not be after snapshot_at")
        row = {
            "nav_snapshot_id": nav_snapshot_id,
            "account_id": account_id,
            "session_date": session_date,
            "cash_krw": cash_krw,
            "position_value_krw": position_value_krw,
            "position_marks_json": json.dumps(normalized, sort_keys=True, separators=(",", ":")),
            "nav_krw": nav_krw,
            "snapshot_at": snapshot_at,
        }
        with self.transaction_immediate():
            positions = self.positions_as_of(account_id, snapshot_at)
            if set(normalized) != set(positions) or any(
                normalized[s]["quantity"] != positions[s] for s in positions
            ):
                raise LedgerError("position marks must exactly match event-sourced positions")
            marked_value = sum(m["quantity"] * m["price_krw"] for m in normalized.values())
            if marked_value != position_value_krw:
                raise LedgerError("position marks must exactly sum to position_value_krw")
            if self.cash_balance_as_of(account_id, snapshot_at) != cash_krw:
                raise LedgerError("cash_krw must exactly match event-sourced cash")
            if self._is_replay("paper_nav_snapshots", "nav_snapshot_id", row):
                return nav_snapshot_id
            self._insert("paper_nav_snapshots", row)
        return nav_snapshot_id

    @_write_transaction
    def append_kill_switch_event(
        self, *, kill_switch_event_id: str, account_id: str, event_type: str,
        reason_code: str, event_at: str,
    ) -> str:
        _require_choice("event_type", event_type, frozenset({"ENGAGED", "RELEASED"}))
        _require_choice("reason_code", reason_code, KILL_SWITCH_REASON_CODES)
        row = {
            "kill_switch_event_id": kill_switch_event_id,
            "account_id": account_id,
            "seq": None,
            "event_type": event_type,
            "reason_code": reason_code,
            "event_at": _require_kst_iso("event_at", event_at),
        }
        if self._is_replay("paper_kill_switch_events", "kill_switch_event_id", row, ignore=("seq",)):
            return kill_switch_event_id
        with self._txn():
            row["seq"] = self._next_seq("paper_kill_switch_events", "account_id", account_id)
            self._insert("paper_kill_switch_events", row)
        return kill_switch_event_id

    # ------------------------------------------------------------------
    # 파생 조회 (read-only)
    # ------------------------------------------------------------------

    def cash_balance(self, account_id: str) -> int:
        return self.conn.execute(
            "SELECT COALESCE(SUM(amount_krw), 0) FROM paper_cash_events WHERE account_id = ?",
            (account_id,),
        ).fetchone()[0]

    def cash_balance_as_of(self, account_id: str, as_of: str) -> int:
        cutoff = datetime.fromisoformat(_require_tz_iso("as_of", as_of))
        total = 0
        for row in self.conn.execute(
            "SELECT amount_krw,event_at FROM paper_cash_events WHERE account_id=?", (account_id,)):
            try:
                event_time = datetime.fromisoformat(row["event_at"])
            except (TypeError, ValueError) as exc:
                raise LedgerError("cash event has malformed timestamp") from exc
            if event_time.tzinfo is None:
                raise LedgerError("cash event has malformed timestamp")
            if event_time <= cutoff:
                total += _require_int("cash event amount_krw", row["amount_krw"])
        return total

    def positions(self, account_id: str) -> dict[str, int]:
        rows = self.conn.execute(
            """
            SELECT symbol, SUM(quantity_delta) AS qty
            FROM paper_position_events WHERE account_id = ?
            GROUP BY symbol HAVING SUM(quantity_delta) != 0
            """,
            (account_id,),
        ).fetchall()
        return {row["symbol"]: row["qty"] for row in rows}

    def positions_as_of(self, account_id: str, as_of: str) -> dict[str, int]:
        cutoff = datetime.fromisoformat(_require_tz_iso("as_of", as_of))
        totals: dict[str, int] = {}
        for row in self.conn.execute(
            "SELECT symbol,quantity_delta,event_at FROM paper_position_events WHERE account_id=?",
            (account_id,)):
            try:
                event_time = datetime.fromisoformat(row["event_at"])
            except (TypeError, ValueError) as exc:
                raise LedgerError("position event has malformed timestamp") from exc
            if event_time.tzinfo is None:
                raise LedgerError("position event has malformed timestamp")
            if event_time <= cutoff:
                symbol = _require_choice("position event symbol", row["symbol"], SUPPORTED_SYMBOLS)
                totals[symbol] = totals.get(symbol, 0) + _require_int(
                    "position event quantity_delta", row["quantity_delta"])
        if any(quantity < 0 for quantity in totals.values()):
            raise LedgerError("event-sourced position is negative")
        return {symbol: quantity for symbol, quantity in totals.items() if quantity}

    def latest_nav(self, account_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT * FROM paper_nav_snapshots WHERE account_id = ?
            ORDER BY session_date DESC, snapshot_at DESC, nav_snapshot_id DESC LIMIT 1
            """,
            (account_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def order_state(self, order_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT event_type FROM paper_order_events WHERE order_id = ? ORDER BY seq DESC LIMIT 1",
            (order_id,),
        ).fetchone()
        return row[0] if row is not None else None

    def kill_switch_engaged(self, account_id: str) -> bool:
        row = self.conn.execute(
            "SELECT event_type FROM paper_kill_switch_events WHERE account_id = ? ORDER BY seq DESC LIMIT 1",
            (account_id,),
        ).fetchone()
        if row is None:
            return True  # fail-closed: 기록이 없으면 kill switch 활성으로 간주
        return row[0] == "ENGAGED"

    def account_enabled(self, account_id: str) -> bool:
        row = self.conn.execute(
            "SELECT event_type FROM paper_account_events WHERE account_id = ? ORDER BY seq DESC LIMIT 1",
            (account_id,),
        ).fetchone()
        return row is not None and row[0] == "ENABLED"
