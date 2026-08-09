"""Task 4 deterministic paper-risk contract tests."""
from dataclasses import asdict, replace
from contextlib import contextmanager
import hashlib
import json
import sqlite3
import threading
from pathlib import Path

import pytest

from paper_trading.ledger import LedgerConflictError, PaperLedger, RISK_REJECT_REASON_CODES
from ksf.feature_engine import FeatureEngine
from strategy.paper_risk import RiskInput, RiskPolicy, RiskResult, evaluate_risk, review_and_persist


def _feature_snapshot(*, omit=(), overrides=None, symbol="005930"):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE ksf_normalized_features (
        feature_id TEXT, symbol TEXT, feature_group TEXT, feature_name TEXT,
        value_num REAL, unit TEXT, source_as_of TEXT, feature_status TEXT,
        missing_reason TEXT, ingested_at_kst TEXT, available_data_cutoff TEXT)""")
    rows = {
        "close": (70000.0, "KRW", "domestic_price", "READY"),
        "volume": (1000.0, "shares", "domestic_price", "READY"),
        "foreigner_net_buy_quantity": (100.0, "shares", "domestic_investor_flow", "READY"),
        "institution_total_net_buy_quantity": (100.0, "shares", "domestic_investor_flow", "READY"),
    }
    rows.update(overrides or {})
    for name, (value, unit, group, status) in rows.items():
        if name in omit:
            continue
        conn.execute("INSERT INTO ksf_normalized_features VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
            "f-" + name, symbol, group, name, value, unit,
            "2026-08-08T09:58:30+09:00", status, None,
            "2026-08-08T09:58:31+09:00", "2026-08-08T09:58:31+09:00"))
    output = FeatureEngine(conn).build_symbol(
        symbol, available_data_cutoff="2026-08-08T09:59:00+09:00",
        as_of_kst="2026-08-08T10:00:00+09:00")
    conn.close()
    return output


def _mark(snapshot, quantity: int, price_krw: int) -> dict[str, object]:
    return {"quantity": quantity, "price_krw": price_krw,
            "feature_snapshot_sha256": hashlib.sha256(snapshot.stable_json().encode()).hexdigest(),
            "observed_at": snapshot.available_data_cutoff}


def test_forged_risk_result_has_no_storage_boundary() -> None:
    import strategy.paper_risk as module

    assert not hasattr(module, "persist_risk_review")
    with pytest.raises(ValueError):
        RiskResult("APPROVE", 999, None, "0" * 64, False)


@pytest.fixture
def policy() -> RiskPolicy:
    return RiskPolicy(2_500, 8_000, 5, 5, 500_000, 1_000, 10_000, 300)


@pytest.fixture
def risk_input() -> RiskInput:
    snapshot = _feature_snapshot()
    return RiskInput(
        account_id="acct-1", ksf_run_id="run-1", ksf_decision_id="ksf-dec-1",
        feature_snapshot_sha256=hashlib.sha256(snapshot.stable_json().encode()).hexdigest(), session_id="2026-08-08",
        symbol="005930", action="ENTER", target_exposure_bps=2_000,
        price_krw=70_000, cash_krw=10_000_000, holdings_quantity=0,
        aggregate_position_value_krw=0, nav_krw=10_000_000,
        high_water_nav_krw=10_000_000, session_start_nav_krw=10_000_000,
        daily_order_count=0, daily_fill_count=0, trading_enabled=True,
        kill_switch_engaged=False, duplicate_state_snapshot=False,
        decision_at="2026-08-08T10:00:00+09:00",
        available_data_cutoff="2026-08-08T09:59:00+09:00",
        feature_snapshot=snapshot,
    )


def test_risk_input_requires_actual_canonical_feature_snapshot() -> None:
    snapshot = _feature_snapshot()
    digest = hashlib.sha256(snapshot.stable_json().encode()).hexdigest()
    assert snapshot.scoring_allowed
    assert digest


def test_engine_owns_quantity_and_approves_integer_target(risk_input, policy) -> None:
    result = evaluate_risk(risk_input, policy)

    assert result.verdict == "APPROVE"
    assert result.approved_quantity == 28  # floor(20% NAV / KRW 70,000)
    assert result.reject_reason_code is None


@pytest.mark.parametrize(("changes", "reason"), [
    ({"trading_enabled": False, "kill_switch_engaged": True}, "TRADING_DISABLED"),
    ({"kill_switch_engaged": True, "duplicate_state_snapshot": True}, "KILL_SWITCH_ACTIVE"),
    ({"duplicate_state_snapshot": True}, "DUPLICATE_SESSION"),
    ({"action": "ENTER", "target_exposure_bps": 0}, "INVALID_ACTION"),
    ({"action": "REDUCE", "holdings_quantity": 0}, "INSUFFICIENT_POSITION"),
    ({"cash_krw": 1, "aggregate_position_value_krw": 9_999_999}, "INSUFFICIENT_CASH"),
    ({"daily_order_count": 5}, "DAILY_FILL_LIMIT"),
    ({"daily_fill_count": 5}, "DAILY_FILL_LIMIT"),
    ({"nav_krw": 9_500_000, "cash_krw": 9_500_000}, "DAILY_LOSS_STOP"),
    ({"nav_krw": 9_000_000, "cash_krw": 9_000_000}, "DAILY_LOSS_STOP"),
])
def test_rejection_precedence_paths(risk_input, policy, changes, reason) -> None:
    assert evaluate_risk(replace(risk_input, **changes), policy).reject_reason_code == reason


def test_position_and_aggregate_limit_rejections(risk_input, policy) -> None:
    position = replace(risk_input, target_exposure_bps=5_000, holdings_quantity=44,
                       aggregate_position_value_krw=3_100_000, cash_krw=6_900_000)
    assert evaluate_risk(position, policy).reject_reason_code == "POSITION_LIMIT"
    aggregate = replace(risk_input, target_exposure_bps=5_000,
                        aggregate_position_value_krw=8_100_000, cash_krw=1_900_000)
    assert evaluate_risk(aggregate, policy).reject_reason_code == "EXPOSURE_LIMIT"


def test_drawdown_exact_boundary_and_daily_loss_order(risk_input, policy) -> None:
    at_drawdown = replace(risk_input, nav_krw=9_000_000, cash_krw=9_000_000,
                          session_start_nav_krw=9_000_000)
    assert evaluate_risk(at_drawdown, policy).reject_reason_code == "DRAWDOWN_STOP"
    before = replace(risk_input, nav_krw=9_000_001, cash_krw=9_000_001,
                     high_water_nav_krw=10_000_000, session_start_nav_krw=9_000_001)
    assert evaluate_risk(before, policy).verdict in {"APPROVE", "RESIZE"}


def test_daily_order_and_fill_caps_are_independent_at_boundaries(risk_input, policy) -> None:
    unequal = replace(policy, daily_order_cap=2, daily_fill_cap=4)
    assert evaluate_risk(replace(risk_input, daily_order_count=2, daily_fill_count=3), unequal).reject_reason_code == "DAILY_FILL_LIMIT"
    assert evaluate_risk(replace(risk_input, daily_order_count=1, daily_fill_count=4), unequal).reject_reason_code == "DAILY_FILL_LIMIT"
    assert evaluate_risk(replace(risk_input, daily_order_count=1, daily_fill_count=3), unequal).verdict == "APPROVE"


def test_resize_for_position_cap_and_cash(risk_input, policy) -> None:
    capped = evaluate_risk(replace(risk_input, target_exposure_bps=4_000), policy)
    assert (capped.verdict, capped.approved_quantity) == ("RESIZE", 35)
    cash_limited = replace(risk_input, target_exposure_bps=2_000, cash_krw=1_000_000,
                           aggregate_position_value_krw=9_000_000)
    limited_policy = replace(policy, max_aggregate_bps=10_000)
    resized = evaluate_risk(cash_limited, limited_policy)
    assert (resized.verdict, resized.approved_quantity) == ("RESIZE", 14)


@pytest.mark.parametrize(("action", "holdings", "target", "verdict", "qty"), [
    ("HOLD", 10, 700, "NO_OP", None),
    ("ABSTAIN", 0, 0, "NO_OP", None),
    ("EXIT", 0, 0, "NO_OP", None),
    ("EXIT", 10, 0, "APPROVE", 10),
    ("REDUCE", 10, 350, "APPROVE", 5),
])
def test_action_validity_and_no_op(risk_input, policy, action, holdings, target, verdict, qty) -> None:
    value = holdings * risk_input.price_krw
    candidate = replace(risk_input, action=action, holdings_quantity=holdings,
                        target_exposure_bps=target, aggregate_position_value_krw=value,
                        cash_krw=risk_input.nav_krw - value)
    result = evaluate_risk(candidate, policy)
    assert (result.verdict, result.approved_quantity) == (verdict, qty)
    assert result.order_required is (verdict != "NO_OP")


def test_integer_rounding_zero_one_share_and_min_notional(risk_input, policy) -> None:
    one = replace(risk_input, target_exposure_bps=70)  # exactly KRW 70,000 => one share
    assert evaluate_risk(one, policy).approved_quantity == 1
    zero = replace(risk_input, target_exposure_bps=69)
    assert evaluate_risk(zero, policy).reject_reason_code == "INVALID_ACTION"
    high_min = replace(policy, min_notional_krw=70_001)
    assert evaluate_risk(one, high_min).reject_reason_code == "MIN_NOTIONAL"


def test_canonical_snapshot_required_omission_rejects_and_optional_omission_allows(risk_input, policy) -> None:
    missing = _feature_snapshot(omit={"close"})
    missing_request = replace(risk_input, feature_snapshot=missing,
        feature_snapshot_sha256=hashlib.sha256(missing.stable_json().encode()).hexdigest())
    assert evaluate_risk(missing_request, policy).reject_reason_code == "STALE_DATA"
    assert risk_input.feature_snapshot.state == "PARTIAL_DATA"
    assert evaluate_risk(risk_input, policy).verdict == "APPROVE"


@pytest.mark.parametrize("timestamp", [
    "2026-08-08T09:59:00", "2026-08-08T00:59:00+00:00",
])
def test_cutoff_malformed_timestamp_raises_exactly(risk_input, timestamp) -> None:
    with pytest.raises(ValueError, match=r"explicit \+09:00 offset"):
        replace(risk_input, available_data_cutoff=timestamp)


def test_snapshot_hash_and_exact_type_fail_closed(risk_input) -> None:
    with pytest.raises(ValueError, match="sha256 mismatch"):
        replace(risk_input, feature_snapshot_sha256="0" * 64)
    class Deceptive(type(risk_input.feature_snapshot)):
        pass
    with pytest.raises(ValueError, match="exact canonical"):
        replace(risk_input, feature_snapshot=Deceptive(**{
            name: getattr(risk_input.feature_snapshot, name)
            for name in risk_input.feature_snapshot.__dataclass_fields__
        }))


def test_policy_hash_is_canonical_and_closed(policy) -> None:
    reversed_map = dict(reversed(list(asdict(policy).items())))
    assert RiskPolicy.from_mapping(reversed_map).sha256 == policy.sha256
    assert len(policy.sha256) == 64 and policy.sha256 == policy.sha256.lower()
    with pytest.raises(ValueError):
        RiskPolicy.from_mapping({**asdict(policy), "extra": 1})


@pytest.mark.parametrize(("field", "bad"), [
    ("price_krw", True), ("cash_krw", -1), ("symbol", "035420"),
    ("action", "BUY"), ("trading_enabled", 1), ("feature_snapshot", {}),
])
def test_malformed_untrusted_values_fail_closed(risk_input, field, bad) -> None:
    with pytest.raises(ValueError):
        replace(risk_input, **{field: bad})
    payload = {name: getattr(risk_input, name) for name in risk_input.__dataclass_fields__}
    payload["extra"] = "secret"
    with pytest.raises(ValueError):
        RiskInput.from_mapping(payload)


def _ledger_with_proposal(path: Path, *, operational: bool = True, with_nav: bool = True,
                          side: str = "BUY",
                          target_exposure_bp: int = 2_000, action: str | None = None,
                          symbol: str = "005930", decision_id: str = "dec-1",
                          proposal_id: str = "prop-1", ksf_run_id: str = "run-1",
                          ksf_decision_id: str = "ksf-dec-1",
                          account_created_at: str = "2026-08-08T08:00:00+09:00") -> PaperLedger:
    ledger = PaperLedger(path)
    ledger.create_account(account_id="acct-1", initial_cash_krw=10_000_000,
                          policy_version="v1", created_at=account_created_at)
    if operational:
        ledger.append_account_event(account_event_id="enable-1", account_id="acct-1",
            event_type="ENABLED", reason_code="MANUAL", event_at="2026-08-08T08:01:00+09:00")
        ledger.append_kill_switch_event(kill_switch_event_id="release-1", account_id="acct-1",
            event_type="RELEASED", reason_code="MANUAL", event_at="2026-08-08T08:02:00+09:00")
    ledger.append_agent_decision(
        decision_id=decision_id, account_id="acct-1", symbol=symbol, ksf_run_id=ksf_run_id,
        ksf_decision_id=ksf_decision_id, feature_snapshot_sha256=hashlib.sha256(_feature_snapshot().stable_json().encode()).hexdigest(),
        available_data_cutoff="2026-08-08T09:59:00+09:00",
        action=action or ("ENTER" if side == "BUY" else "EXIT" if target_exposure_bp == 0 else "REDUCE"),
        reason_code="FLOW_SIGNAL_POSITIVE", rationale_sha256="b" * 64,
        created_at="2026-08-08T10:00:00+09:00")
    ledger.append_order_proposal(
        proposal_id=proposal_id, decision_id=decision_id, side=side,
        target_exposure_bp=target_exposure_bp, idempotency_key="prop-key", model_name="model",
        model_version="v1", proposed_at="2026-08-08T10:00:00+09:00")
    if with_nav:
        ledger.append_nav_snapshot(nav_snapshot_id="nav-current", account_id="acct-1",
            session_date="2026-08-08", cash_krw=10_000_000, position_value_krw=0,
            position_marks={}, nav_krw=10_000_000,
            snapshot_at="2026-08-08T09:59:30+09:00")
    return ledger


def test_integrated_boundary_binds_account_and_risk_owns_quantity(tmp_path, risk_input, policy) -> None:
    ledger = _ledger_with_proposal(tmp_path / "binding.sqlite3")
    try:
        with pytest.raises(ValueError, match="account_id"):
            review_and_persist(
                ledger, proposal_id="prop-1",
                reviewed_at=risk_input.decision_at,
                request=replace(risk_input, account_id="acct-other"), policy=policy,
            )
        assert ledger.conn.execute("SELECT COUNT(*) FROM paper_risk_reviews").fetchone()[0] == 0
        review_and_persist(
            ledger, proposal_id="prop-1",
            reviewed_at=risk_input.decision_at, request=risk_input, policy=policy,
        )
        row = dict(ledger.conn.execute("SELECT * FROM paper_risk_reviews").fetchone())
        assert (row["verdict"], row["approved_quantity"]) == ("APPROVE", 28)
    finally:
        ledger.close()


def test_integrated_boundary_cannot_forge_disabled_ledger_to_approve(tmp_path, risk_input, policy) -> None:
    ledger = _ledger_with_proposal(tmp_path / "disabled.sqlite3", operational=False)
    try:
        review_and_persist(ledger, proposal_id="prop-1",
            reviewed_at=risk_input.decision_at, request=risk_input, policy=policy)
        row = dict(ledger.conn.execute("SELECT * FROM paper_risk_reviews").fetchone())
        assert (row["verdict"], row["reject_reason_code"]) == ("REJECT", "TRADING_DISABLED")
    finally:
        ledger.close()


def test_integrated_missing_current_nav_snapshot_fails_closed(tmp_path, risk_input, policy) -> None:
    ledger = _ledger_with_proposal(tmp_path / "missing-nav.sqlite3", with_nav=False)
    try:
        with pytest.raises(LedgerConflictError, match="NAV snapshot"):
            review_and_persist(ledger, proposal_id="prop-1", reviewed_at=risk_input.decision_at,
                               request=risk_input, policy=policy)
        assert ledger.conn.execute("SELECT COUNT(*) FROM paper_risk_reviews").fetchone()[0] == 0
    finally:
        ledger.close()


def test_integrated_ignores_caller_cash_nav_counts_and_uses_ledger(tmp_path, risk_input, policy) -> None:
    ledger = _ledger_with_proposal(tmp_path / "caller-lies.sqlite3")
    lied = replace(risk_input, cash_krw=100_000_000, aggregate_position_value_krw=0,
        nav_krw=100_000_000, high_water_nav_krw=100_000_000,
        session_start_nav_krw=100_000_000, daily_order_count=0, daily_fill_count=0)
    try:
        review_and_persist(ledger, proposal_id="prop-1", reviewed_at=risk_input.decision_at,
                           request=lied, policy=policy)
        row = ledger.conn.execute("SELECT verdict, approved_quantity FROM paper_risk_reviews").fetchone()
        assert tuple(row) == ("APPROVE", 28)
    finally:
        ledger.close()


def test_integrated_exit_without_position_returns_noop_and_writes_no_row(tmp_path, risk_input, policy) -> None:
    ledger = _ledger_with_proposal(
        tmp_path / "noop.sqlite3", side="SELL", target_exposure_bp=0,
    )
    request = replace(risk_input, action="EXIT", target_exposure_bps=0)
    try:
        assert review_and_persist(ledger, proposal_id="prop-1",
            reviewed_at=risk_input.decision_at, request=request, policy=policy) is None
        assert ledger.conn.execute("SELECT COUNT(*) FROM paper_risk_reviews").fetchone()[0] == 0
    finally:
        ledger.close()


def test_persisted_reduce_cannot_be_reviewed_as_exit(tmp_path, risk_input, policy) -> None:
    ledger = _ledger_with_proposal(tmp_path / "reduce-lineage.sqlite3", side="SELL",
                                   action="REDUCE", target_exposure_bp=2_000)
    try:
        with pytest.raises(LedgerConflictError, match="decision action"):
            review_and_persist(ledger, proposal_id="prop-1", reviewed_at=risk_input.decision_at,
                               request=replace(risk_input, action="EXIT", target_exposure_bps=2_000),
                               policy=policy)
        assert ledger.conn.execute("SELECT count(*) FROM paper_risk_reviews").fetchone()[0] == 0
    finally:
        ledger.close()


def test_pending_approval_reserves_daily_order_capacity_across_connections(
    tmp_path, risk_input, policy,
) -> None:
    path = tmp_path / "reservations.sqlite3"
    first = _ledger_with_proposal(path)
    second = PaperLedger(path)
    second_snapshot = _feature_snapshot(symbol="000660")
    second_digest = hashlib.sha256(second_snapshot.stable_json().encode()).hexdigest()
    try:
        first.append_agent_decision(
            decision_id="dec-2", account_id="acct-1", symbol="000660", ksf_run_id="run-2",
            ksf_decision_id="ksf-dec-2", feature_snapshot_sha256=second_digest,
            available_data_cutoff=risk_input.available_data_cutoff, action="ENTER",
            reason_code="FLOW_SIGNAL_POSITIVE", rationale_sha256="d" * 64,
            created_at=risk_input.decision_at)
        first.append_order_proposal(
            proposal_id="prop-2", decision_id="dec-2", side="BUY", target_exposure_bp=2_000,
            idempotency_key="prop-key-2", model_name="model", model_version="v1",
            proposed_at=risk_input.decision_at)
        one_cap = replace(policy, daily_order_cap=1)
        first_id = review_and_persist(first, proposal_id="prop-1", reviewed_at=risk_input.decision_at,
                                      request=risk_input, policy=one_cap)
        second_request = replace(
            risk_input, symbol="000660", ksf_run_id="run-2", ksf_decision_id="ksf-dec-2",
            feature_snapshot=second_snapshot, feature_snapshot_sha256=second_digest)
        second_id = review_and_persist(second, proposal_id="prop-2", reviewed_at=risk_input.decision_at,
                                       request=second_request, policy=one_cap)
        rows = first.conn.execute(
            "SELECT review_id, verdict, reject_reason_code FROM paper_risk_reviews ORDER BY review_id"
        ).fetchall()
        by_id = {row[0]: (row[1], row[2]) for row in rows}
        assert by_id[first_id][0] in {"APPROVE", "RESIZE"}
        assert by_id[second_id] == ("REJECT", "DAILY_FILL_LIMIT")
    finally:
        second.close()
        first.close()


def test_pending_buy_reserves_cash_and_aggregate_exposure_for_second_symbol(
    tmp_path, risk_input, policy,
) -> None:
    path = tmp_path / "cash-reservations.sqlite3"
    first = _ledger_with_proposal(path, target_exposure_bp=6_000)
    second = PaperLedger(path)
    snapshot = _feature_snapshot(symbol="000660")
    digest = hashlib.sha256(snapshot.stable_json().encode()).hexdigest()
    try:
        first.append_agent_decision(
            decision_id="dec-2", account_id="acct-1", symbol="000660", ksf_run_id="run-2",
            ksf_decision_id="ksf-dec-2", feature_snapshot_sha256=digest,
            available_data_cutoff=risk_input.available_data_cutoff, action="ENTER",
            reason_code="FLOW_SIGNAL_POSITIVE", rationale_sha256="e" * 64,
            created_at=risk_input.decision_at)
        first.append_order_proposal(
            proposal_id="prop-2", decision_id="dec-2", side="BUY", target_exposure_bp=6_000,
            idempotency_key="prop-key-2", model_name="model", model_version="v1",
            proposed_at=risk_input.decision_at)
        roomy = replace(policy, max_position_bps=10_000, max_aggregate_bps=10_000)
        first_request = replace(risk_input, target_exposure_bps=6_000)
        first_id = review_and_persist(first, proposal_id="prop-1", reviewed_at=risk_input.decision_at,
                                      request=first_request, policy=roomy)
        second_request = replace(
            first_request, symbol="000660", ksf_run_id="run-2", ksf_decision_id="ksf-dec-2",
            feature_snapshot=snapshot, feature_snapshot_sha256=digest)
        second_id = review_and_persist(second, proposal_id="prop-2", reviewed_at=risk_input.decision_at,
                                       request=second_request, policy=roomy)
        assert tuple(first.conn.execute(
            "SELECT verdict,approved_quantity FROM paper_risk_reviews WHERE review_id=?", (first_id,)
        ).fetchone()) == ("APPROVE", 85)
        assert tuple(first.conn.execute(
            "SELECT verdict,approved_quantity FROM paper_risk_reviews WHERE review_id=?", (second_id,)
        ).fetchone()) == ("RESIZE", 57)
    finally:
        second.close()
        first.close()


def test_integrated_exit_target_zero_uses_positive_ledger_holdings(tmp_path, risk_input, policy) -> None:
    ledger = _ledger_with_proposal(tmp_path / "exit.sqlite3", with_nav=False,
                                   side="SELL", target_exposure_bp=0)
    # The production schema permits position events only through fill triggers; this
    # fresh test ledger injects the resulting event directly to isolate risk replay.
    ledger.conn.execute("DROP TRIGGER trg_paper_position_events_match_fill")
    ledger.conn.execute("PRAGMA foreign_keys=OFF")
    ledger.conn.execute(
        "INSERT INTO paper_position_events VALUES (?,?,?,?,?,?)",
        ("pos-existing", "acct-1", "005930", "historical-fill", 10,
         "2026-08-07T15:30:00+09:00"))
    ledger.conn.execute("PRAGMA foreign_keys=ON")
    ledger.append_nav_snapshot(nav_snapshot_id="nav-current", account_id="acct-1",
        session_date="2026-08-08", cash_krw=10_000_000, position_value_krw=700_000,
        position_marks={"005930": _mark(risk_input.feature_snapshot, 10, 70_000)},
        nav_krw=10_700_000, snapshot_at="2026-08-08T09:59:30+09:00")
    request = replace(risk_input, action="EXIT", target_exposure_bps=0)
    try:
        review_id = review_and_persist(ledger, proposal_id="prop-1",
            reviewed_at=risk_input.decision_at, request=request, policy=policy)
        row = ledger.conn.execute(
            "SELECT verdict, approved_quantity FROM paper_risk_reviews WHERE review_id=?", (review_id,)
        ).fetchone()
        assert tuple(row) == ("APPROVE", 10)
    finally:
        ledger.close()


def test_integrated_current_held_mark_must_equal_canonical_close(tmp_path, risk_input, policy) -> None:
    ledger = _ledger_with_proposal(tmp_path / "wrong-held-mark.sqlite3", with_nav=False,
                                   side="SELL", target_exposure_bp=0)
    ledger.conn.execute("DROP TRIGGER trg_paper_position_events_match_fill")
    ledger.conn.execute("PRAGMA foreign_keys=OFF")
    ledger.conn.execute("INSERT INTO paper_position_events VALUES (?,?,?,?,?,?)",
        ("pos-existing", "acct-1", "005930", "historical-fill", 10,
         "2026-08-07T15:30:00+09:00"))
    ledger.conn.execute("PRAGMA foreign_keys=ON")
    ledger.append_nav_snapshot(nav_snapshot_id="nav-current", account_id="acct-1",
        session_date="2026-08-08", cash_krw=10_000_000, position_value_krw=710_000,
        position_marks={"005930": _mark(risk_input.feature_snapshot, 10, 71_000)},
        nav_krw=10_710_000, snapshot_at="2026-08-08T09:59:30+09:00")
    try:
        with pytest.raises(LedgerConflictError, match="canonical close"):
            review_and_persist(ledger, proposal_id="prop-1", reviewed_at=risk_input.decision_at,
                request=replace(risk_input, action="EXIT", target_exposure_bps=0), policy=policy)
    finally:
        ledger.close()


def test_integrated_future_dated_historical_snapshot_fails_closed(tmp_path, risk_input, policy) -> None:
    ledger = _ledger_with_proposal(tmp_path / "future-history.sqlite3")
    ledger.conn.execute(
        """INSERT INTO paper_nav_snapshots
           (nav_snapshot_id,account_id,session_date,cash_krw,position_value_krw,
            position_marks_json,nav_krw,snapshot_at) VALUES (?,?,?,?,?,?,?,?)""",
        ("future-history", "acct-1", "2026-08-07", 10_000_000, 0, "{}", 10_000_000,
         "2026-08-09T10:00:00+09:00"))
    try:
        with pytest.raises(LedgerConflictError, match="future-dated"):
            review_and_persist(ledger, proposal_id="prop-1", reviewed_at=risk_input.decision_at,
                               request=risk_input, policy=policy)
    finally:
        ledger.close()


def test_integrated_multi_symbol_holdings_require_every_mark(tmp_path, risk_input, policy) -> None:
    ledger = _ledger_with_proposal(tmp_path / "missing-mark.sqlite3", with_nav=False)
    ledger.conn.execute("DROP TRIGGER trg_paper_position_events_match_fill")
    ledger.conn.execute("PRAGMA foreign_keys=OFF")
    ledger.conn.executemany("INSERT INTO paper_position_events VALUES (?,?,?,?,?,?)", [
        ("pos-a", "acct-1", "005930", "fill-a", 10, "2026-08-07T15:30:00+09:00"),
        ("pos-b", "acct-1", "000660", "fill-b", 5, "2026-08-07T15:30:00+09:00"),
    ])
    ledger.conn.execute("PRAGMA foreign_keys=ON")
    ledger.conn.execute(
        """INSERT INTO paper_nav_snapshots
           (nav_snapshot_id,account_id,session_date,cash_krw,position_value_krw,
            position_marks_json,nav_krw,snapshot_at) VALUES (?,?,?,?,?,?,?,?)""",
        ("missing-mark", "acct-1", "2026-08-08", 10_000_000, 700_000,
         json.dumps({"005930": _mark(risk_input.feature_snapshot, 10, 70_000)},
                    sort_keys=True, separators=(",", ":")), 10_700_000,
         "2026-08-08T09:59:30+09:00"))
    try:
        with pytest.raises(LedgerConflictError, match="mismatch holdings"):
            review_and_persist(ledger, proposal_id="prop-1", reviewed_at=risk_input.decision_at,
                               request=risk_input, policy=policy)
    finally:
        ledger.close()


def test_integrated_inconsistent_nav_cash_fails_closed(tmp_path, risk_input, policy) -> None:
    ledger = _ledger_with_proposal(tmp_path / "bad-nav.sqlite3", with_nav=False)
    ledger.conn.execute("PRAGMA ignore_check_constraints=ON")
    ledger.conn.execute(
        "INSERT INTO paper_nav_snapshots VALUES (?,?,?,?,?,?,?,?)",
        ("bad-nav", "acct-1", "2026-08-08", 9_999_999, 0, "{}", 9_999_999,
         "2026-08-08T09:59:30+09:00"))
    ledger.conn.execute("PRAGMA ignore_check_constraints=OFF")
    try:
        with pytest.raises(LedgerConflictError, match="inconsistent"):
            review_and_persist(ledger, proposal_id="prop-1", reviewed_at=risk_input.decision_at,
                               request=risk_input, policy=policy)
        assert ledger.conn.execute("SELECT COUNT(*) FROM paper_risk_reviews").fetchone()[0] == 0
    finally:
        ledger.close()


def test_integrated_duplicate_is_authoritative_and_session_is_decision_date(tmp_path, risk_input, policy) -> None:
    ledger = _ledger_with_proposal(tmp_path / "duplicate.sqlite3")
    try:
        first_id = review_and_persist(ledger, proposal_id="prop-1",
            reviewed_at=risk_input.decision_at, request=risk_input, policy=policy)
        assert review_and_persist(ledger, proposal_id="prop-1",
            reviewed_at=risk_input.decision_at, request=risk_input, policy=policy) == first_id
        ledger.append_agent_decision(
            decision_id="dec-2", account_id="acct-1", symbol="005930", ksf_run_id="run-2",
            ksf_decision_id="ksf-dec-2", feature_snapshot_sha256=risk_input.feature_snapshot_sha256,
            available_data_cutoff=risk_input.available_data_cutoff, action="ENTER",
            reason_code="FLOW_SIGNAL_POSITIVE", rationale_sha256="c" * 64,
            created_at=risk_input.decision_at,
        )
        ledger.append_order_proposal(
            proposal_id="prop-2", decision_id="dec-2", side="BUY",
            target_exposure_bp=2_000, idempotency_key="prop-key-2", model_name="model",
            model_version="v1", proposed_at=risk_input.decision_at,
        )
        second = replace(risk_input, ksf_run_id="run-2", ksf_decision_id="ksf-dec-2")
        second_id = review_and_persist(ledger, proposal_id="prop-2",
            reviewed_at=risk_input.decision_at, request=second, policy=policy)
        row = dict(ledger.conn.execute("SELECT * FROM paper_risk_reviews WHERE review_id=?", (second_id,)).fetchone())
        assert (row["verdict"], row["reject_reason_code"]) == ("REJECT", "DUPLICATE_SESSION")
        with pytest.raises(ValueError, match="session_id"):
            review_and_persist(ledger, proposal_id="prop-2",
                reviewed_at=risk_input.decision_at,
                request=replace(second, session_id="2026-08-07"), policy=policy)
    finally:
        ledger.close()


@pytest.mark.parametrize(("changes", "field"), [
    ({"account_id": "acct-other"}, "account_id"),
    ({"symbol": "000660"}, "symbol"),
    ({"action": "REDUCE"}, "side"),
    ({"target_exposure_bps": 1_999}, "target_exposure_bp"),
    ({"ksf_run_id": "run-other"}, "ksf_run_id"),
    ({"ksf_decision_id": "ksf-dec-other"}, "ksf_decision_id"),
    ({"feature_snapshot_sha256": "d" * 64}, "feature_snapshot_sha256"),
    ({"available_data_cutoff": "2026-08-08T09:58:59+09:00"}, "available_data_cutoff"),
])
def test_integrated_boundary_binds_every_proposal_and_lineage_field(
    tmp_path, risk_input, policy, changes, field,
) -> None:
    ledger = _ledger_with_proposal(tmp_path / f"bind-{field}.sqlite3")
    try:
        with pytest.raises(ValueError, match=field):
            review_and_persist(ledger, proposal_id="prop-1",
                reviewed_at=risk_input.decision_at,
                request=replace(risk_input, **changes), policy=policy)
        assert ledger.conn.execute("SELECT COUNT(*) FROM paper_risk_reviews").fetchone()[0] == 0
    finally:
        ledger.close()


def test_persisted_approve_reject_replay_conflict_and_noop(tmp_path, risk_input, policy) -> None:
    ledger = _ledger_with_proposal(tmp_path / "paper.sqlite3")
    try:
        first_id = review_and_persist(ledger, proposal_id="prop-1",
            reviewed_at=risk_input.decision_at, request=risk_input, policy=policy)
        row = dict(ledger.conn.execute("SELECT * FROM paper_risk_reviews").fetchone())
        assert row["verdict"] == "APPROVE" and row["approved_quantity"] == 28
        assert row["reject_reason_code"] is None and row["policy_sha256"] == policy.sha256
        assert review_and_persist(ledger, proposal_id="prop-1",
            reviewed_at=risk_input.decision_at, request=risk_input, policy=policy) == first_id
        with pytest.raises(LedgerConflictError):
            review_and_persist(ledger, proposal_id="prop-1",
                reviewed_at=risk_input.decision_at,
                request=replace(risk_input, trading_enabled=False), policy=policy)
        with pytest.raises(LedgerConflictError):
            review_and_persist(ledger, proposal_id="prop-1",
                reviewed_at=risk_input.decision_at, request=risk_input,
                policy=replace(policy, min_notional_krw=20_000))
        assert ledger.conn.execute("SELECT COUNT(*) FROM paper_risk_reviews").fetchone()[0] == 1
    finally:
        ledger.close()


@pytest.mark.parametrize("changes", [
    {"price_krw": 70_001},
    {"daily_order_count": 1},
])
def test_replay_fingerprint_rejects_request_changes_even_when_result_is_unchanged(
    tmp_path, risk_input, policy, changes,
) -> None:
    ledger = _ledger_with_proposal(tmp_path / "request-replay.sqlite3")
    try:
        review_and_persist(ledger, proposal_id="prop-1", reviewed_at=risk_input.decision_at,
                           request=risk_input, policy=policy)
        with pytest.raises(LedgerConflictError, match="different risk review"):
            review_and_persist(ledger, proposal_id="prop-1", reviewed_at=risk_input.decision_at,
                               request=replace(risk_input, **changes), policy=policy)
        assert ledger.conn.execute("SELECT count(*) FROM paper_risk_reviews").fetchone()[0] == 1
    finally:
        ledger.close()


def test_replay_fingerprint_rejects_later_review_time_and_operational_change(
    tmp_path, risk_input, policy,
) -> None:
    ledger = _ledger_with_proposal(tmp_path / "state-replay.sqlite3")
    try:
        first = review_and_persist(ledger, proposal_id="prop-1", reviewed_at=risk_input.decision_at,
                                   request=risk_input, policy=policy)
        assert review_and_persist(ledger, proposal_id="prop-1", reviewed_at=risk_input.decision_at,
                                  request=risk_input, policy=policy) == first
        with pytest.raises(LedgerConflictError, match="different risk review"):
            review_and_persist(ledger, proposal_id="prop-1",
                reviewed_at="2026-08-08T10:00:01+09:00", request=risk_input, policy=policy)
        ledger.append_kill_switch_event(kill_switch_event_id="engage-after-review",
            account_id="acct-1", event_type="ENGAGED", reason_code="MANUAL",
            event_at="2026-08-08T10:00:01+09:00")
        assert review_and_persist(
            ledger, proposal_id="prop-1", reviewed_at=risk_input.decision_at,
            request=risk_input, policy=policy,
        ) == first
        assert ledger.conn.execute("SELECT count(*) FROM paper_risk_reviews").fetchone()[0] == 1
    finally:
        ledger.close()


def test_snapshot_lineage_change_is_rejected_before_replay_identity(
    tmp_path, risk_input, policy,
) -> None:
    ledger = _ledger_with_proposal(tmp_path / "lineage-replay.sqlite3")
    changed = _feature_snapshot(overrides={"volume": (1001.0, "shares", "domestic_price", "READY")})
    try:
        review_and_persist(ledger, proposal_id="prop-1", reviewed_at=risk_input.decision_at,
                           request=risk_input, policy=policy)
        with pytest.raises(ValueError, match="feature_snapshot_sha256"):
            review_and_persist(ledger, proposal_id="prop-1", reviewed_at=risk_input.decision_at,
                request=replace(risk_input, feature_snapshot=changed,
                    feature_snapshot_sha256=hashlib.sha256(changed.stable_json().encode()).hexdigest()),
                policy=policy)
        assert ledger.conn.execute("SELECT count(*) FROM paper_risk_reviews").fetchone()[0] == 1
    finally:
        ledger.close()


def test_reject_persisted_shape_and_reason_allowlist(tmp_path, risk_input, policy) -> None:
    ledger = _ledger_with_proposal(tmp_path / "paper.sqlite3", operational=False)
    try:
        rejected = evaluate_risk(replace(risk_input, trading_enabled=False), policy)
        review_and_persist(ledger, proposal_id="prop-1",
                            reviewed_at=risk_input.decision_at,
                            request=risk_input, policy=policy)
        row = dict(ledger.conn.execute("SELECT * FROM paper_risk_reviews").fetchone())
        assert row["verdict"] == "REJECT" and row["approved_quantity"] is None
        assert row["reject_reason_code"] == "TRADING_DISABLED"
        assert rejected.reject_reason_code in RISK_REJECT_REASON_CODES
    finally:
        ledger.close()


def test_resize_persisted_shape(tmp_path, risk_input, policy) -> None:
    ledger = _ledger_with_proposal(tmp_path / "paper.sqlite3")
    try:
        review_and_persist(ledger, proposal_id="prop-1",
                            reviewed_at=risk_input.decision_at, request=risk_input, policy=policy)
        row = dict(ledger.conn.execute("SELECT * FROM paper_risk_reviews").fetchone())
        assert row["verdict"] == "APPROVE" and row["approved_quantity"] == 28
        assert row["reject_reason_code"] is None
    finally:
        ledger.close()


def test_persisted_quantity_is_owned_by_engine(tmp_path, risk_input, policy) -> None:
    ledger = _ledger_with_proposal(tmp_path / "risk-owned.sqlite3", target_exposure_bp=4_000)
    request = replace(risk_input, target_exposure_bps=4_000)
    try:
        review_and_persist(ledger, proposal_id="prop-1", reviewed_at=request.decision_at,
                           request=request, policy=policy)
        row = ledger.conn.execute("SELECT verdict, approved_quantity FROM paper_risk_reviews").fetchone()
        assert tuple(row) == ("RESIZE", 35)
    finally:
        ledger.close()


def test_engine_quantity_is_not_constrained_by_agent_proposal(tmp_path, risk_input, policy) -> None:
    ledger = _ledger_with_proposal(tmp_path / "risk-owned.sqlite3", target_exposure_bp=4_000)
    try:
        review_and_persist(ledger, proposal_id="prop-1", reviewed_at=risk_input.decision_at,
                           request=replace(risk_input, target_exposure_bps=4_000), policy=policy)
        assert ledger.conn.execute("SELECT approved_quantity FROM paper_risk_reviews").fetchone()[0] == 35
    finally:
        ledger.close()


@pytest.mark.parametrize("bad", [True, 1, "", type("S", (str,), {})("prop-1")])
def test_integrated_proposal_id_is_exact_nonempty_plain_string(tmp_path, risk_input, policy, bad) -> None:
    ledger = _ledger_with_proposal(tmp_path / f"bad-id-{type(bad).__name__}.sqlite3")
    try:
        with pytest.raises(ValueError, match="proposal_id"):
            review_and_persist(ledger, proposal_id=bad, reviewed_at=risk_input.decision_at,
                               request=risk_input, policy=policy)
    finally:
        ledger.close()


@pytest.mark.parametrize("bad", [
    "2026-08-08T10:00:00", "2026-08-08T01:00:00+00:00",
    "2026-08-08T09:59:59+09:00",
])
def test_reviewed_at_is_exact_kst_and_causal(tmp_path, risk_input, policy, bad) -> None:
    ledger = _ledger_with_proposal(tmp_path / (hashlib.sha256(bad.encode()).hexdigest() + ".sqlite3"))
    try:
        with pytest.raises(ValueError):
            review_and_persist(ledger, proposal_id="prop-1", reviewed_at=bad,
                               request=risk_input, policy=policy)
        assert ledger.conn.execute("SELECT count(*) FROM paper_risk_reviews").fetchone()[0] == 0
    finally:
        ledger.close()


def test_exit_remains_subject_to_literal_daily_loss_precedence(risk_input, policy) -> None:
    request = replace(risk_input, action="EXIT", target_exposure_bps=0,
                      holdings_quantity=10, aggregate_position_value_krw=700_000,
                      cash_krw=8_800_000, nav_krw=9_500_000)
    assert evaluate_risk(request, policy).reject_reason_code == "DAILY_LOSS_STOP"


def test_real_ledger_defaults_are_fail_closed(tmp_path, risk_input, policy) -> None:
    ledger = PaperLedger(tmp_path / "paper.sqlite3")
    try:
        ledger.create_account(account_id="acct-1", initial_cash_krw=10_000_000,
                              policy_version="v1", created_at="2026-08-08T08:00:00+09:00")
        assert ledger.account_enabled("acct-1") is False
        assert ledger.kill_switch_engaged("acct-1") is True
        result = evaluate_risk(replace(risk_input, trading_enabled=ledger.account_enabled("acct-1"),
            kill_switch_engaged=ledger.kill_switch_engaged("acct-1")), policy)
        assert result.reject_reason_code == "TRADING_DISABLED"
    finally:
        ledger.close()


def _inject_malformed_event(ledger: PaperLedger, table: str, *, event_id: str) -> None:
    id_column = "account_event_id" if table == "paper_account_events" else "kill_switch_event_id"
    ledger.conn.execute("PRAGMA ignore_check_constraints = ON")
    ledger.conn.execute(
        f"INSERT INTO {table} ({id_column}, account_id, seq, event_type, reason_code, event_at) "
        f"SELECT ?, 'acct-1', COALESCE(MAX(seq), 0) + 1, 'MALFORMED', 'MANUAL', "
        f"'2026-08-08T09:00:00+09:00' FROM {table} WHERE account_id='acct-1'",
        (event_id,),
    )
    ledger.conn.execute("PRAGMA ignore_check_constraints = OFF")
    assert ledger.conn.execute(
        f"SELECT event_type FROM {table} WHERE {id_column}=?", (event_id,)
    ).fetchone()[0] == "MALFORMED"


@pytest.mark.parametrize("table", ["paper_account_events", "paper_kill_switch_events"])
def test_malformed_latest_authoritative_event_conflicts_without_review(
    tmp_path, risk_input, policy, table,
) -> None:
    ledger = _ledger_with_proposal(tmp_path / f"corrupt-{table}.sqlite3")
    try:
        _inject_malformed_event(ledger, table, event_id="corrupt-latest")
        with pytest.raises(LedgerConflictError, match="malformed"):
            review_and_persist(ledger, proposal_id="prop-1", reviewed_at=risk_input.decision_at,
                               request=risk_input, policy=policy)
        assert ledger.conn.execute("SELECT count(*) FROM paper_risk_reviews").fetchone()[0] == 0
    finally:
        ledger.close()


def test_disabled_account_precedes_malformed_kill_state(tmp_path, risk_input, policy) -> None:
    ledger = _ledger_with_proposal(tmp_path / "disabled-precedence.sqlite3")
    try:
        ledger.append_account_event(account_event_id="disable-latest", account_id="acct-1",
            event_type="DISABLED", reason_code="MANUAL", event_at="2026-08-08T09:00:00+09:00")
        _inject_malformed_event(ledger, "paper_kill_switch_events", event_id="corrupt-kill")
        review_and_persist(ledger, proposal_id="prop-1", reviewed_at=risk_input.decision_at,
                           request=risk_input, policy=policy)
        row = ledger.conn.execute(
            "SELECT verdict, reject_reason_code FROM paper_risk_reviews"
        ).fetchone()
        assert tuple(row) == ("REJECT", "TRADING_DISABLED")
    finally:
        ledger.close()


def test_future_enabled_and_released_cannot_authorize_past_review(
    tmp_path, risk_input, policy,
) -> None:
    ledger = _ledger_with_proposal(tmp_path / "future-state.sqlite3", operational=False)
    try:
        ledger.append_account_event(account_event_id="future-enable", account_id="acct-1",
            event_type="ENABLED", reason_code="MANUAL", event_at="2026-08-08T11:00:00+09:00")
        ledger.append_kill_switch_event(kill_switch_event_id="future-release", account_id="acct-1",
            event_type="RELEASED", reason_code="MANUAL", event_at="2026-08-08T11:01:00+09:00")
        review_and_persist(ledger, proposal_id="prop-1", reviewed_at=risk_input.decision_at,
                           request=risk_input, policy=policy)
        assert tuple(ledger.conn.execute(
            "SELECT verdict,reject_reason_code FROM paper_risk_reviews"
        ).fetchone()) == ("REJECT", "TRADING_DISABLED")
    finally:
        ledger.close()


def test_past_enabled_future_release_leaves_kill_active(
    tmp_path, risk_input, policy,
) -> None:
    ledger = _ledger_with_proposal(tmp_path / "future-release.sqlite3", operational=False)
    try:
        ledger.append_account_event(account_event_id="past-enable", account_id="acct-1",
            event_type="ENABLED", reason_code="MANUAL", event_at="2026-08-08T08:01:00+09:00")
        ledger.append_kill_switch_event(kill_switch_event_id="past-engage", account_id="acct-1",
            event_type="ENGAGED", reason_code="MANUAL", event_at="2026-08-08T08:02:00+09:00")
        ledger.append_kill_switch_event(kill_switch_event_id="future-release", account_id="acct-1",
            event_type="RELEASED", reason_code="MANUAL", event_at="2026-08-08T11:00:00+09:00")
        review_and_persist(ledger, proposal_id="prop-1", reviewed_at=risk_input.decision_at,
                           request=risk_input, policy=policy)
        assert tuple(ledger.conn.execute(
            "SELECT verdict,reject_reason_code FROM paper_risk_reviews"
        ).fetchone()) == ("REJECT", "KILL_SWITCH_ACTIVE")
    finally:
        ledger.close()


@pytest.mark.parametrize(("table", "id_column"), [
    ("paper_account_events", "account_event_id"),
    ("paper_kill_switch_events", "kill_switch_event_id"),
])
@pytest.mark.parametrize("bad_time", ["2026-08-08T01:00:00+00:00", "2026-08-08T10:00:00"])
def test_malformed_authoritative_state_timestamp_conflicts_without_review(
    tmp_path, risk_input, policy, table, id_column, bad_time,
) -> None:
    ledger = _ledger_with_proposal(tmp_path / f"bad-time-{table}-{bad_time[-1]}.sqlite3")
    try:
        ledger.conn.execute("PRAGMA ignore_check_constraints=ON")
        ledger.conn.execute(f"DROP TRIGGER trg_{table}_causal_time")
        event_type = "ENABLED" if table == "paper_account_events" else "RELEASED"
        ledger.conn.execute(
            f"INSERT INTO {table} ({id_column},account_id,seq,event_type,reason_code,event_at) "
            f"SELECT 'bad-time','acct-1',MAX(seq)+1,?,'MANUAL',? FROM {table} "
            "WHERE account_id='acct-1'", (event_type, bad_time))
        ledger.conn.execute("PRAGMA ignore_check_constraints=OFF")
        with pytest.raises(LedgerConflictError, match="malformed"):
            review_and_persist(ledger, proposal_id="prop-1", reviewed_at=risk_input.decision_at,
                               request=risk_input, policy=policy)
        assert ledger.conn.execute("SELECT count(*) FROM paper_risk_reviews").fetchone()[0] == 0
    finally:
        ledger.close()


def _insert_raw_nav(ledger: PaperLedger, *, nav_id: str, session_date: str,
                    cash: int, position_value: int, marks: dict, snapshot_at: str) -> None:
    ledger.conn.execute(
        "INSERT INTO paper_nav_snapshots VALUES (?,?,?,?,?,?,?,?)",
        (nav_id, "acct-1", session_date, cash, position_value,
         json.dumps(marks, sort_keys=True, separators=(",", ":")),
         cash + position_value, snapshot_at),
    )


def test_historical_nav_cash_mismatch_conflicts_without_review(tmp_path, risk_input, policy) -> None:
    ledger = _ledger_with_proposal(tmp_path / "historical-cash.sqlite3")
    try:
        _insert_raw_nav(ledger, nav_id="forged", session_date="2026-08-07",
                        cash=9_000_000, position_value=0, marks={},
                        snapshot_at="2026-08-07T16:00:00+09:00")
        with pytest.raises(LedgerConflictError, match="historical NAV"):
            review_and_persist(ledger, proposal_id="prop-1", reviewed_at=risk_input.decision_at,
                               request=risk_input, policy=policy)
        assert ledger.conn.execute("SELECT count(*) FROM paper_risk_reviews").fetchone()[0] == 0
    finally:
        ledger.close()


def test_historical_nav_symbol_quantity_mismatch_conflicts_without_review(
    tmp_path, risk_input, policy,
) -> None:
    ledger = _ledger_with_proposal(tmp_path / "historical-position.sqlite3")
    try:
        forged_mark = _mark(risk_input.feature_snapshot, 1, 70_000)
        forged_mark["observed_at"] = "2026-08-07T15:59:00+09:00"
        _insert_raw_nav(ledger, nav_id="forged", session_date="2026-08-07",
                        cash=9_930_000, position_value=70_000,
                        marks={"005930": forged_mark},
                        snapshot_at="2026-08-07T16:00:00+09:00")
        with pytest.raises(LedgerConflictError, match="historical NAV"):
            review_and_persist(ledger, proposal_id="prop-1", reviewed_at=risk_input.decision_at,
                               request=risk_input, policy=policy)
        assert ledger.conn.execute("SELECT count(*) FROM paper_risk_reviews").fetchone()[0] == 0
    finally:
        ledger.close()


def test_forged_low_prior_nav_cannot_bypass_daily_loss_stop(tmp_path, risk_input, policy) -> None:
    ledger = _ledger_with_proposal(tmp_path / "historical-loss-bypass.sqlite3", with_nav=False)
    try:
        _insert_raw_nav(ledger, nav_id="forged-prior", session_date="2026-08-07",
                        cash=9_000_000, position_value=0, marks={},
                        snapshot_at="2026-08-07T16:00:00+09:00")
        _insert_raw_nav(ledger, nav_id="current", session_date="2026-08-08",
                        cash=10_000_000, position_value=0, marks={},
                        snapshot_at="2026-08-08T09:59:30+09:00")
        with pytest.raises(LedgerConflictError, match="historical NAV"):
            review_and_persist(ledger, proposal_id="prop-1", reviewed_at=risk_input.decision_at,
                               request=risk_input, policy=policy)
        assert ledger.conn.execute("SELECT count(*) FROM paper_risk_reviews").fetchone()[0] == 0
    finally:
        ledger.close()


def test_prior_session_start_uses_last_snapshot_by_timestamp_then_id(
    tmp_path, risk_input, policy,
) -> None:
    ledger = _ledger_with_proposal(tmp_path / "prior-session-order.sqlite3", with_nav=False,
                                   account_created_at="2026-08-07T08:00:00+09:00")
    try:
        ledger.conn.execute("DROP TRIGGER trg_paper_cash_events_match_fill")
        ledger.conn.execute("PRAGMA foreign_keys=OFF")
        ledger.conn.executemany(
            "INSERT INTO paper_cash_events VALUES (?,?,?,?,?,?)", [
                ("debit-1", "acct-1", "fake-1", "FEE", -1_000_000,
                 "2026-08-07T15:00:00+09:00"),
                ("credit-1", "acct-1", "fake-2", "FILL_CREDIT", 1_000_000,
                 "2026-08-07T15:30:00+09:00"),
                ("debit-2", "acct-1", "fake-3", "FEE", -500_000,
                 "2026-08-07T17:00:00+09:00"),
            ])
        ledger.conn.execute("PRAGMA foreign_keys=ON")
        _insert_raw_nav(ledger, nav_id="z-early", session_date="2026-08-07",
                        cash=9_000_000, position_value=0, marks={},
                        snapshot_at="2026-08-07T15:10:00+09:00")
        _insert_raw_nav(ledger, nav_id="a-late", session_date="2026-08-07",
                        cash=10_000_000, position_value=0, marks={},
                        snapshot_at="2026-08-07T15:40:00+09:00")
        _insert_raw_nav(ledger, nav_id="current", session_date="2026-08-08",
                        cash=9_500_000, position_value=0, marks={},
                        snapshot_at="2026-08-08T09:59:30+09:00")
        review_and_persist(ledger, proposal_id="prop-1", reviewed_at=risk_input.decision_at,
                           request=risk_input, policy=policy)
        assert tuple(ledger.conn.execute(
            "SELECT verdict,reject_reason_code FROM paper_risk_reviews"
        ).fetchone()) == ("REJECT", "DAILY_LOSS_STOP")
    finally:
        ledger.close()


def test_engaged_kill_precedes_duplicate_session(tmp_path, risk_input, policy) -> None:
    ledger = _ledger_with_proposal(tmp_path / "kill-precedence.sqlite3")
    try:
        review_and_persist(ledger, proposal_id="prop-1", reviewed_at=risk_input.decision_at,
                           request=risk_input, policy=policy)
        ledger.append_agent_decision(decision_id="dec-2", account_id="acct-1", symbol="005930",
            ksf_run_id="run-1", ksf_decision_id="ksf-dec-2",
            feature_snapshot_sha256=risk_input.feature_snapshot_sha256,
            available_data_cutoff=risk_input.available_data_cutoff, action="ENTER",
            reason_code="FLOW_SIGNAL_POSITIVE", rationale_sha256="c" * 64,
            created_at=risk_input.decision_at)
        ledger.append_order_proposal(proposal_id="prop-2", decision_id="dec-2", side="BUY",
            target_exposure_bp=2_000, idempotency_key="prop-key-2",
            model_name="model", model_version="v1", proposed_at=risk_input.decision_at)
        ledger.append_kill_switch_event(kill_switch_event_id="engage-latest", account_id="acct-1",
            event_type="ENGAGED", reason_code="MANUAL", event_at="2026-08-08T09:00:00+09:00")
        review_and_persist(ledger, proposal_id="prop-2", reviewed_at=risk_input.decision_at,
                           request=replace(risk_input, ksf_decision_id="ksf-dec-2"), policy=policy)
        row = ledger.conn.execute(
            "SELECT verdict, reject_reason_code FROM paper_risk_reviews WHERE proposal_id='prop-2'"
        ).fetchone()
        assert tuple(row) == ("REJECT", "KILL_SWITCH_ACTIVE")
    finally:
        ledger.close()


def test_closed_connection_select_failure_propagates_and_persists_nothing(
    tmp_path, risk_input, policy,
) -> None:
    path = tmp_path / "closed.sqlite3"
    ledger = _ledger_with_proposal(path)
    ledger.close()
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        review_and_persist(ledger, proposal_id="prop-1", reviewed_at=risk_input.decision_at,
                           request=risk_input, policy=policy)
    reopened = PaperLedger(path)
    try:
        assert reopened.conn.execute("SELECT count(*) FROM paper_risk_reviews").fetchone()[0] == 0
    finally:
        reopened.close()


def test_sqlite_immediate_transaction_serializes_duplicate_session_race(
    tmp_path, risk_input, policy,
) -> None:
    path = tmp_path / "race.sqlite3"
    setup = _ledger_with_proposal(path)
    setup.append_agent_decision(decision_id="dec-2", account_id="acct-1", symbol="005930",
        ksf_run_id="run-1", ksf_decision_id="ksf-dec-2",
        feature_snapshot_sha256=risk_input.feature_snapshot_sha256,
        available_data_cutoff=risk_input.available_data_cutoff, action="ENTER",
        reason_code="FLOW_SIGNAL_POSITIVE", rationale_sha256="c" * 64,
        created_at=risk_input.decision_at)
    setup.append_order_proposal(proposal_id="prop-2", decision_id="dec-2", side="BUY",
        target_exposure_bp=2_000, idempotency_key="prop-key-2",
        model_name="model", model_version="v1", proposed_at=risk_input.decision_at)
    setup.close()

    barrier = threading.Barrier(2)
    connection_init_lock = threading.Lock()
    outcomes = []
    outcome_lock = threading.Lock()

    def worker(proposal_id, decision_id):
        with connection_init_lock:
            ledger = PaperLedger(path)
        try:
            barrier.wait(timeout=5)
            review_id = review_and_persist(ledger, proposal_id=proposal_id,
                reviewed_at=risk_input.decision_at,
                request=replace(risk_input, ksf_decision_id=decision_id), policy=policy)
            with outcome_lock:
                outcomes.append(("ok", review_id))
        except BaseException as exc:
            with outcome_lock:
                outcomes.append(("error", exc))
        finally:
            ledger.close()

    threads = [threading.Thread(target=worker, args=("prop-1", "ksf-dec-1")),
               threading.Thread(target=worker, args=("prop-2", "ksf-dec-2"))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in threads)
    assert [kind for kind, _ in outcomes] == ["ok", "ok"]

    check = PaperLedger(path)
    try:
        rows = check.conn.execute(
            "SELECT verdict, reject_reason_code FROM paper_risk_reviews ORDER BY verdict"
        ).fetchall()
        assert sorted(map(tuple, rows)) == [("APPROVE", None), ("REJECT", "DUPLICATE_SESSION")]
    finally:
        check.close()


def test_committed_account_write_is_observed_before_review(tmp_path, risk_input, policy) -> None:
    path = tmp_path / "state-first.sqlite3"
    setup = _ledger_with_proposal(path)
    writer = PaperLedger(path)
    reviewer = PaperLedger(path)
    setup.close()
    try:
        writer.append_account_event(account_event_id="disable-first", account_id="acct-1",
            event_type="DISABLED", reason_code="MANUAL",
            event_at="2026-08-08T09:59:59+09:00")
        review_id = review_and_persist(reviewer, proposal_id="prop-1",
            reviewed_at=risk_input.decision_at, request=risk_input, policy=policy)
        row = reviewer.conn.execute(
            "SELECT verdict, reject_reason_code FROM paper_risk_reviews WHERE review_id=?", (review_id,)
        ).fetchone()
        assert tuple(row) == ("REJECT", "TRADING_DISABLED")
    finally:
        writer.close()
        reviewer.close()


def test_review_commit_precedes_later_kill_event(tmp_path, risk_input, policy) -> None:
    path = tmp_path / "review-first.sqlite3"
    setup = _ledger_with_proposal(path)
    writer = PaperLedger(path)
    reviewer = PaperLedger(path)
    setup.close()
    try:
        review_id = review_and_persist(reviewer, proposal_id="prop-1",
            reviewed_at=risk_input.decision_at, request=risk_input, policy=policy)
        writer.append_kill_switch_event(kill_switch_event_id="kill-after", account_id="acct-1",
            event_type="ENGAGED", reason_code="MANUAL",
            event_at="2026-08-08T10:00:01+09:00")
        assert reviewer.conn.execute(
            "SELECT verdict FROM paper_risk_reviews WHERE review_id=?", (review_id,)
        ).fetchone()[0] == "APPROVE"
        assert writer.conn.execute(
            "SELECT event_type FROM paper_kill_switch_events ORDER BY seq DESC LIMIT 1"
        ).fetchone()[0] == "ENGAGED"
    finally:
        writer.close()
        reviewer.close()
