from dataclasses import replace
import builtins
import hashlib
import importlib
import json
import math
import os
import socket
import sys

import pytest

from strategy.paper_evaluation import (
    DecisionRecord, EvaluationConfig, MarketOutcome, ShadowAuditInput,
    audit_shadow, evaluate_replay, shadow_evidence_sha256,
)


SHA_A = "a" * 64
SHA_B = "b" * 64


def decision(day, action="ENTER", symbol="005930", *, persisted_hour=16):
    return DecisionRecord(
        session_id=f"2026-07-{day:02d}", symbol=symbol, action=action,
        decided_at=f"2026-07-{day:02d}T15:30:00+09:00",
        persisted_at=f"2026-07-{day:02d}T{persisted_hour:02d}:00:00+09:00",
        available_data_cutoff=f"2026-07-{day:02d}T15:20:00+09:00",
        decision_sha256=SHA_A, source_artifact_sha256=SHA_B,
    )


def outcome(day, price, symbol="005930", *, hour=9):
    return MarketOutcome(
        session_id=f"2026-07-{day:02d}", symbol=symbol, price_krw=price,
        observed_at=f"2026-07-{day:02d}T{hour:02d}:00:00+09:00",
        source_artifact_sha256=SHA_B,
    )


def fixture():
    decisions = (decision(1, "ENTER"), decision(2, "HOLD"), decision(3, "EXIT"))
    observations = (outcome(1, 100), outcome(2, 110), outcome(3, 90), outcome(4, 120))
    return decisions, observations


def test_deterministic_replay_and_canonical_bytes_hash():
    decisions, observations = fixture()
    first = evaluate_replay(decisions, observations, EvaluationConfig(initial_cash_krw=1_000))
    second = evaluate_replay(tuple(reversed(decisions)), tuple(reversed(observations)),
                             EvaluationConfig(initial_cash_krw=1_000))
    assert first.canonical_json == second.canonical_json
    assert first.report_sha256 == second.report_sha256
    assert hashlib.sha256(first.canonical_json.encode()).hexdigest() == first.report_sha256
    assert first.strategies["deployed"].completed_sessions == 3


def test_next_session_only_and_same_close_never_fills():
    report = evaluate_replay((decision(1),), (outcome(1, 1), outcome(2, 100)),
                             EvaluationConfig(initial_cash_krw=1_000))
    deployed = report.strategies["deployed"]
    assert deployed.fill_count == 1
    assert deployed.fills[0].observation_session_id == "2026-07-02"
    assert deployed.fills[0].raw_price_krw == 100


@pytest.mark.parametrize("bad", [
    lambda: replace(decision(1), persisted_at="2026-07-01T16:00:00"),
    lambda: replace(decision(1), persisted_at="2026-07-01T16:00:00Z"),
    lambda: replace(decision(1), decision_sha256="A" * 64),
    lambda: replace(decision(1), source_artifact_sha256="x" * 64),
    lambda: replace(decision(1), action="BUY"),
    lambda: replace(decision(1), symbol="123456"),
    lambda: replace(decision(1), persisted_at="2026-07-01T15:00:00+09:00"),
    lambda: replace(decision(1), persisted_at="2026-07-02T10:00:00+09:00"),
])
def test_malformed_or_causally_invalid_decisions_fail_closed(bad):
    with pytest.raises(ValueError):
        evaluate_replay((bad(),), (outcome(2, 100),))


@pytest.mark.parametrize("price", [True, 0, -1, math.nan, math.inf, 1.5])
def test_malformed_prices_fail_closed(price):
    with pytest.raises(ValueError):
        evaluate_replay((decision(1),), (replace(outcome(2, 100), price_krw=price),))


@pytest.mark.parametrize("over", [
    {"initial_cash_krw": True}, {"initial_cash_krw": 0}, {"fee_bps": -1},
    {"sell_tax_bps": True}, {"slippage_bps": 10_001}, {"target_exposure_bps": -1},
])
def test_malformed_config_numbers_fail_closed(over):
    with pytest.raises(ValueError):
        EvaluationConfig(**over)


def test_duplicate_identity_and_cross_symbol_lineage_fail_closed():
    with pytest.raises(ValueError, match="duplicate"):
        evaluate_replay((decision(1), decision(1, "HOLD")), (outcome(2, 100),))
    with pytest.raises(ValueError, match="outcome|symbol"):
        evaluate_replay((decision(1),), (outcome(2, 100, "000660"),))


def test_future_and_same_observation_data_fail_closed():
    with pytest.raises(ValueError):
        evaluate_replay((decision(1),), (replace(outcome(2, 100), observed_at="2026-07-01T15:00:00+09:00"),))
    with pytest.raises(ValueError, match="strictly before"):
        evaluate_replay((decision(1),), (replace(outcome(2, 100), observed_at="2026-07-01T16:00:00+09:00"),))


def test_random_baseline_is_seeded_stable_and_order_independent():
    d, o = fixture()
    a = evaluate_replay(d, o, EvaluationConfig(random_seed="seed-one"))
    b = evaluate_replay(tuple(reversed(d)), tuple(reversed(o)), EvaluationConfig(random_seed="seed-one"))
    c = evaluate_replay(d, o, EvaluationConfig(random_seed="different"))
    assert a.strategies["random"].action_trace == b.strategies["random"].action_trace
    assert a.strategies["random"].action_trace != c.strategies["random"].action_trace


def test_costs_turnover_drawdown_and_no_negative_balances():
    d = (decision(1, "ENTER"), decision(2, "EXIT"), decision(3, "ENTER"))
    o = (outcome(2, 100), outcome(3, 80), outcome(4, 70))
    report = evaluate_replay(d, o, EvaluationConfig(
        initial_cash_krw=1_000, target_exposure_bps=10_000,
        fee_bps=100, sell_tax_bps=100, slippage_bps=100))
    s = report.strategies["deployed"]
    assert s.total_fees_krw > 0 and s.total_tax_krw > 0 and s.total_slippage_krw > 0
    assert s.turnover_bps > 0 and s.max_drawdown_bps > 0
    assert all(x.cash_krw >= 0 and x.position_quantity >= 0 for x in s.session_states)


def test_unaffordable_enter_is_no_partial_fill_and_never_borrows():
    r = evaluate_replay((decision(1),), (outcome(2, 101),), EvaluationConfig(
        initial_cash_krw=100, target_exposure_bps=10_000, fee_bps=100,
        sell_tax_bps=0, slippage_bps=0))
    s = r.strategies["deployed"]
    assert s.fill_count == 0 and s.ending_cash_krw == 100 and s.ending_position_quantity == 0


def test_baselines_share_schedule_costs_and_do_not_claim_probability_quality():
    d, o = fixture()
    r = evaluate_replay(d, o, EvaluationConfig())
    assert set(r.strategies) == {"deployed", "no_action", "random", "flow_momentum"}
    assert {s.completed_sessions for s in r.strategies.values()} == {3}
    assert r.probability_calibration_status == "NOT_EVALUATED"
    assert r.brier_status == "NOT_EVALUATED"
    assert r.reliability_status == "NOT_ESTABLISHED"
    assert "profit" not in r.canonical_json.lower()


def test_readiness_below_20_and_at_20_is_always_approval_gated():
    d, o = fixture()
    small = evaluate_replay(d, o, EvaluationConfig())
    assert not small.strategy_validity_ready
    assert small.user_approval_required
    days = tuple(range(1, 21))
    many_d = tuple(decision(day, "HOLD") for day in days)
    many_o = tuple(outcome(day + 1, 100 + day) for day in days)
    large = evaluate_replay(many_d, many_o, EvaluationConfig())
    assert large.completed_sessions == 20
    assert large.session_count_threshold_met
    assert not large.strategy_validity_ready
    assert large.strategy_validity_status == "NOT_ESTABLISHED"
    assert large.user_approval_required and not large.activation_performed


def test_completed_sessions_are_unique_observation_sessions_across_symbols():
    days = tuple(range(1, 11))
    decisions = tuple(
        decision(day, "HOLD", symbol)
        for day in days for symbol in ("005930", "000660")
    )
    observations = tuple(
        outcome(day + 1, 100 + day, symbol)
        for day in days for symbol in ("005930", "000660")
    )
    report = evaluate_replay(decisions, observations)
    assert report.completed_sessions == 10
    assert {result.completed_sessions for result in report.strategies.values()} == {10}
    assert not report.session_count_threshold_met


def test_all_strategies_execute_in_observation_chronology_and_are_order_stable():
    decisions = (decision(1, "ENTER", "005930"), decision(1, "ENTER", "000660"))
    observations = (outcome(5, 100, "005930"), outcome(2, 100, "000660"))
    config = EvaluationConfig(initial_cash_krw=1_000, target_exposure_bps=10_000,
                              fee_bps=0, slippage_bps=0)
    report = evaluate_replay(decisions, observations, config)
    reversed_report = evaluate_replay(tuple(reversed(decisions)), tuple(reversed(observations)), config)
    assert report.canonical_json == reversed_report.canonical_json
    for result in report.strategies.values():
        assert tuple(state.session_id for state in result.session_states) == (
            "2026-07-02", "2026-07-05")
    deployed = report.strategies["deployed"]
    assert deployed.session_states[0].nav_krw == 1_000
    assert tuple(fill.observation_session_id for fill in deployed.fills) == ("2026-07-02",)


def test_high_cash_low_price_enter_uses_exact_affordable_integer_size():
    report = evaluate_replay((decision(1),), (outcome(2, 1),), EvaluationConfig(
        initial_cash_krw=100_000_000, target_exposure_bps=10_000,
        fee_bps=15, slippage_bps=0))
    fill = report.strategies["deployed"].fills[0]
    assert fill.quantity == 99_850_224
    assert fill.quantity * fill.fill_price_krw + fill.fee_krw <= 100_000_000


def audit(**overrides):
    base = dict(
        decisions_count=2, proposals_count=2, reviews_count=2, orders_count=0,
        fills_count=0, cash_mutation_krw=0, position_mutation_quantity=0,
        decision_ids=("d1", "d2"), proposal_decision_ids=("d1", "d2"),
        review_decision_ids=("d1", "d2"), persisted_ats=(
            "2026-07-01T16:00:00+09:00", "2026-07-02T16:00:00+09:00"),
        proposal_persisted_ats=(
            "2026-07-01T16:01:00+09:00", "2026-07-02T16:01:00+09:00"),
        review_persisted_ats=(
            "2026-07-01T16:02:00+09:00", "2026-07-02T16:02:00+09:00"),
        outcome_ats=("2026-07-02T09:00:00+09:00", "2026-07-03T09:00:00+09:00"),
        evidence_sha256="0" * 64,
    )
    base.update(overrides)
    evidence = ShadowAuditInput(**base)
    if "evidence_sha256" not in overrides:
        evidence = replace(evidence, evidence_sha256=shadow_evidence_sha256(evidence))
    return evidence


def test_shadow_audit_returns_evidence_only_and_is_ready():
    evidence = audit()
    result = audit_shadow(evidence)
    assert result.shadow_ready and not result.activation_performed
    assert result.evidence_sha256 == evidence.evidence_sha256


def test_shadow_audit_rejects_well_formed_wrong_commitment():
    with pytest.raises(ValueError, match="commitment"):
        audit_shadow(audit(evidence_sha256="f" * 64))


@pytest.mark.parametrize("over", [
    {"decision_ids": ["d1", "d2"]},
    {"proposal_decision_ids": ("d1", "d1")},
    {"review_decision_ids": ("d1", "")},
    {"persisted_ats": ["2026-07-01T16:00:00+09:00", "2026-07-02T16:00:00+09:00"]},
    {"proposal_persisted_ats": ("2026-07-02T16:01:00+09:00", "2026-07-01T16:01:00+09:00")},
])
def test_shadow_audit_requires_exact_tuples_unique_ids_and_positional_lineage(over):
    with pytest.raises(ValueError):
        audit_shadow(audit(**over))


@pytest.mark.parametrize("over", [
    {"orders_count": 1}, {"fills_count": 1}, {"cash_mutation_krw": 1},
    {"position_mutation_quantity": -1}, {"decisions_count": 3},
    {"proposal_decision_ids": ("d1", "forged")}, {"reviews_count": 1},
    {"persisted_ats": ("2026-07-01T16:00:00", "2026-07-02T16:00:00+09:00")},
    {"review_persisted_ats": ("2026-07-02T10:00:00+09:00", "2026-07-02T16:02:00+09:00")},
    {"outcome_ats": ("2026-07-01T15:00:00+09:00", "2026-07-03T09:00:00+09:00")},
    {"evidence_sha256": "bad"},
    {"orders_count": True}, {"decision_ids": ("d1", "")},
])
def test_shadow_audit_rejects_mutation_forgery_or_missing_evidence(over):
    with pytest.raises(ValueError):
        audit_shadow(audit(**over))


def test_import_has_no_file_network_environment_or_activation_side_effects(monkeypatch):
    before_env = dict(os.environ)
    opened = []
    connected = []
    real_open = builtins.open
    monkeypatch.setattr(builtins, "open", lambda *a, **k: (opened.append(a), real_open(*a, **k))[1])
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **k: connected.append(a))
    sys.modules.pop("strategy.paper_evaluation", None)
    module = importlib.import_module("strategy.paper_evaluation")
    assert module and opened == [] and connected == [] and dict(os.environ) == before_env
    assert "paper_trading.ledger" not in module.__dict__
