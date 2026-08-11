"""Pure deterministic sizing and risk checks for paper trading.

All monetary values are integer KRW, quantities are integer shares, and rates
are integer basis points.  The caller supplies time and aggregate state.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime, timedelta
from typing import Any, Mapping

from ksf.feature_engine import SymbolFeatureOutput
from ksf.global_collector import stable_json
from strategy.paper_agent import _normalize_feature_output, _pre_model_abstain_reason

from paper_trading.ledger import (
    LedgerConflictError, LedgerError, PaperLedger, RISK_REJECT_REASON_CODES, SUPPORTED_SYMBOLS,
)

KST = timedelta(hours=9)
ACTIONS = frozenset({"ENTER", "HOLD", "REDUCE", "EXIT", "ABSTAIN"})


def _plain_int(name: str, value: Any, minimum: int = 0) -> None:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be a plain integer >= {minimum}")


def _text(name: str, value: Any) -> None:
    if type(value) is not str or not value or len(value) > 256:
        raise ValueError(f"{name} must be a non-empty plain string")


def _kst_time(name: str, value: Any) -> datetime:
    _text(name, value)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != KST:
        raise ValueError(f"{name} must use an explicit +09:00 offset")
    return parsed


def _sha256(name: str, value: Any) -> None:
    if type(value) is not str or len(value) != 64 or value != value.lower() or any(c not in "0123456789abcdef" for c in value):
        raise ValueError(f"{name} must be a lowercase sha256 hex digest")


@dataclass(frozen=True, slots=True)
class RiskPolicy:
    max_position_bps: int
    max_aggregate_bps: int
    daily_order_cap: int
    daily_fill_cap: int
    daily_loss_stop_krw: int
    drawdown_stop_bps: int
    min_notional_krw: int
    max_source_age_seconds: int

    def __post_init__(self) -> None:
        for field in fields(self):
            _plain_int(field.name, getattr(self, field.name))
        if not 1 <= self.max_position_bps <= 10_000:
            raise ValueError("max_position_bps must be in [1, 10000]")
        if not 1 <= self.max_aggregate_bps <= 10_000:
            raise ValueError("max_aggregate_bps must be in [1, 10000]")
        if self.max_position_bps > self.max_aggregate_bps:
            raise ValueError("position cap cannot exceed aggregate cap")
        if self.daily_order_cap < 1 or self.daily_fill_cap < 1 or self.daily_loss_stop_krw < 1:
            raise ValueError("daily caps must be positive")
        if not 1 <= self.drawdown_stop_bps <= 10_000:
            raise ValueError("drawdown_stop_bps must be in [1, 10000]")
        if self.min_notional_krw < 1 or self.max_source_age_seconds < 0:
            raise ValueError("notional must be positive and source age non-negative")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RiskPolicy":
        if type(value) is not dict or set(value) != {f.name for f in fields(cls)}:
            raise ValueError("policy must be a plain closed-schema dictionary")
        return cls(**value)

    @property
    def sha256(self) -> str:
        canonical = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class RiskInput:
    account_id: str
    ksf_run_id: str
    ksf_decision_id: str
    feature_snapshot_sha256: str
    session_id: str
    symbol: str
    action: str
    target_exposure_bps: int
    price_krw: int
    cash_krw: int
    holdings_quantity: int
    aggregate_position_value_krw: int
    nav_krw: int
    high_water_nav_krw: int
    session_start_nav_krw: int
    daily_order_count: int
    daily_fill_count: int
    trading_enabled: bool
    kill_switch_engaged: bool
    duplicate_state_snapshot: bool
    decision_at: str
    available_data_cutoff: str
    feature_snapshot: SymbolFeatureOutput

    def __post_init__(self) -> None:
        for name in ("account_id", "ksf_run_id", "ksf_decision_id", "session_id"):
            _text(name, getattr(self, name))
        _sha256("feature_snapshot_sha256", self.feature_snapshot_sha256)
        if type(self.symbol) is not str or self.symbol not in SUPPORTED_SYMBOLS:
            raise ValueError("unknown symbol")
        if type(self.action) is not str or self.action not in ACTIONS:
            raise ValueError("unknown action")
        for name in (
            "target_exposure_bps", "price_krw", "cash_krw", "holdings_quantity",
            "aggregate_position_value_krw", "nav_krw", "high_water_nav_krw",
            "session_start_nav_krw", "daily_order_count", "daily_fill_count",
        ):
            _plain_int(name, getattr(self, name))
        if self.price_krw < 1 or self.nav_krw < 1 or self.high_water_nav_krw < 1 or self.session_start_nav_krw < 1:
            raise ValueError("price and NAV values must be positive")
        if not 0 <= self.target_exposure_bps <= 10_000:
            raise ValueError("target_exposure_bps must be in [0, 10000]")
        for name in ("trading_enabled", "kill_switch_engaged", "duplicate_state_snapshot"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be a plain boolean")
        _kst_time("decision_at", self.decision_at)
        _kst_time("available_data_cutoff", self.available_data_cutoff)
        canonical = _normalize_feature_output(self.feature_snapshot)
        if canonical is None:
            raise ValueError("feature_snapshot must be an exact canonical SymbolFeatureOutput")
        object.__setattr__(self, "feature_snapshot", canonical)
        if canonical.symbol != self.symbol:
            raise ValueError("feature snapshot symbol must equal request symbol")
        if canonical.available_data_cutoff != self.available_data_cutoff:
            raise ValueError("feature snapshot available_data_cutoff must equal request cutoff")
        digest = hashlib.sha256(canonical.stable_json().encode("utf-8")).hexdigest()
        if digest != self.feature_snapshot_sha256:
            raise ValueError("feature_snapshot_sha256 mismatch")
        if self.nav_krw != self.cash_krw + self.aggregate_position_value_krw:
            raise ValueError("nav must equal cash plus aggregate position value")
        own_value = self.holdings_quantity * self.price_krw
        if own_value > self.aggregate_position_value_krw:
            raise ValueError("symbol position cannot exceed aggregate position value")
        if self.high_water_nav_krw < self.nav_krw:
            raise ValueError("high-water NAV cannot be below current NAV")
    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RiskInput":
        if type(value) is not dict or set(value) != {f.name for f in fields(cls)}:
            raise ValueError("risk input must be a plain closed-schema dictionary")
        if type(value["feature_snapshot"]) is not SymbolFeatureOutput:
            raise ValueError("feature_snapshot must be an exact SymbolFeatureOutput")
        return cls(**value)


@dataclass(frozen=True, slots=True)
class RiskResult:
    verdict: str
    approved_quantity: int | None
    reject_reason_code: str | None
    policy_sha256: str
    order_required: bool

    def __post_init__(self) -> None:
        if type(self.verdict) is not str or self.verdict not in {"APPROVE", "RESIZE", "REJECT", "NO_OP"}:
            raise ValueError("invalid risk verdict")
        if type(self.order_required) is not bool:
            raise ValueError("order_required must be a plain boolean")
        _sha256("policy_sha256", self.policy_sha256)
        if self.verdict in {"APPROVE", "RESIZE"}:
            _plain_int("approved_quantity", self.approved_quantity, 1)
            if self.reject_reason_code is not None or not self.order_required:
                raise ValueError("approval shape is invalid")
        elif self.verdict == "REJECT":
            if (self.approved_quantity is not None or type(self.reject_reason_code) is not str
                    or self.reject_reason_code not in RISK_REJECT_REASON_CODES or not self.order_required):
                raise ValueError("reject shape is invalid")
        elif self.approved_quantity is not None or self.reject_reason_code is not None or self.order_required:
            raise ValueError("no-op shape is invalid")


def _result(policy: RiskPolicy, verdict: str, quantity: int | None = None,
            reason: str | None = None, *, order: bool = True) -> RiskResult:
    if reason is not None and reason not in RISK_REJECT_REASON_CODES:
        raise AssertionError("risk reason must be ledger-allowlisted")
    return RiskResult(verdict, quantity, reason, policy.sha256, order)


def evaluate_risk(request: RiskInput, policy: RiskPolicy) -> RiskResult:
    """Evaluate in the plan's exact precedence order; never read external state."""
    if type(request) is not RiskInput or type(policy) is not RiskPolicy:
        raise ValueError("exact validated RiskInput and RiskPolicy instances required")
    if not request.trading_enabled:
        return _result(policy, "REJECT", reason="TRADING_DISABLED")
    if request.kill_switch_engaged:
        return _result(policy, "REJECT", reason="KILL_SWITCH_ACTIVE")
    if request.duplicate_state_snapshot:
        return _result(policy, "REJECT", reason="DUPLICATE_SESSION")

    decision = _kst_time("decision_at", request.decision_at)
    cutoff = _kst_time("available_data_cutoff", request.available_data_cutoff)
    max_age = timedelta(seconds=policy.max_source_age_seconds)
    snapshot = request.feature_snapshot
    if cutoff > decision or decision - cutoff > max_age:
        return _result(policy, "REJECT", reason="STALE_DATA")
    if _pre_model_abstain_reason(snapshot, request.available_data_cutoff) is not None:
        return _result(policy, "REJECT", reason="STALE_DATA")
    observations = tuple(
        _kst_time("feature source_as_of", feature.source_as_of)
        for group in snapshot.groups.values() for feature in group.features
    )
    if not observations:
        return _result(policy, "REJECT", reason="STALE_DATA")
    if any(obs > cutoff or cutoff - obs > max_age for obs in observations):
        return _result(policy, "REJECT", reason="STALE_DATA")

    current = request.holdings_quantity
    if (request.action in {"ENTER", "REDUCE"} and request.target_exposure_bps == 0) or (
        request.action == "EXIT" and request.target_exposure_bps != 0
    ):
        return _result(policy, "REJECT", reason="INVALID_ACTION")
    if request.action in {"HOLD", "ABSTAIN"}:
        return _result(policy, "NO_OP", order=False)
    if request.action == "EXIT":
        if current == 0:
            return _result(policy, "NO_OP", order=False)
        desired, side = 0, "SELL"
    else:
        desired = (request.nav_krw * request.target_exposure_bps // 10_000) // request.price_krw
        side = "BUY" if request.action == "ENTER" else "SELL"
        if request.action == "ENTER" and desired <= current:
            return _result(policy, "REJECT", reason="INVALID_ACTION")
        if request.action == "REDUCE" and (current == 0 or desired >= current):
            return _result(policy, "REJECT", reason="INSUFFICIENT_POSITION" if current == 0 else "INVALID_ACTION")

    quantity = desired - current if side == "BUY" else current - desired
    resized = False
    if side == "BUY" and quantity * request.price_krw > request.cash_krw:
        quantity = request.cash_krw // request.price_krw
        if quantity < 1:
            return _result(policy, "REJECT", reason="INSUFFICIENT_CASH")
        desired = current + quantity
        resized = True
    if side == "SELL" and quantity > current:
        return _result(policy, "REJECT", reason="INSUFFICIENT_POSITION")

    current_value = current * request.price_krw
    if current_value * 10_000 > request.nav_krw * policy.max_position_bps and side == "BUY":
        return _result(policy, "REJECT", reason="POSITION_LIMIT")
    position_cap_qty = (request.nav_krw * policy.max_position_bps // 10_000) // request.price_krw
    if desired > position_cap_qty:
        desired = position_cap_qty
        quantity = desired - current
        if quantity < 1:
            return _result(policy, "REJECT", reason="POSITION_LIMIT")
        resized = True
    other_value = request.aggregate_position_value_krw - current_value
    if other_value * 10_000 > request.nav_krw * policy.max_aggregate_bps and side == "BUY":
        return _result(policy, "REJECT", reason="EXPOSURE_LIMIT")
    aggregate_room = request.nav_krw * policy.max_aggregate_bps // 10_000 - other_value
    aggregate_cap_qty = max(0, aggregate_room // request.price_krw)
    if desired > aggregate_cap_qty:
        desired = aggregate_cap_qty
        quantity = desired - current
        if quantity < 1:
            return _result(policy, "REJECT", reason="EXPOSURE_LIMIT")
        resized = True

    if request.daily_order_count >= policy.daily_order_cap or request.daily_fill_count >= policy.daily_fill_cap:
        return _result(policy, "REJECT", reason="DAILY_FILL_LIMIT")
    if request.session_start_nav_krw - request.nav_krw >= policy.daily_loss_stop_krw:
        return _result(policy, "REJECT", reason="DAILY_LOSS_STOP")
    drawdown = request.high_water_nav_krw - request.nav_krw
    if drawdown * 10_000 >= request.high_water_nav_krw * policy.drawdown_stop_bps:
        return _result(policy, "REJECT", reason="DRAWDOWN_STOP")
    if quantity < 1 or quantity * request.price_krw < policy.min_notional_krw:
        return _result(policy, "REJECT", reason="MIN_NOTIONAL")
    return _result(policy, "RESIZE" if resized else "APPROVE", quantity)


def _exact_row(name: str, value: Any, expected_type: type, allowed: set[Any] | None = None) -> Any:
    if type(value) is not expected_type or (allowed is not None and value not in allowed):
        raise LedgerConflictError(f"authoritative {name} is malformed")
    return value


def _review_identity(*, proposal_id: str, reviewed_at: str, request: RiskInput,
                     policy: RiskPolicy, authoritative: Mapping[str, Any]) -> str:
    request_payload = {
        field.name: (request.feature_snapshot.stable_json() if field.name == "feature_snapshot"
                     else getattr(request, field.name))
        for field in fields(request)
    }
    payload = {"proposal_id": proposal_id, "reviewed_at": reviewed_at,
               "request": request_payload, "policy": asdict(policy),
               "authoritative": dict(authoritative)}
    return "risk-" + hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()


def review_and_persist(ledger: Any, *, proposal_id: str, reviewed_at: str,
                       request: RiskInput, policy: RiskPolicy,
                       valuation_snapshots: Mapping[str, SymbolFeatureOutput] | None = None) -> str | None:
    """Atomically review; current marks bind to payload cutoff as observation time."""
    if type(ledger) is not PaperLedger:
        raise ValueError("exact PaperLedger required")
    _text("proposal_id", proposal_id)
    reviewed_time = _kst_time("reviewed_at", reviewed_at)
    if type(request) is not RiskInput or type(policy) is not RiskPolicy:
        raise ValueError("exact validated RiskInput and RiskPolicy required")
    if valuation_snapshots is None:
        valuation_snapshots = {}
    if type(valuation_snapshots) is not dict:
        raise ValueError("valuation_snapshots must be an exact mapping")
    # Trust binding and the SQLite write reservation precede every semantic read.
    with ledger.transaction_immediate():
        row = ledger.conn.execute(
            """SELECT p.account_id, p.symbol, p.side, p.target_exposure_bp,
                      p.proposed_at, d.ksf_run_id, d.ksf_decision_id,
                      d.feature_snapshot_sha256, d.available_data_cutoff,
                      d.action AS decision_action, d.created_at AS decision_created_at
               FROM paper_order_proposals p JOIN paper_agent_decisions d
                 ON d.decision_id = p.decision_id WHERE p.proposal_id = ?""",
            (proposal_id,),
        ).fetchone()
        if row is None:
            raise ValueError("unknown proposal_id")
        expected_side = "BUY" if request.action == "ENTER" else "SELL" if request.action in {"REDUCE", "EXIT"} else None
        if expected_side is None:
            raise LedgerConflictError("actionable proposal cannot bind a no-op action")
        exact = {
            "account_id": _exact_row("account_id", row["account_id"], str),
            "symbol": _exact_row("symbol", row["symbol"], str, set(SUPPORTED_SYMBOLS)),
            "side": _exact_row("side", row["side"], str, {"BUY", "SELL"}),
            "target_exposure_bp": _exact_row("target_exposure_bp", row["target_exposure_bp"], int),
            "ksf_run_id": _exact_row("ksf_run_id", row["ksf_run_id"], str),
            "ksf_decision_id": _exact_row("ksf_decision_id", row["ksf_decision_id"], str),
            "feature_snapshot_sha256": _exact_row("feature_snapshot_sha256", row["feature_snapshot_sha256"], str),
            "available_data_cutoff": _exact_row("available_data_cutoff", row["available_data_cutoff"], str),
            "decision_action": _exact_row("decision_action", row["decision_action"], str, set(ACTIONS)),
        }
        if not 0 <= exact["target_exposure_bp"] <= 10_000:
            raise LedgerConflictError("authoritative proposal exposure is malformed")
        if exact["side"] != expected_side or exact["decision_action"] != request.action:
            raise LedgerConflictError("proposal side / decision action mismatch")
        if ((request.action in {"ENTER", "REDUCE"}) != (exact["target_exposure_bp"] > 0)):
            raise LedgerConflictError("proposal target does not match exact decision action")
        checks = {"account_id": request.account_id, "symbol": request.symbol,
                  "target_exposure_bp": request.target_exposure_bps,
                  "ksf_run_id": request.ksf_run_id, "ksf_decision_id": request.ksf_decision_id,
                  "feature_snapshot_sha256": request.feature_snapshot_sha256,
                  "available_data_cutoff": request.available_data_cutoff}
        for name, expected in checks.items():
            if exact[name] != expected:
                raise ValueError(f"proposal {name} does not match risk request")
        decision_time = _kst_time("decision_created_at", _exact_row("decision_created_at", row["decision_created_at"], str))
        proposed_time = _kst_time("proposed_at", _exact_row("proposed_at", row["proposed_at"], str))
        if request.decision_at != row["decision_created_at"]:
            raise ValueError("proposal decision_created_at does not match risk request")
        if request.session_id != decision_time.date().isoformat():
            raise ValueError("session_id must equal the KST decision date")
        if reviewed_time < decision_time or reviewed_time < proposed_time:
            raise ValueError("reviewed_at must not precede decision or proposal")
        def authoritative_state_as_of(table: str, *, allowed: set[str]) -> str | None:
            rows = ledger.conn.execute(
                f"SELECT seq,event_type,event_at FROM {table} WHERE account_id=? ORDER BY seq",
                (request.account_id,),
            ).fetchall()
            selected = None
            previous_time = None
            for expected_seq, event in enumerate(rows, 1):
                seq = _exact_row(f"{table} seq", event["seq"], int)
                event_type = _exact_row(f"{table} event_type", event["event_type"], str, allowed)
                try:
                    event_time = _kst_time(f"{table} event_at", event["event_at"])
                except ValueError as exc:
                    raise LedgerConflictError(f"{table} authoritative row is malformed") from exc
                if seq != expected_seq or (previous_time is not None and event_time < previous_time):
                    raise LedgerConflictError(f"{table} authoritative chronology is malformed")
                previous_time = event_time
                if event_time <= reviewed_time:
                    selected = event_type
            return selected

        account_type = authoritative_state_as_of(
            "paper_account_events", allowed={"CREATED", "ENABLED", "DISABLED"})
        not_queried = "NOT_QUERIED"
        if account_type != "ENABLED":
            stage = "ACCOUNT_DISABLED"
            kill_type = not_queried
            duplicate = not_queried
        else:
            kill_type = authoritative_state_as_of(
                "paper_kill_switch_events", allowed={"ENGAGED", "RELEASED"})
            if kill_type != "RELEASED":
                stage = "KILL_ENGAGED"
                duplicate = not_queried
            else:
                stage = "DUPLICATE_CHECKED"
                duplicate = None
        prior = ledger.conn.execute("SELECT review_id FROM paper_risk_reviews WHERE proposal_id=?", (proposal_id,)).fetchone()
        if duplicate is None:
            accepted = ledger.conn.execute(
                """SELECT r.verdict, d.created_at FROM paper_risk_reviews r
                   JOIN paper_order_proposals p ON p.proposal_id=r.proposal_id
                   JOIN paper_agent_decisions d ON d.decision_id=p.decision_id
                   WHERE p.account_id=? AND p.symbol=? AND p.proposal_id<>?
                     AND r.verdict IN ('APPROVE','RESIZE')""",
                (request.account_id, request.symbol, proposal_id)).fetchall()
            duplicate = False
            for accepted_row in accepted:
                accepted_at = _kst_time("accepted decision created_at", _exact_row(
                    "accepted decision created_at", accepted_row["created_at"], str))
                if accepted_at.date().isoformat() == request.session_id:
                    duplicate = True
                    break

        state = {"stage": stage, "account_event": account_type, "kill_event": kill_type,
                 "duplicate": duplicate, "proposal": exact, "proposed_at": row["proposed_at"],
                 "decision_created_at": row["decision_created_at"]}
        # Earlier definitive rejects intentionally do not touch later mutable state.
        if account_type == "ENABLED" and kill_type == "RELEASED" and not duplicate:
            cash = ledger.conn.execute(
                "SELECT COALESCE(SUM(amount_krw),0) FROM paper_cash_events WHERE account_id=?",
                (request.account_id,)).fetchone()[0]
            cash = _exact_row("derived cash", cash, int)
            position_rows = ledger.conn.execute(
                "SELECT symbol, SUM(quantity_delta) qty FROM paper_position_events "
                "WHERE account_id=? GROUP BY symbol", (request.account_id,)).fetchall()
            holdings: dict[str, int] = {}
            for position_row in position_rows:
                symbol = _exact_row("position symbol", position_row["symbol"], str, set(SUPPORTED_SYMBOLS))
                qty = _exact_row("position quantity", position_row["qty"], int)
                if qty < 0:
                    raise LedgerConflictError("authoritative position quantity is malformed")
                holdings[symbol] = qty

            nav_row = ledger.conn.execute(
                "SELECT * FROM paper_nav_snapshots WHERE account_id=? AND session_date=? "
                "AND julianday(snapshot_at)<=julianday(?) "
                "ORDER BY snapshot_at DESC, "
                "CASE WHEN nav_snapshot_id LIKE 'paper-cycle-%:nav' THEN 1 ELSE 0 END DESC, "
                "nav_snapshot_id DESC LIMIT 1",
                (request.account_id, request.session_id, reviewed_at)).fetchone()
            if nav_row is None:
                raise LedgerConflictError("authoritative current NAV snapshot is required")
            nav = {name: _exact_row(f"NAV {name}", nav_row[name], int)
                   for name in ("cash_krw", "position_value_krw", "nav_krw")}
            snapshot_time = _kst_time("NAV snapshot_at", _exact_row(
                "NAV snapshot_at", nav_row["snapshot_at"], str))
            try:
                cash = ledger.cash_balance_as_of(request.account_id, nav_row["snapshot_at"])
                holdings = ledger.positions_as_of(request.account_id, nav_row["snapshot_at"])
                reviewed_cash = ledger.cash_balance_as_of(request.account_id, reviewed_at)
                reviewed_holdings = ledger.positions_as_of(request.account_id, reviewed_at)
            except ValueError as exc:
                raise LedgerConflictError("authoritative as-of event state is malformed") from exc
            if (snapshot_time > reviewed_time or snapshot_time.date().isoformat() != request.session_id
                    or nav["cash_krw"] != cash
                    or reviewed_cash != cash or reviewed_holdings != holdings
                    or nav["nav_krw"] != nav["cash_krw"] + nav["position_value_krw"]):
                raise LedgerConflictError("authoritative current NAV snapshot is inconsistent")
            try:
                marks = json.loads(_exact_row("NAV position_marks_json", nav_row["position_marks_json"], str))
            except (ValueError, TypeError) as exc:
                raise LedgerConflictError("authoritative NAV position marks are malformed") from exc
            if type(marks) is not dict or set(marks) != {s for s, q in holdings.items() if q != 0}:
                raise LedgerConflictError("authoritative NAV position marks mismatch holdings")
            marked_value = 0
            for symbol, mark in marks.items():
                if (symbol not in SUPPORTED_SYMBOLS or type(mark) is not dict
                        or set(mark) != {"quantity", "price_krw", "feature_snapshot_sha256", "observed_at"}):
                    raise LedgerConflictError("authoritative NAV position marks are malformed")
                quantity, mark_price = mark["quantity"], mark["price_krw"]
                if type(quantity) is not int or quantity <= 0 or type(mark_price) is not int or mark_price <= 0:
                    raise LedgerConflictError("authoritative NAV position marks are malformed")
                if quantity != holdings[symbol]:
                    raise LedgerConflictError("authoritative NAV position marks mismatch holdings")
                _sha256("NAV mark feature_snapshot_sha256", mark["feature_snapshot_sha256"])
                observed = _kst_time("NAV mark observed_at", mark["observed_at"])
                if observed > snapshot_time:
                    raise LedgerConflictError("authoritative NAV mark observation is future-dated")
                marked_value += quantity * mark_price
            if marked_value != nav["position_value_krw"]:
                raise LedgerConflictError("authoritative NAV position marks value mismatch")

            expected_payloads = dict(valuation_snapshots)
            if request.symbol in marks:
                if request.symbol in expected_payloads:
                    raise LedgerConflictError("valuation snapshots must not override request symbol")
                expected_payloads[request.symbol] = request.feature_snapshot
            if set(expected_payloads) != set(marks):
                raise LedgerConflictError("valuation snapshots must exactly cover current holdings")
            for symbol, mark in marks.items():
                payload = expected_payloads.get(symbol)
                if type(payload) is not SymbolFeatureOutput or payload.symbol != symbol:
                    raise LedgerConflictError("valuation snapshot is missing or malformed")
                digest = hashlib.sha256(payload.stable_json().encode("utf-8")).hexdigest()
                if digest != mark["feature_snapshot_sha256"]:
                    raise LedgerConflictError("valuation snapshot hash does not match NAV mark")
                cutoff = _kst_time("valuation available_data_cutoff", payload.available_data_cutoff)
                if cutoff != _kst_time("NAV mark observed_at", mark["observed_at"]):
                    raise LedgerConflictError("valuation snapshot cutoff does not match NAV mark observation")
                close_values = [feature.value for group in payload.groups.values()
                                for feature in group.features if feature.name == "close"]
                if len(close_values) != 1 or type(close_values[0]) not in (int, float):
                    raise LedgerConflictError("canonical close price is missing or malformed")
                close = close_values[0]
                if isinstance(close, float) and (not math.isfinite(close) or not close.is_integer()):
                    raise LedgerConflictError("canonical close price is not positive whole KRW")
                if int(close) < 1 or int(close) != mark["price_krw"]:
                    raise LedgerConflictError("held symbol mark does not equal canonical close")
            price_values = [feature.value for group in request.feature_snapshot.groups.values()
                            for feature in group.features if feature.name == "close"]
            if len(price_values) != 1 or not math.isfinite(float(price_values[0])) or not float(price_values[0]).is_integer():
                raise LedgerConflictError("canonical close price is missing or malformed")
            price = int(price_values[0])

            history = ledger.conn.execute(
                "SELECT session_date, cash_krw, position_value_krw, position_marks_json, nav_krw, snapshot_at FROM paper_nav_snapshots "
                "WHERE account_id=? AND session_date<=? "
                "ORDER BY session_date, snapshot_at, "
                "CASE WHEN nav_snapshot_id LIKE 'paper-cycle-%:nav' THEN 1 ELSE 0 END, "
                "nav_snapshot_id",
                (request.account_id, request.session_id)).fetchall()
            historical_navs: list[tuple[str, int]] = []
            for history_row in history:
                date = _exact_row("historical NAV session_date", history_row["session_date"], str)
                try:
                    datetime.strptime(date, "%Y-%m-%d")
                except ValueError as exc:
                    raise LedgerConflictError("historical NAV session_date is malformed") from exc
                value = _exact_row("historical NAV nav_krw", history_row["nav_krw"], int)
                history_time = _kst_time("historical NAV snapshot_at", _exact_row(
                    "historical NAV snapshot_at", history_row["snapshot_at"], str))
                if history_time > reviewed_time or history_time.date().isoformat() != date:
                    raise LedgerConflictError("historical NAV snapshot is future-dated or causally invalid")
                hcash = _exact_row("historical NAV cash", history_row["cash_krw"], int)
                hposition = _exact_row("historical NAV position value", history_row["position_value_krw"], int)
                try:
                    hmarks = json.loads(_exact_row("historical NAV marks", history_row["position_marks_json"], str))
                except (ValueError, TypeError) as exc:
                    raise LedgerConflictError("historical NAV marks are malformed") from exc
                if type(hmarks) is not dict:
                    raise LedgerConflictError("historical NAV marks are malformed")
                try:
                    historical_cash = ledger.cash_balance_as_of(request.account_id, history_row["snapshot_at"])
                    historical_holdings = ledger.positions_as_of(request.account_id, history_row["snapshot_at"])
                except (LedgerError, ValueError) as exc:
                    raise LedgerConflictError("historical NAV event-sourced state is malformed") from exc
                if hcash != historical_cash or set(hmarks) != set(historical_holdings):
                    raise LedgerConflictError("historical NAV does not match event-sourced state")
                hsum = 0
                for symbol, mark in hmarks.items():
                    if (symbol not in SUPPORTED_SYMBOLS or type(mark) is not dict
                            or set(mark) != {"quantity", "price_krw", "feature_snapshot_sha256", "observed_at"}
                            or type(mark["quantity"]) is not int or mark["quantity"] <= 0
                            or type(mark["price_krw"]) is not int or mark["price_krw"] <= 0):
                        raise LedgerConflictError("historical NAV marks are malformed")
                    if mark["quantity"] != historical_holdings[symbol]:
                        raise LedgerConflictError("historical NAV does not match event-sourced state")
                    _sha256("historical NAV mark hash", mark["feature_snapshot_sha256"])
                    if _kst_time("historical NAV mark observed_at", mark["observed_at"]) > history_time:
                        raise LedgerConflictError("historical NAV mark is causally invalid")
                    hsum += mark["quantity"] * mark["price_krw"]
                if value < 1 or hcash < 0 or hposition < 0 or hsum != hposition or value != hcash + hposition:
                    raise LedgerConflictError("historical NAV is malformed")
                historical_navs.append((date, value))
            high_water = max(value for _, value in historical_navs)
            prior_navs = [value for date, value in historical_navs if date < request.session_id]
            # First session uses current NAV, which cannot manufacture a loss bypass.
            session_start = prior_navs[-1] if prior_navs else nav["nav_krw"]

            reservation_rows = ledger.conn.execute(
                """SELECT r.review_id, r.approved_quantity, r.reference_price_krw,
                          r.reviewed_at, p.symbol, p.side, d.created_at,
                          o.order_id,
                          (SELECT event_type FROM paper_order_events e WHERE e.order_id=o.order_id
                           ORDER BY seq DESC LIMIT 1) AS latest_state,
                          (SELECT seq FROM paper_order_events e WHERE e.order_id=o.order_id
                           ORDER BY seq DESC LIMIT 1) AS latest_seq,
                          (SELECT COUNT(*) FROM paper_order_events e WHERE e.order_id=o.order_id) AS event_count
                   FROM paper_risk_reviews r
                   JOIN paper_order_proposals p ON p.proposal_id=r.proposal_id
                   JOIN paper_agent_decisions d ON d.decision_id=p.decision_id
                   LEFT JOIN paper_orders o ON o.review_id=r.review_id
                   WHERE p.account_id=? AND p.proposal_id<>?
                     AND r.verdict IN ('APPROVE','RESIZE')""",
                (request.account_id, proposal_id),
            ).fetchall()
            effective_cash = cash
            effective_holdings = dict(holdings)
            effective_position_value = nav["position_value_krw"]
            pending_without_order = 0
            for reservation in reservation_rows:
                quantity = _exact_row("reserved quantity", reservation["approved_quantity"], int)
                reference_price = _exact_row("reserved reference price", reservation["reference_price_krw"], int)
                side = _exact_row("reserved side", reservation["side"], str, {"BUY", "SELL"})
                symbol = _exact_row("reserved symbol", reservation["symbol"], str, set(SUPPORTED_SYMBOLS))
                decision_date = _kst_time("reserved decision time", _exact_row(
                    "reserved decision time", reservation["created_at"], str)).date().isoformat()
                _kst_time("reserved review time", _exact_row(
                    "reserved review time", reservation["reviewed_at"], str))
                if quantity <= 0 or reference_price <= 0:
                    raise LedgerConflictError("accepted reservation is malformed")
                if reservation["order_id"] is None and decision_date == request.session_id:
                    pending_without_order += 1
                state_name = reservation["latest_state"]
                reserve = reservation["order_id"] is None
                if reservation["order_id"] is not None:
                    state_name = _exact_row("latest order state", state_name, str,
                                            {"ACCEPTED", "FILLED", "CANCELLED", "EXPIRED"})
                    latest_seq = _exact_row("latest order seq", reservation["latest_seq"], int)
                    event_count = _exact_row("order event count", reservation["event_count"], int)
                    if latest_seq != event_count or latest_seq not in ({1} if state_name == "ACCEPTED" else {2}):
                        raise LedgerConflictError("latest order transition state is malformed")
                    reserve = state_name == "ACCEPTED"
                if reserve and side == "BUY":
                    notional = quantity * reference_price
                    effective_cash -= notional
                    effective_holdings[symbol] = effective_holdings.get(symbol, 0) + quantity
                    effective_position_value += notional
                elif reserve and side == "SELL":
                    remaining = effective_holdings.get(symbol, 0) - quantity
                    if remaining < 0:
                        raise LedgerConflictError("accepted SELL reservations exceed authoritative holdings")
                    effective_holdings[symbol] = remaining
            if effective_cash < 0:
                raise LedgerConflictError("accepted BUY reservations exceed authoritative cash")

            order_rows = ledger.conn.execute(
                "SELECT created_at FROM paper_orders WHERE account_id=?", (request.account_id,)).fetchall()
            fill_rows = ledger.conn.execute(
                "SELECT filled_at FROM paper_fills WHERE account_id=?", (request.account_id,)).fetchall()
            daily_orders = sum(_kst_time("order created_at", _exact_row(
                "order created_at", item[0], str)).date().isoformat() == request.session_id for item in order_rows)
            daily_orders += pending_without_order
            daily_fills = sum(_kst_time("fill filled_at", _exact_row(
                "fill filled_at", item[0], str)).date().isoformat() == request.session_id for item in fill_rows)
            state.update({"cash_krw": effective_cash, "holdings": effective_holdings, "price_krw": price,
                "position_value_krw": effective_position_value, "nav_krw": nav["nav_krw"],
                "high_water_nav_krw": high_water, "session_start_nav_krw": session_start,
                "daily_order_count": daily_orders, "daily_fill_count": daily_fills,
                "nav_snapshot_id": nav_row["nav_snapshot_id"], "nav_snapshot_at": nav_row["snapshot_at"]})
        review_id = _review_identity(proposal_id=proposal_id, reviewed_at=reviewed_at,
                                     request=request, policy=policy, authoritative=state)
        if prior is not None and prior["review_id"] != review_id:
            raise LedgerConflictError("proposal_id already has a different risk review")
        authoritative = replace(
            request, trading_enabled=account_type == "ENABLED",
            kill_switch_engaged=account_type == "ENABLED" and kill_type != "RELEASED",
            duplicate_state_snapshot=duplicate is True,
        )
        if "cash_krw" in state:
            authoritative = replace(authoritative, price_krw=state["price_krw"],
                cash_krw=state["cash_krw"], holdings_quantity=state["holdings"].get(request.symbol, 0),
                aggregate_position_value_krw=state["position_value_krw"], nav_krw=state["nav_krw"],
                high_water_nav_krw=state["high_water_nav_krw"],
                session_start_nav_krw=state["session_start_nav_krw"],
                daily_order_count=state["daily_order_count"], daily_fill_count=state["daily_fill_count"])
        result = evaluate_risk(authoritative, policy)
        if not result.order_required:
            return None
        return ledger.append_risk_review(
            review_id=review_id, proposal_id=proposal_id, verdict=result.verdict,
            approved_quantity=result.approved_quantity, reject_reason_code=result.reject_reason_code,
            reference_price_krw=state.get("price_krw") if result.verdict in {"APPROVE", "RESIZE"} else None,
            policy_sha256=result.policy_sha256, reviewed_at=reviewed_at)
