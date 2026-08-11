"""Pure, deterministic evaluation of persisted autonomous paper decisions.

This module only transforms caller-supplied immutable records.  It does not
open files, inspect configuration, connect to a broker, or change runtime mode.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
import hashlib
import json
from types import MappingProxyType
from typing import Mapping, Sequence


SUPPORTED_SYMBOLS = frozenset({"005930", "000660"})
ACTIONS = frozenset({"ENTER", "HOLD", "REDUCE", "EXIT", "ABSTAIN"})
KST = timedelta(hours=9)


def _kst(value: object, name: str) -> datetime:
    if type(value) is not str:
        raise ValueError(f"{name} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != KST or not value.endswith("+09:00"):
        raise ValueError(f"{name} must use explicit +09:00")
    return parsed


def _sha(value: object, name: str) -> str:
    if (type(value) is not str or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)):
        raise ValueError(f"{name} must be a lowercase SHA-256")
    return value


def _int(value: object, name: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    session_id: str
    symbol: str
    action: str
    decided_at: str
    persisted_at: str
    available_data_cutoff: str
    decision_sha256: str
    source_artifact_sha256: str


@dataclass(frozen=True, slots=True)
class MarketOutcome:
    session_id: str
    symbol: str
    price_krw: int
    observed_at: str
    source_artifact_sha256: str


@dataclass(frozen=True, slots=True)
class EvaluationConfig:
    initial_cash_krw: int = 10_000_000
    target_exposure_bps: int = 5_000
    fee_bps: int = 15
    sell_tax_bps: int = 20
    slippage_bps: int = 10
    random_seed: str = "kronostock-paper-evaluation-v1"

    def __post_init__(self) -> None:
        _int(self.initial_cash_krw, "initial_cash_krw", 1)
        for name in ("target_exposure_bps", "fee_bps", "sell_tax_bps", "slippage_bps"):
            if _int(getattr(self, name), name) > 10_000:
                raise ValueError(f"{name} must be <= 10000")
        if type(self.random_seed) is not str or not self.random_seed:
            raise ValueError("random_seed must be a nonempty string")


@dataclass(frozen=True, slots=True)
class Fill:
    decision_session_id: str
    observation_session_id: str
    symbol: str
    side: str
    quantity: int
    raw_price_krw: int
    fill_price_krw: int
    fee_krw: int
    tax_krw: int
    slippage_krw: int


@dataclass(frozen=True, slots=True)
class SessionState:
    session_id: str
    cash_krw: int
    position_quantity: int
    nav_krw: int


@dataclass(frozen=True, slots=True)
class StrategyResult:
    ending_cash_krw: int
    ending_position_quantity: int
    ending_nav_krw: int
    return_bps: int
    turnover_bps: int
    max_drawdown_bps: int
    fill_count: int
    completed_sessions: int
    total_fees_krw: int
    total_tax_krw: int
    total_slippage_krw: int
    action_trace: tuple[str, ...]
    fills: tuple[Fill, ...]
    session_states: tuple[SessionState, ...]


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    schema_version: str
    assumptions: Mapping[str, object]
    strategies: Mapping[str, StrategyResult]
    completed_sessions: int
    probability_calibration_status: str
    brier_status: str
    reliability_status: str
    session_count_threshold_met: bool
    strategy_validity_ready: bool
    strategy_validity_status: str
    user_approval_required: bool
    activation_performed: bool
    canonical_json: str
    report_sha256: str


@dataclass(frozen=True, slots=True)
class ShadowAuditInput:
    decisions_count: int
    proposals_count: int
    reviews_count: int
    orders_count: int
    fills_count: int
    cash_mutation_krw: int
    position_mutation_quantity: int
    decision_ids: tuple[str, ...]
    proposal_decision_ids: tuple[str, ...]
    review_decision_ids: tuple[str, ...]
    persisted_ats: tuple[str, ...]
    proposal_persisted_ats: tuple[str, ...]
    review_persisted_ats: tuple[str, ...]
    outcome_ats: tuple[str, ...]
    evidence_sha256: str


@dataclass(frozen=True, slots=True)
class ShadowAuditResult:
    shadow_ready: bool
    evidence_sha256: str
    activation_performed: bool
    status: str


def shadow_evidence_sha256(evidence: ShadowAuditInput) -> str:
    """Commit to canonical shadow evidence, excluding its commitment field."""
    if type(evidence) is not ShadowAuditInput:
        raise ValueError("exact ShadowAuditInput required")
    content = asdict(evidence)
    del content["evidence_sha256"]
    return hashlib.sha256(_canonical(content).encode()).hexdigest()


def _validate(decisions: Sequence[DecisionRecord], outcomes: Sequence[MarketOutcome]):
    if type(decisions) not in (tuple, list) or type(outcomes) not in (tuple, list):
        raise ValueError("decisions and outcomes must be sequences")
    decision_keys: set[tuple[str, str]] = set()
    symbols: set[str] = set()
    checked_decisions = []
    for item in decisions:
        if type(item) is not DecisionRecord:
            raise ValueError("exact DecisionRecord instances required")
        if type(item.session_id) is not str or type(item.symbol) is not str or item.symbol not in SUPPORTED_SYMBOLS:
            raise ValueError("unsupported symbol or malformed session")
        if type(item.action) is not str or item.action not in ACTIONS:
            raise ValueError("unsupported action")
        decided = _kst(item.decided_at, "decided_at")
        persisted = _kst(item.persisted_at, "persisted_at")
        cutoff = _kst(item.available_data_cutoff, "available_data_cutoff")
        if item.session_id != decided.date().isoformat() or cutoff > decided or persisted <= decided:
            raise ValueError("decision chronology is causally invalid")
        if persisted.date().isoformat() != item.session_id:
            raise ValueError("decision must be persisted in its session")
        _sha(item.decision_sha256, "decision_sha256")
        _sha(item.source_artifact_sha256, "source_artifact_sha256")
        key = (item.session_id, item.symbol)
        if key in decision_keys:
            raise ValueError("duplicate decision symbol/session identity")
        decision_keys.add(key); symbols.add(item.symbol); checked_decisions.append((item, persisted, cutoff))
    outcome_keys: set[tuple[str, str]] = set()
    checked_outcomes = []
    for item in outcomes:
        if type(item) is not MarketOutcome:
            raise ValueError("exact MarketOutcome instances required")
        if type(item.session_id) is not str or item.symbol not in SUPPORTED_SYMBOLS or item.symbol not in symbols:
            raise ValueError("outcome symbol lacks decision lineage")
        observed = _kst(item.observed_at, "observed_at")
        _int(item.price_krw, "price_krw", 1)
        _sha(item.source_artifact_sha256, "source_artifact_sha256")
        key = (item.session_id, item.symbol)
        if key in outcome_keys:
            raise ValueError("duplicate outcome symbol/session identity")
        outcome_keys.add(key); checked_outcomes.append((item, observed))
    # Every outcome must be later than any prior persisted decision it could settle.
    for decision_item, persisted, _ in checked_decisions:
        later = [(outcome_item, observed) for outcome_item, observed in checked_outcomes
                 if outcome_item.symbol == decision_item.symbol and outcome_item.session_id > decision_item.session_id]
        if later and min(observed for _, observed in later) <= persisted:
            raise ValueError("decision must be persisted strictly before outcomes")
    if any(item.session_id != observed.date().isoformat() for item, observed in checked_outcomes):
        raise ValueError("outcome session must match observation timestamp")
    return checked_decisions, checked_outcomes


def _ceil(value: int, bps: int) -> int:
    return (value * bps + 9_999) // 10_000


def _random_action(seed: str, decision_item: DecisionRecord) -> str:
    material = f"{seed}|{decision_item.session_id}|{decision_item.symbol}|{decision_item.decision_sha256}"
    choices = ("ENTER", "HOLD", "REDUCE", "EXIT", "ABSTAIN")
    return choices[int(hashlib.sha256(material.encode()).hexdigest(), 16) % len(choices)]


def _run(name: str, schedule, config: EvaluationConfig) -> StrategyResult:
    cash = config.initial_cash_krw
    positions = {symbol: 0 for symbol in SUPPORTED_SYMBOLS}
    last_prices: dict[str, int] = {}
    fills, states, actions = [], [], []
    fees = taxes = slippage = turnover = 0
    peak = config.initial_cash_krw
    max_dd = 0
    for decision_item, observation, action in schedule:
        raw = observation.price_krw
        symbol = observation.symbol
        last_prices[symbol] = raw
        quantity = positions[symbol]
        actions.append(action)
        current_nav = cash + sum(positions[item] * last_prices.get(item, 0) for item in positions)
        desired = quantity
        if action == "ENTER":
            desired = (current_nav * config.target_exposure_bps // 10_000) // raw
            # Size the single all-or-none order with its adverse execution and
            # fee already reserved. ceil(n*f/10000) <= cash-n is equivalent
            # to n*(10000+f) <= cash*10000 because cash-n is an integer.
            candidate_price = (raw * (10_000 + config.slippage_bps) + 9_999) // 10_000
            affordable_delta = (cash * 10_000) // (candidate_price * (10_000 + config.fee_bps))
            desired = min(desired, quantity + affordable_delta)
        elif action == "REDUCE":
            desired = quantity // 2
        elif action == "EXIT":
            desired = 0
        delta = desired - quantity
        if delta > 0:
            fill_price = (raw * (10_000 + config.slippage_bps) + 9_999) // 10_000
            notional = delta * fill_price; fee = _ceil(notional, config.fee_bps)
            if notional + fee <= cash:  # no partial fill and no borrowing
                cash -= notional + fee; quantity += delta; positions[symbol] = quantity
                slip = delta * (fill_price - raw)
                fills.append(Fill(decision_item.session_id, observation.session_id, observation.symbol,
                                  "BUY", delta, raw, fill_price, fee, 0, slip))
                fees += fee; slippage += slip; turnover += delta * raw
        elif delta < 0:
            sold = -delta
            fill_price = max(1, raw * (10_000 - config.slippage_bps) // 10_000)
            notional = sold * fill_price; fee = _ceil(notional, config.fee_bps); tax = _ceil(notional, config.sell_tax_bps)
            proceeds = notional - fee - tax
            if proceeds >= 0 and sold <= quantity:
                cash += proceeds; quantity -= sold; positions[symbol] = quantity
                slip = sold * (raw - fill_price)
                fills.append(Fill(decision_item.session_id, observation.session_id, observation.symbol,
                                  "SELL", sold, raw, fill_price, fee, tax, slip))
                fees += fee; taxes += tax; slippage += slip; turnover += sold * raw
        nav = cash + sum(positions[item] * last_prices.get(item, 0) for item in positions)
        if cash < 0 or any(value < 0 for value in positions.values()) or nav < 0:
            raise ValueError("negative economic state forbidden")
        peak = max(peak, nav)
        max_dd = max(max_dd, (peak - nav) * 10_000 // peak)
        states.append(SessionState(observation.session_id, cash, sum(positions.values()), nav))
    ending_nav = states[-1].nav_krw if states else config.initial_cash_krw
    completed_sessions = len({observation.session_id for _, observation, _ in schedule})
    return StrategyResult(cash, sum(positions.values()), ending_nav,
        (ending_nav - config.initial_cash_krw) * 10_000 // config.initial_cash_krw,
        turnover * 10_000 // config.initial_cash_krw, max_dd, len(fills), completed_sessions,
        fees, taxes, slippage, tuple(actions), tuple(fills), tuple(states))


def evaluate_replay(decisions: Sequence[DecisionRecord], outcomes: Sequence[MarketOutcome],
                    config: EvaluationConfig = EvaluationConfig()) -> EvaluationReport:
    """Evaluate deployed decisions and baselines using strictly next-session prices."""
    if type(config) is not EvaluationConfig:
        raise ValueError("exact EvaluationConfig required")
    checked_decisions, checked_outcomes = _validate(decisions, outcomes)
    ordered_decisions = sorted(checked_decisions, key=lambda pair: (pair[0].session_id, pair[0].symbol))
    ordered_outcomes = sorted(checked_outcomes, key=lambda pair: (pair[0].session_id, pair[0].symbol))
    schedule = []
    flow_schedule = []
    for item, persisted, _ in ordered_decisions:
        eligible = [(outcome_item, observed) for outcome_item, observed in ordered_outcomes
                    if outcome_item.symbol == item.symbol and outcome_item.session_id > item.session_id
                    and observed > persisted]
        if eligible:
            next_outcome, _ = eligible[0]
            schedule.append((item, next_outcome, item.action))
            known = [outcome_item for outcome_item, observed in ordered_outcomes
                     if outcome_item.symbol == item.symbol and observed <= datetime.fromisoformat(item.available_data_cutoff)]
            flow_action = "HOLD"
            if len(known) >= 2:
                flow_action = "ENTER" if known[-1].price_krw > known[-2].price_krw else (
                    "EXIT" if known[-1].price_krw < known[-2].price_krw else "HOLD")
            flow_schedule.append((item, next_outcome, flow_action))
    chronology_key = lambda entry: (
        datetime.fromisoformat(entry[1].observed_at), entry[1].symbol,
        entry[0].session_id, entry[0].decision_sha256)
    schedule.sort(key=chronology_key)
    flow_schedule.sort(key=chronology_key)
    tracks = {
        "deployed": _run("deployed", schedule, config),
        "no_action": _run("no_action", [(d, o, "HOLD") for d, o, _ in schedule], config),
        "random": _run("random", [(d, o, _random_action(config.random_seed, d)) for d, o, _ in schedule], config),
        "flow_momentum": _run("flow_momentum", flow_schedule, config),
    }
    assumptions = {**asdict(config), "execution_timing": "strictly_later_next_session",
                   "quantity_policy": "integer_target_exposure_no_partial_fills",
                   "integer_rounding": "target_and_turnover_floor; costs_and_buy_price_ceil",
                   "turnover_denominator": "initial_cash_krw",
                   "baseline_timing_and_costs": "identical"}
    completed = len({observation.session_id for _, observation, _ in schedule})
    threshold_met = completed >= 20
    payload = {"schema_version": "paper-evaluation-v1", "assumptions": assumptions,
        "strategies": {key: asdict(value) for key, value in sorted(tracks.items())},
        "completed_sessions": completed,
        "probability_calibration_status": "NOT_EVALUATED", "brier_status": "NOT_EVALUATED",
        "reliability_status": "NOT_ESTABLISHED", "session_count_threshold_met": threshold_met,
        "strategy_validity_ready": False, "strategy_validity_status": "NOT_ESTABLISHED",
        "user_approval_required": True, "activation_performed": False}
    canonical_json = _canonical(payload)
    digest = hashlib.sha256(canonical_json.encode()).hexdigest()
    return EvaluationReport("paper-evaluation-v1", MappingProxyType(assumptions),
        MappingProxyType(tracks), completed, "NOT_EVALUATED", "NOT_EVALUATED",
        "NOT_ESTABLISHED", threshold_met, False, "NOT_ESTABLISHED", True, False,
        canonical_json, digest)


def audit_shadow(evidence: ShadowAuditInput) -> ShadowAuditResult:
    """Validate shadow evidence without reading or mutating any runtime state."""
    if type(evidence) is not ShadowAuditInput:
        raise ValueError("exact ShadowAuditInput required")
    for name in ("decisions_count", "proposals_count", "reviews_count", "orders_count", "fills_count"):
        _int(getattr(evidence, name), name)
    if type(evidence.cash_mutation_krw) is not int or type(evidence.position_mutation_quantity) is not int:
        raise ValueError("economic mutations must be exact integers")
    claimed_commitment = _sha(evidence.evidence_sha256, "evidence_sha256")
    if (evidence.orders_count or evidence.fills_count or evidence.cash_mutation_krw
            or evidence.position_mutation_quantity):
        raise ValueError("shadow mode requires zero economic mutations")
    tuple_fields = ("decision_ids", "proposal_decision_ids", "review_decision_ids",
                    "persisted_ats", "proposal_persisted_ats", "review_persisted_ats",
                    "outcome_ats")
    if any(type(getattr(evidence, name)) is not tuple for name in tuple_fields):
        raise ValueError("shadow ID and timestamp fields must be exact immutable tuples")
    ids = evidence.decision_ids
    if (any(type(value) is not str or not value for value in ids)
            or len(ids) != evidence.decisions_count or len(set(ids)) != len(ids)
            or len(evidence.proposal_decision_ids) != evidence.proposals_count
            or len(evidence.review_decision_ids) != evidence.reviews_count
            or tuple(evidence.proposal_decision_ids) != tuple(ids)
            or tuple(evidence.review_decision_ids) != tuple(ids)):
        raise ValueError("shadow counts or lineage are inconsistent")
    if (len(evidence.persisted_ats) != len(ids)
            or len(evidence.proposal_persisted_ats) != len(ids)
            or len(evidence.review_persisted_ats) != len(ids)
            or len(evidence.outcome_ats) != len(ids) or not ids):
        raise ValueError("required shadow audit chronology is missing")
    persisted = tuple(_kst(value, "persisted_at") for value in evidence.persisted_ats)
    proposals = tuple(_kst(value, "proposal_persisted_at") for value in evidence.proposal_persisted_ats)
    reviews = tuple(_kst(value, "review_persisted_at") for value in evidence.review_persisted_ats)
    outcomes = tuple(_kst(value, "outcome_at") for value in evidence.outcome_ats)
    if any(not decision_time <= proposal_time <= review_time < outcome_time
           for decision_time, proposal_time, review_time, outcome_time
           in zip(persisted, proposals, reviews, outcomes)):
        raise ValueError("decisions, proposals, and reviews must be persisted before outcomes")
    if claimed_commitment != shadow_evidence_sha256(evidence):
        raise ValueError("evidence_sha256 does not match canonical evidence commitment")
    return ShadowAuditResult(True, claimed_commitment, False, "EVIDENCE_ONLY_NOT_ACTIVATED")


__all__ = ["DecisionRecord", "EvaluationConfig", "EvaluationReport", "MarketOutcome",
           "ShadowAuditInput", "ShadowAuditResult", "audit_shadow", "evaluate_replay",
           "shadow_evidence_sha256"]
