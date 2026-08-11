"""Deterministic event-sourced paper execution; never connects to a broker.

Defaults are deliberately simple simulation assumptions, not claims about any
broker's actual fee, tax, or execution schedule. All rates are integer basis
points. Adverse price movement and charges round up to a whole KRW so a paper
account never receives optimistic fractional-KRW treatment.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from typing import Callable

from paper_trading.ledger import (
    LedgerConflictError, LedgerError, PaperLedger, SUPPORTED_SYMBOLS,
    _require_int, _require_kst_iso, _require_sha256,
)


def _ceil_div(value: int, divisor: int) -> int:
    return (value + divisor - 1) // divisor


@dataclass(frozen=True)
class ExecutionPolicy:
    """Paper assumptions: 15bp fee, 20bp sell tax, 10bp adverse slippage."""
    fee_bps: int = 15
    sell_tax_bps: int = 20
    slippage_bps: int = 10

    def __post_init__(self) -> None:
        for name in ("fee_bps", "sell_tax_bps", "slippage_bps"):
            value = _require_int(name, getattr(self, name), minimum=0)
            if value > 10_000:
                raise LedgerError(f"{name}: must be <= 10000")

    @property
    def sha256(self) -> str:
        payload = json.dumps({"fee_bps": self.fee_bps,
            "sell_tax_bps": self.sell_tax_bps, "slippage_bps": self.slippage_bps},
            sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class MarketObservation:
    symbol: str
    reference_price_krw: int
    observed_at: str
    source_sha256: str

    def __post_init__(self) -> None:
        if type(self.symbol) is not str or self.symbol not in SUPPORTED_SYMBOLS:
            raise LedgerError("symbol: unsupported symbol")
        _require_int("reference_price_krw", self.reference_price_krw, minimum=1)
        _require_kst_iso("observed_at", self.observed_at)
        _require_sha256("source_sha256", self.source_sha256)


@dataclass(frozen=True)
class ExecutionResult:
    execution_id: str
    order_id: str
    raw_reference_price_krw: int
    fill_price_krw: int
    quantity: int
    fee_krw: int
    tax_krw: int
    slippage_krw: int
    cash_krw: int
    position_quantity: int
    nav_krw: int


def _result(ledger: PaperLedger, fill: dict) -> ExecutionResult:
    nav = ledger.conn.execute(
        "SELECT * FROM paper_nav_snapshots WHERE nav_snapshot_id=?", (fill["fill_id"] + ":nav",)
    ).fetchone()
    if nav is None:
        raise LedgerConflictError("execution fill exists without its atomic NAV snapshot")
    try:
        marks = json.loads(nav["position_marks_json"])
        if type(marks) is not dict or nav["account_id"] != fill["account_id"] \
                or nav["snapshot_at"] != fill["filled_at"]:
            raise ValueError
        position_value = 0
        for symbol, mark in marks.items():
            if symbol not in SUPPORTED_SYMBOLS or type(mark) is not dict or set(mark) != {
                    "quantity", "price_krw", "feature_snapshot_sha256", "observed_at"}:
                raise ValueError
            quantity = _require_int("position mark quantity", mark["quantity"], minimum=1)
            price = _require_int("position mark price_krw", mark["price_krw"], minimum=1)
            _require_sha256("position mark feature_snapshot_sha256",
                            mark["feature_snapshot_sha256"])
            _require_kst_iso("position mark observed_at", mark["observed_at"])
            position_value += quantity * price
        if type(nav["cash_krw"]) is not int or type(nav["position_value_krw"]) is not int \
                or type(nav["nav_krw"]) is not int \
                or position_value != nav["position_value_krw"] \
                or nav["nav_krw"] != nav["cash_krw"] + position_value:
            raise ValueError
        position_quantity = marks.get(fill["symbol"], {}).get("quantity", 0)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, LedgerError) as exc:
        raise LedgerConflictError("execution NAV snapshot lineage is malformed") from exc
    return ExecutionResult(fill["fill_id"], fill["order_id"], fill["raw_reference_price_krw"],
        fill["fill_price_krw"], fill["quantity"], fill["fee_krw"], fill["tax_krw"],
        fill["slippage_krw"], nav["cash_krw"], position_quantity, nav["nav_krw"])


def execute_order(ledger: PaperLedger, *, order_id: str, execution_id: str,
                  observation: MarketObservation, policy: ExecutionPolicy,
                  executed_at: str, fault_hook: Callable[[str], None] | None = None,
                  **untrusted_authorization_values) -> ExecutionResult:
    """Fill one accepted order from a strictly later KST observation atomically."""
    if type(ledger) is not PaperLedger or type(observation) is not MarketObservation \
            or type(policy) is not ExecutionPolicy:
        raise LedgerError("exact ledger, observation, and policy types required")
    if untrusted_authorization_values:
        raise LedgerError("caller-supplied portfolio authorization is forbidden")
    executed_at = _require_kst_iso("executed_at", executed_at)
    observed_dt = datetime.fromisoformat(observation.observed_at)
    executed_dt = datetime.fromisoformat(executed_at)
    if observed_dt > executed_dt:
        raise LedgerError("observation cannot be in the future relative to execution")
    hook = fault_hook or (lambda _stage: None)

    with ledger.transaction_immediate():
        row = ledger.conn.execute("""
            SELECT o.*, d.available_data_cutoff, d.created_at AS decision_created_at
            FROM paper_orders o
            JOIN paper_order_proposals p ON p.proposal_id=o.proposal_id
            JOIN paper_agent_decisions d ON d.decision_id=p.decision_id
            WHERE o.order_id=?
        """, (order_id,)).fetchone()
        if row is None:
            raise LedgerError(f"unknown order_id {order_id!r}")
        order = dict(row)
        if observation.symbol != order["symbol"]:
            raise LedgerError("observation symbol does not match order")
        if observed_dt <= datetime.fromisoformat(order["available_data_cutoff"]) \
                or observed_dt <= datetime.fromisoformat(order["decision_created_at"]):
            raise LedgerError("execution observation must be strictly later than decision/cutoff")

        slip_per_share = _ceil_div(observation.reference_price_krw * policy.slippage_bps, 10_000)
        fill_price = (observation.reference_price_krw + slip_per_share if order["side"] == "BUY"
                      else observation.reference_price_krw - slip_per_share)
        if fill_price <= 0:
            raise LedgerError("slippage produced a non-positive fill price")
        gross = fill_price * order["quantity"]
        fee = _ceil_div(gross * policy.fee_bps, 10_000) if policy.fee_bps else 0
        tax = (_ceil_div(gross * policy.sell_tax_bps, 10_000)
               if order["side"] == "SELL" and policy.sell_tax_bps else 0)
        expected = {"fill_id": execution_id, "order_id": order_id,
            "raw_reference_price_krw": observation.reference_price_krw,
            "fill_price_krw": fill_price, "quantity": order["quantity"], "fee_krw": fee,
            "tax_krw": tax, "slippage_krw": slip_per_share * order["quantity"],
            "fee_bps": policy.fee_bps, "tax_bps": policy.sell_tax_bps,
            "slippage_bps": policy.slippage_bps, "execution_policy_sha256": policy.sha256,
            "observed_at": observation.observed_at,
            "observation_sha256": observation.source_sha256, "filled_at": executed_at}
        existing_row = ledger.conn.execute("SELECT * FROM paper_fills WHERE fill_id=?",
                                           (execution_id,)).fetchone()
        if existing_row is not None:
            existing = dict(existing_row)
            if any(existing[key] != value for key, value in expected.items()):
                raise LedgerConflictError("execution identity already recorded with different payload")
            return _result(ledger, existing)
        if ledger.order_state(order_id) != "ACCEPTED":
            raise LedgerConflictError("order is not in authoritative ACCEPTED state")
        cash = ledger.cash_balance(order["account_id"])
        holdings = ledger.positions(order["account_id"]).get(order["symbol"], 0)
        if order["side"] == "BUY" and cash < gross + fee + tax:
            raise LedgerError("authoritative cash is insufficient after execution costs")
        if order["side"] == "SELL" and holdings < order["quantity"]:
            raise LedgerError("authoritative holdings are insufficient")

        ledger.append_fill(**expected)
        for stage in ("fill", "cash", "position"):
            hook(stage)
        new_cash = ledger.cash_balance(order["account_id"])
        positions = ledger.positions(order["account_id"])
        prior = ledger.latest_nav(order["account_id"])
        prior_marks = json.loads(prior["position_marks_json"]) if prior else {}
        marks = {}
        for symbol, quantity in positions.items():
            if symbol == observation.symbol:
                marks[symbol] = {"quantity": quantity, "price_krw": observation.reference_price_krw,
                    "feature_snapshot_sha256": observation.source_sha256,
                    "observed_at": observation.observed_at}
            elif symbol in prior_marks:
                marks[symbol] = {**prior_marks[symbol], "quantity": quantity}
            else:
                raise LedgerError("missing authoritative mark for an existing position")
        position_value = sum(m["quantity"] * m["price_krw"] for m in marks.values())
        ledger.append_nav_snapshot(nav_snapshot_id=execution_id + ":nav",
            account_id=order["account_id"], session_date=executed_at[:10], cash_krw=new_cash,
            position_value_krw=position_value, nav_krw=new_cash + position_value,
            position_marks=marks, snapshot_at=executed_at)
        hook("nav")
        hook("order")
        return _result(ledger, dict(ledger.conn.execute(
            "SELECT * FROM paper_fills WHERE fill_id=?", (execution_id,)).fetchone()))
