"""Task 5 paper-only, next-observation execution contract."""
from __future__ import annotations

import ast
import hashlib
import inspect
import threading
from pathlib import Path

import pytest

from paper_trading.ledger import LedgerConflictError, LedgerError, PaperLedger
from strategy.paper_broker import ExecutionPolicy, ExecutionResult, MarketObservation, execute_order

SHA = "a" * 64
DECISION_AT = "2026-08-07T15:30:00+09:00"
NEXT_OPEN = "2026-08-10T09:00:00+09:00"


def _seed(path: Path, *, cash=1_000_000, side="BUY", quantity=10) -> PaperLedger:
    ledger = PaperLedger(path)
    ledger.create_account(account_id="acct", initial_cash_krw=cash, policy_version="v1",
                          created_at="2026-08-07T08:00:00+09:00")
    action = "ENTER" if side == "BUY" else "EXIT"
    ledger.append_agent_decision(decision_id="decision", account_id="acct", symbol="005930",
        ksf_run_id="run", ksf_decision_id="ksf-decision", feature_snapshot_sha256=SHA,
        available_data_cutoff=DECISION_AT, action=action, reason_code="FLOW_SIGNAL_POSITIVE",
        rationale_sha256="b" * 64, created_at=DECISION_AT)
    ledger.append_order_proposal(proposal_id="proposal", decision_id="decision", side=side,
        target_exposure_bp=1_000 if side == "BUY" else 0, idempotency_key="proposal-key",
        model_name="model", model_version="v1", proposed_at=DECISION_AT)
    ledger.append_risk_review(review_id="review", proposal_id="proposal", verdict="APPROVE",
        approved_quantity=quantity, reference_price_krw=10_000, policy_sha256="c" * 64,
        reviewed_at=DECISION_AT)
    ledger.append_order(order_id="order", review_id="review", idempotency_key="order-key",
                        created_at=DECISION_AT)
    return ledger


def _obs(**changes) -> MarketObservation:
    values = dict(symbol="005930", reference_price_krw=10_000, observed_at=NEXT_OPEN,
                  source_sha256=SHA)
    values.update(changes)
    return MarketObservation(**values)


def _append_order(ledger: PaperLedger, *, prefix: str, side: str, quantity: int,
                  created_at: str) -> str:
    action = "ENTER" if side == "BUY" else "EXIT"
    ledger.append_agent_decision(decision_id=f"{prefix}-decision", account_id="acct",
        symbol="005930", ksf_run_id=f"{prefix}-run", ksf_decision_id=f"{prefix}-ksf-decision",
        feature_snapshot_sha256=SHA, available_data_cutoff=created_at, action=action,
        reason_code="FLOW_SIGNAL_POSITIVE", rationale_sha256="b" * 64, created_at=created_at)
    ledger.append_order_proposal(proposal_id=f"{prefix}-proposal",
        decision_id=f"{prefix}-decision", side=side,
        target_exposure_bp=1_000 if side == "BUY" else 0,
        idempotency_key=f"{prefix}-proposal-key", model_name="model", model_version="v1",
        proposed_at=created_at)
    ledger.append_risk_review(review_id=f"{prefix}-review", proposal_id=f"{prefix}-proposal",
        verdict="APPROVE", approved_quantity=quantity, reference_price_krw=10_000,
        policy_sha256="c" * 64, reviewed_at=created_at)
    order_id = f"{prefix}-order"
    ledger.append_order(order_id=order_id, review_id=f"{prefix}-review",
        idempotency_key=f"{prefix}-order-key", created_at=created_at)
    return order_id


def _counts(ledger):
    return tuple(ledger.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in
                 ("paper_fills", "paper_cash_events", "paper_position_events",
                  "paper_nav_snapshots", "paper_order_events"))


def test_same_observation_rejected_without_side_effects(tmp_path):
    ledger = _seed(tmp_path / "same.sqlite3")
    before = _counts(ledger)
    with pytest.raises(LedgerError, match="strictly later"):
        execute_order(ledger, order_id="order", execution_id="exec", observation=_obs(
            observed_at=DECISION_AT), policy=ExecutionPolicy(), executed_at=DECISION_AT)
    assert _counts(ledger) == before


def test_later_buy_has_exact_deterministic_arithmetic_and_evidence(tmp_path):
    ledger = _seed(tmp_path / "buy.sqlite3")
    policy = ExecutionPolicy(fee_bps=15, sell_tax_bps=20, slippage_bps=25)
    result = execute_order(ledger, order_id="order", execution_id="exec", observation=_obs(),
                           policy=policy, executed_at=NEXT_OPEN)
    assert isinstance(result, ExecutionResult)
    assert "record_nav" not in inspect.signature(execute_order).parameters
    # BUY slippage rounds up: ceil(10,000 * 25 / 10,000) = 25; fee rounds up.
    assert (result.raw_reference_price_krw, result.fill_price_krw, result.quantity) == (10_000, 10_025, 10)
    assert (result.fee_krw, result.tax_krw, result.slippage_krw) == (151, 0, 250)
    # NAV marks at the raw observation (not the adverse execution price).
    assert (result.cash_krw, result.position_quantity, result.nav_krw) == (899_599, 10, 999_599)
    fill = dict(ledger.conn.execute("SELECT * FROM paper_fills").fetchone())
    assert (fill["raw_reference_price_krw"], fill["slippage_bps"], fill["fee_bps"],
            fill["tax_bps"], fill["execution_policy_sha256"]) == (10_000, 25, 15, 20, policy.sha256)
    assert fill["observation_sha256"] == SHA and fill["observed_at"] == NEXT_OPEN
    assert ledger.conn.execute(
        "SELECT count(*) FROM paper_nav_snapshots WHERE nav_snapshot_id='exec:nav'"
    ).fetchone()[0] == 1
    assert ledger.order_state("order") == "FILLED"


def test_buy_then_sell_full_exit_has_exact_arithmetic_state_and_evidence(tmp_path):
    ledger = _seed(tmp_path / "sell-success.sqlite3")
    execute_order(ledger, order_id="order", execution_id="buy-exec", observation=_obs(),
                  policy=ExecutionPolicy(fee_bps=0, sell_tax_bps=0, slippage_bps=0),
                  executed_at=NEXT_OPEN)
    sell_created = "2026-08-10T09:01:00+09:00"
    sell_order = _append_order(ledger, prefix="sell", side="SELL", quantity=10,
                               created_at=sell_created)
    sell_observation = _obs(reference_price_krw=10_001,
                            observed_at="2026-08-10T09:02:00+09:00")
    policy = ExecutionPolicy(fee_bps=15, sell_tax_bps=20, slippage_bps=25)
    result = execute_order(ledger, order_id=sell_order, execution_id="sell-exec",
        observation=sell_observation, policy=policy, executed_at="2026-08-10T09:03:00+09:00")

    # SELL slippage is adverse and rounds up per share: ceil(10,001 * 25 / 10,000) = 26.
    assert (result.raw_reference_price_krw, result.fill_price_krw, result.quantity) == (10_001, 9_975, 10)
    assert (result.fee_krw, result.tax_krw, result.slippage_krw) == (150, 200, 260)
    assert (result.cash_krw, result.position_quantity, result.nav_krw) == (999_400, 0, 999_400)
    assert ledger.positions("acct") == {}
    nav = dict(ledger.conn.execute(
        "SELECT * FROM paper_nav_snapshots WHERE nav_snapshot_id='sell-exec:nav'").fetchone())
    assert (nav["cash_krw"], nav["position_value_krw"], nav["nav_krw"],
            nav["position_marks_json"]) == (999_400, 0, 999_400, "{}")
    fill = dict(ledger.conn.execute(
        "SELECT * FROM paper_fills WHERE fill_id='sell-exec'").fetchone())
    assert (fill["side"], fill["raw_reference_price_krw"], fill["fill_price_krw"],
            fill["fee_krw"], fill["tax_krw"], fill["slippage_krw"],
            fill["execution_policy_sha256"], fill["observation_sha256"],
            fill["observed_at"]) == ("SELL", 10_001, 9_975, 150, 200, 260,
                                      policy.sha256, SHA, sell_observation.observed_at)
    assert ledger.order_state(sell_order) == "FILLED"


def test_authoritative_cash_and_holdings_reject_and_rollback(tmp_path):
    buy = _seed(tmp_path / "cash.sqlite3", cash=100_000)
    with pytest.raises(LedgerError, match="cash"):
        execute_order(buy, order_id="order", execution_id="x", observation=_obs(),
                      policy=ExecutionPolicy(fee_bps=1), executed_at=NEXT_OPEN)
    assert _counts(buy)[0:4] == (0, 1, 0, 0)
    sell = _seed(tmp_path / "shares.sqlite3", side="SELL")
    with pytest.raises(LedgerError, match="holdings"):
        execute_order(sell, order_id="order", execution_id="x", observation=_obs(),
                      policy=ExecutionPolicy(), executed_at=NEXT_OPEN)
    assert _counts(sell)[0:4] == (0, 1, 0, 0)


def test_exact_replay_reuses_result_conflict_fails_closed(tmp_path):
    ledger = _seed(tmp_path / "replay.sqlite3")
    args = dict(order_id="order", execution_id="exec", observation=_obs(),
                policy=ExecutionPolicy(), executed_at=NEXT_OPEN)
    first = execute_order(ledger, **args)
    counts = _counts(ledger)
    assert execute_order(ledger, **args) == first
    assert _counts(ledger) == counts
    with pytest.raises(LedgerConflictError):
        execute_order(ledger, **{**args, "observation": _obs(reference_price_krw=10_001)})
    assert _counts(ledger) == counts


def test_exact_replay_returns_original_historical_position_after_later_fill(tmp_path):
    ledger = _seed(tmp_path / "historical-replay.sqlite3")
    first_args = dict(order_id="order", execution_id="first-exec", observation=_obs(),
                      policy=ExecutionPolicy(), executed_at=NEXT_OPEN)
    original = execute_order(ledger, **first_args)

    later_created = "2026-08-10T09:01:00+09:00"
    later_order = _append_order(ledger, prefix="later", side="BUY", quantity=3,
                                created_at=later_created)
    execute_order(ledger, order_id=later_order, execution_id="later-exec",
        observation=_obs(observed_at="2026-08-10T09:02:00+09:00"),
        policy=ExecutionPolicy(), executed_at="2026-08-10T09:03:00+09:00")

    assert ledger.positions("acct") == {"005930": 13}
    assert execute_order(ledger, **first_args) == original


@pytest.mark.parametrize("stage", ["fill", "cash", "position", "nav", "order"])
def test_fault_after_each_append_stage_rolls_back_everything(tmp_path, stage):
    ledger = _seed(tmp_path / f"fault-{stage}.sqlite3")
    before = _counts(ledger)
    def fault(current):
        if current == stage:
            raise RuntimeError(stage)
    with pytest.raises(RuntimeError, match=stage):
        execute_order(ledger, order_id="order", execution_id="exec", observation=_obs(),
                      policy=ExecutionPolicy(), executed_at=NEXT_OPEN, fault_hook=fault)
    assert _counts(ledger) == before


@pytest.mark.parametrize("changes", [
    {"reference_price_krw": True}, {"reference_price_krw": 0}, {"symbol": "035420"},
    {"source_sha256": "A" * 64}, {"source_sha256": "x" * 64},
    {"observed_at": "2026-08-10T09:00:00"},
    {"observed_at": "2026-08-10T00:00:00+00:00"},
])
def test_malformed_observation_fails_closed(tmp_path, changes):
    ledger = _seed(tmp_path / "bad.sqlite3")
    with pytest.raises((LedgerError, ValueError)):
        execute_order(ledger, order_id="order", execution_id="exec", observation=_obs(**changes),
                      policy=ExecutionPolicy(), executed_at=NEXT_OPEN)
    assert ledger.conn.execute("SELECT count(*) FROM paper_fills").fetchone()[0] == 0


def test_future_observation_and_causal_execution_rejected(tmp_path):
    ledger = _seed(tmp_path / "future.sqlite3")
    with pytest.raises(LedgerError):
        execute_order(ledger, order_id="order", execution_id="exec", observation=_obs(
            observed_at="2026-08-10T09:01:00+09:00"), policy=ExecutionPolicy(), executed_at=NEXT_OPEN)


def test_runtime_import_graph_has_no_network_or_broker_dependencies():
    root = Path(__file__).resolve().parents[1]
    forbidden = {"requests", "httpx", "urllib", "socket", "subprocess", "pykis", "kis"}
    pending = [root / "strategy" / "paper_broker.py", root / "paper_trading" / "ledger.py"]
    seen = set()
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        seen.add(path)
        tree = ast.parse(path.read_text())
        imports = {n.names[0].name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)}
        imports |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        assert imports.isdisjoint(forbidden)


def test_concurrent_duplicate_execution_is_one_economic_mutation(tmp_path):
    path = tmp_path / "race.sqlite3"
    _seed(path).close()
    barrier = threading.Barrier(2)
    outcomes = []
    def worker():
        with PaperLedger(path) as ledger:
            barrier.wait()
            outcomes.append(execute_order(ledger, order_id="order", execution_id="exec",
                observation=_obs(), policy=ExecutionPolicy(), executed_at=NEXT_OPEN))
    threads = [threading.Thread(target=worker) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    with PaperLedger(path) as ledger:
        assert len(outcomes) == 2 and outcomes[0] == outcomes[1]
        assert _counts(ledger) == (1, 3, 1, 1, 2)
