"""Task 3 — strategy.paper_agent 단일 종목 페이퍼 제안 경계 테스트.

완전 오프라인: 모델 transport 는 주입된 callable 이며, 경계 내부에는
네트워크/브로커/원장/주문 경로가 존재하지 않아야 한다.

Codex BLOCKED 재수정 계약(2차):
- B1 증거 항목은 값/단위/상태/출처/계보를 완결적으로 포함한다.
- B2 타임스탬프는 KST(+09:00) 오프셋까지 강제되며 위반은 예외 없이 fail-closed 로 차단된다.
- B3 계보 해시는 항상 실제 표준 스냅샷 해시(직렬화 불가 시 결정론적 무효 다이제스트)만 증명한다.
- B4 증거 ID 는 holdings/policy_limits 를 포함한 요청 전체 문맥에 결속되어 재사용(replay)이 불가능하다.
- B5 요청은 중첩 컬렉션까지 깊은 불변이다.
- B6 모델 프로즈(thesis/invalidation/data-gap)는 요청이 공표한 표준 문안과 정확히 일치해야 한다.
- B7 최종 검증기는 모든 결정론적 ABSTAIN 을 수용하고, 모델은 내부 사유를 위조할 수 없다.
- B8 스냅샷 리스트/상태/스코어링은 물론 그룹 라우팅/캡/정렬/기여도까지
  FeatureEngine 표준 계약과 정확히 일치해야 transport 에 도달한다.
- B9 정책 한도 입력 + 계획서의 폐쇄 출력 스키마(identity/audit 필드 포함).
"""
from __future__ import annotations

import hashlib
import inspect
import json
import sqlite3
from collections.abc import Mapping as MappingABC
from copy import deepcopy
from dataclasses import replace
from types import MappingProxyType

import pytest

from ksf.feature_engine import (
    DEFAULT_GROUP_CAPS_BPS,
    GROUP_ORDER,
    EngineFeature,
    FeatureEngine,
    FeatureGroupOutput,
    SymbolFeatureOutput,
)
from ksf.global_collector import init_ledger_schema, stable_id
from tests.test_ksf_feature_engine import AS_OF, CUTOFF, add_complete_inputs, add_feature

from strategy import paper_agent
from strategy.paper_agent import (
    ABSTAIN_REASONS,
    DATA_ABSTAIN_REASONS,
    MODEL_ABSTAIN_REASONS,
    MODEL_FAILURE_ABSTAIN_REASONS,
    PROPOSAL_KEYS,
    build_abstain_proposal,
    build_proposal_request,
    propose,
    validate_proposal,
)


EXPECTED_KEYS = {
    "symbol",
    "response_schema_version",
    "prompt_template_version",
    "model_policy_version",
    "model_provider",
    "model_name",
    "action",
    "target_exposure_pct",
    "confidence_bps",
    "thesis",
    "evidence_ids",
    "invalidation_conditions",
    "data_gaps",
    "abstain_reason",
}

GAP_ITEM_KEYS = {"feature_name", "feature_group", "status", "as_of", "text", "evidence_ids"}

RUN_ID = "run_fixture_task3"
DECISION_ID = "decision_fixture_task3"
POLICY = {"max_target_exposure_pct": 25.0}


@pytest.fixture
def ledger() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_ledger_schema(conn)
    return conn


def _build(ledger, symbol="005930", cutoff=CUTOFF):
    return FeatureEngine(ledger).build_symbol(
        symbol, available_data_cutoff=cutoff, as_of_kst=AS_OF
    )


def _output(ledger, symbol="005930"):
    add_complete_inputs(ledger, symbol)
    return _build(ledger, symbol)


def _snapshot_hash(output) -> str:
    return hashlib.sha256(output.stable_json().encode("utf-8")).hexdigest()


def _safe_hash(output) -> str:
    """구조가 깨진 위조 스냅샷은 표준 해시 계산이 불가능하므로 형식만 갖춘 해시."""
    try:
        return _snapshot_hash(output)
    except Exception:
        return "0" * 64


def _recomputed_output_id(output) -> str:
    payload = {
        "symbol": output.symbol,
        "state": output.state,
        "scoring_allowed": output.scoring_allowed,
        "available_data_cutoff": output.available_data_cutoff,
        "as_of_kst": output.as_of_kst,
        "groups": {name: output.groups[name].to_dict() for name in GROUP_ORDER},
        "missing_required": list(output.missing_required),
        "missing_optional": list(output.missing_optional),
        "stale_required": list(output.stale_required),
        "license_blocked": list(output.license_blocked),
    }
    return stable_id("ksf_engine", payload)


def _forge(output, **over):
    """필드를 바꾸되 output_id 는 내용과 일치하도록 재계산한 '일관된 위조'."""
    forged = replace(output, **over)
    return replace(forged, output_id=_recomputed_output_id(forged))


def _replace_feature(output, group_name, feature_name, **feature_over):
    group = output.groups[group_name]
    features = tuple(
        replace(f, **feature_over) if f.name == feature_name else f for f in group.features
    )
    groups = dict(output.groups)
    groups[group_name] = replace(group, features=features)
    forged = replace(output, groups=MappingProxyType(groups))
    return replace(forged, output_id=_recomputed_output_id(forged))


def _replace_group(output, group_name, **group_over):
    groups = dict(output.groups)
    groups[group_name] = replace(groups[group_name], **group_over)
    forged = replace(output, groups=MappingProxyType(groups))
    return replace(forged, output_id=_recomputed_output_id(forged))


def _move_feature(output, from_group, to_group, feature_name):
    """피처 1개를 다른 그룹으로 옮기되 정렬/ID/해시는 표준대로 재구성한 위조."""
    src = output.groups[from_group]
    moved = next(f for f in src.features if f.name == feature_name)
    groups = dict(output.groups)
    groups[from_group] = replace(
        src, features=tuple(f for f in src.features if f.name != feature_name)
    )
    dest = groups[to_group]
    groups[to_group] = replace(
        dest, features=tuple(sorted(dest.features + (moved,), key=lambda f: f.name))
    )
    forged = replace(output, groups=MappingProxyType(groups))
    return replace(forged, output_id=_recomputed_output_id(forged))


def _append_feature(output, group_name, feature):
    group = output.groups[group_name]
    groups = dict(output.groups)
    groups[group_name] = replace(
        group, features=tuple(sorted(group.features + (feature,), key=lambda f: f.name))
    )
    forged = replace(output, groups=MappingProxyType(groups))
    return replace(forged, output_id=_recomputed_output_id(forged))


def _drop_group(output):
    groups = {k: v for k, v in output.groups.items() if k != "event"}
    return replace(output, groups=MappingProxyType(groups))


def _with_duplicate_close(output):
    close = next(f for f in output.groups["price_volume"].features if f.name == "close")
    event = output.groups["event"]
    groups = dict(output.groups)
    groups["event"] = replace(event, features=event.features + (close,))
    forged = replace(output, groups=MappingProxyType(groups))
    return replace(forged, output_id=_recomputed_output_id(forged))


def _holdings(symbol="005930", **over):
    base = {
        "symbol": symbol,
        "cash_krw": 9_000_000,
        "quantity": 10,
        "market_value_krw": 1_000_000,
        "total_equity_krw": 10_000_000,
        "current_exposure_pct": 10.0,
    }
    base.update(over)
    return base


def _flat_holdings(symbol="005930"):
    return {
        "symbol": symbol,
        "cash_krw": 10_000_000,
        "quantity": 0,
        "market_value_krw": 0,
        "total_equity_krw": 10_000_000,
        "current_exposure_pct": 0.0,
    }


def _lineage(output, **over):
    lineage = {
        "ksf_run_id": RUN_ID,
        "ksf_decision_id": DECISION_ID,
        "feature_snapshot_sha256": _safe_hash(output),
        "available_data_cutoff": output.available_data_cutoff,
    }
    lineage.update(over)
    return lineage


def _request(output, holdings=None, policy=None, **lineage_over):
    return build_proposal_request(
        output,
        holdings if holdings is not None else _holdings(output.symbol),
        policy_limits=policy if policy is not None else dict(POLICY),
        model_provider="offline-test",
        model_name="fixture-model",
        **_lineage(output, **lineage_over),
    )


def _propose(output, transport, holdings=None, policy=None, **lineage_over):
    return propose(
        output,
        holdings if holdings is not None else _holdings(output.symbol),
        policy_limits=policy if policy is not None else dict(POLICY),
        model_provider="offline-test",
        model_name="fixture-model",
        transport=transport,
        **_lineage(output, **lineage_over),
    )


def _canonical_thesis(request, action):
    """요청이 공표한 행동별 표준 thesis. 구현 전(RED)에는 기존 문안으로 폴백."""
    theses = request.get("canonical_theses")
    if theses is None:
        return "단일 종목 수급 입력이 유효하여 노출 확대를 제안합니다."
    if action in theses:
        return theses[action]
    return theses["ENTER"]


def _canonical_invalidation(request, abstain=False):
    key = "canonical_abstain_invalidation_text" if abstain else "canonical_invalidation_text"
    default = (
        "판단 자격이 회복되고 검증된 최신 근거가 제공되면 이 보류를 재검토합니다."
        if abstain
        else "인용한 수급 입력 상태가 바뀌면 이 제안을 무효화합니다."
    )
    return request.get(key, default)


def _feature_evidence_id(request):
    return next(
        (i["id"] for i in request["evidence_catalog"] if i.get("kind") == "feature"),
        request["evidence_catalog"][0]["id"],
    )


def _valid_payload(request, **over):
    cited = _feature_evidence_id(request)
    payload = {
        "symbol": request["symbol"],
        "response_schema_version": request["response_schema_version"],
        "prompt_template_version": request["prompt_template_version"],
        "model_policy_version": request["model_policy_version"],
        "model_provider": request["model_provider"],
        "model_name": request["model_name"],
        "action": "ENTER",
        "target_exposure_pct": 15.0,
        "confidence_bps": 4200,
        "thesis": None,
        "evidence_ids": [cited],
        "invalidation_conditions": [
            {
                "text": _canonical_invalidation(request),
                "evidence_ids": [cited],
            }
        ],
        "data_gaps": [],
        "abstain_reason": None,
    }
    payload.update(over)
    if "thesis" not in over:
        payload["thesis"] = _canonical_thesis(request, payload["action"])
    return payload


def _abstain_payload(request, reason="NO_EDGE"):
    return _valid_payload(
        request,
        action="ABSTAIN",
        target_exposure_pct=0.0,
        confidence_bps=0,
        invalidation_conditions=[
            {
                "text": _canonical_invalidation(request, abstain=True),
                "evidence_ids": [_feature_evidence_id(request)],
            }
        ],
        abstain_reason=reason,
    )


class Recorder:
    """주입 transport: 호출 기록 + 고정 결과/예외 반환."""

    def __init__(self, result=None, exc=None):
        self.calls = []
        self.result = result
        self.exc = exc

    def __call__(self, request):
        self.calls.append(request)
        if self.exc is not None:
            raise self.exc
        return self.result(request) if callable(self.result) else self.result


def _valid_transport():
    return Recorder(result=lambda req: json.dumps(_valid_payload(req), ensure_ascii=False))


# ---------------------------------------------------------------------------
# 계약 상수
# ---------------------------------------------------------------------------


def test_public_contract_pins_exact_keys_and_disjoint_reason_allowlists():
    assert set(PROPOSAL_KEYS) == EXPECTED_KEYS
    assert "DATA_SNAPSHOT_INVALID" in DATA_ABSTAIN_REASONS
    assert DATA_ABSTAIN_REASONS.isdisjoint(MODEL_FAILURE_ABSTAIN_REASONS)
    assert DATA_ABSTAIN_REASONS.isdisjoint(MODEL_ABSTAIN_REASONS)
    assert MODEL_FAILURE_ABSTAIN_REASONS.isdisjoint(MODEL_ABSTAIN_REASONS)
    assert (
        DATA_ABSTAIN_REASONS | MODEL_FAILURE_ABSTAIN_REASONS | MODEL_ABSTAIN_REASONS
        == ABSTAIN_REASONS
    )
    assert paper_agent.RESPONSE_SCHEMA_VERSION
    assert paper_agent.PROMPT_TEMPLATE_VERSION
    assert paper_agent.MODEL_POLICY_VERSION


# ---------------------------------------------------------------------------
# 정상 경로 (B9 폐쇄 출력 스키마 포함)
# ---------------------------------------------------------------------------


def test_valid_enter_proposal_round_trips_with_exact_closed_keys(ledger):
    output = _output(ledger)
    transport = _valid_transport()

    proposal = _propose(output, transport)

    assert set(proposal) == EXPECTED_KEYS
    assert proposal["symbol"] == "005930"
    assert proposal["response_schema_version"] == paper_agent.RESPONSE_SCHEMA_VERSION
    assert proposal["prompt_template_version"] == paper_agent.PROMPT_TEMPLATE_VERSION
    assert proposal["model_policy_version"] == paper_agent.MODEL_POLICY_VERSION
    assert proposal["model_provider"] == "offline-test"
    assert proposal["model_name"] == "fixture-model"
    assert proposal["action"] == "ENTER"
    assert proposal["target_exposure_pct"] == 15.0
    assert proposal["confidence_bps"] == 4200
    assert proposal["abstain_reason"] is None
    assert proposal["evidence_ids"]
    assert proposal["invalidation_conditions"][0]["evidence_ids"]
    # run/decision ID 는 요청/감사 컨텍스트 전용이며 모델 출력에 존재하지 않는다.
    assert "ksf_run_id" not in proposal
    assert "ksf_decision_id" not in proposal
    assert "feature_snapshot_sha256" not in proposal
    assert len(transport.calls) == 1
    # 최종(경계) 검증기도 동일 제안을 수용한다.
    request = _request(output)
    assert validate_proposal(proposal, request, mode="final") == proposal


def test_valid_proposal_reflects_unavailable_evidence_in_data_gaps(ledger):
    output = _output(ledger)

    proposal = _propose(output, _valid_transport())

    assert proposal["data_gaps"], "라이선스 차단 입력이 data_gaps 로 반영되어야 한다"
    gap = next(g for g in proposal["data_gaps"] if g["feature_name"] == "hbm_supply_tightness")
    assert set(gap) == GAP_ITEM_KEYS
    assert gap["status"] == "LICENSE_BLOCKED"
    assert gap["evidence_ids"]


def test_transport_receives_immutable_request_with_lineage_and_sanitized_holdings(ledger):
    output = _output(ledger)
    transport = _valid_transport()

    _propose(output, transport)

    sent = transport.calls[0]
    assert sent["symbol"] == "005930"
    assert sent["ksf_run_id"] == RUN_ID
    assert sent["ksf_decision_id"] == DECISION_ID
    assert sent["feature_snapshot_sha256"] == _snapshot_hash(output)
    assert sent["available_data_cutoff"] == CUTOFF
    assert sent["policy_limits"]["max_target_exposure_pct"] == 25.0
    assert sent["holdings"]["cash_krw"] == 9_000_000
    assert set(sent["holdings"]) == {
        "symbol", "cash_krw", "quantity", "market_value_krw",
        "total_equity_krw", "current_exposure_pct",
    }
    with pytest.raises(TypeError):
        sent["ksf_run_id"] = "forged"


def test_transport_mutation_attempt_fails_closed_as_provider_error(ledger):
    output = _output(ledger)

    def evil(request):
        request["ksf_run_id"] = "forged"
        return json.dumps(_valid_payload(request), ensure_ascii=False)

    proposal = _propose(output, evil)

    assert proposal["action"] == "ABSTAIN"
    assert proposal["abstain_reason"] == "MODEL_PROVIDER_ERROR"


def test_transport_may_return_mapping_instead_of_json_string(ledger):
    output = _output(ledger)
    transport = Recorder(result=lambda req: _valid_payload(req))

    proposal = _propose(output, transport)

    assert proposal["action"] == "ENTER"


# ---------------------------------------------------------------------------
# B1: 증거 완결성 — 값/단위/상태/출처/계보가 증거 본문에 결속
# ---------------------------------------------------------------------------


def test_feature_evidence_carries_complete_bound_values(ledger):
    output = _output(ledger)
    request = _request(output)
    catalog = list(request["evidence_catalog"])

    by_name = {i["feature_name"]: i for i in catalog if i["kind"] == "feature"}
    close = by_name["close"]
    assert close["value"] == 70000
    assert close["status"] == "READY"
    assert close["feature_group"] == "price_volume"
    assert close["source_feature_group"] == "domestic_price"
    assert close["as_of"] == "2026-07-31T15:30:00+09:00"
    assert "unit" in close and "missing_reason" in close
    # 계보 결속(B4 와 공유): 모든 증거 본문에 요청 계보가 포함된다.
    assert close["ksf_run_id"] == RUN_ID
    assert close["ksf_decision_id"] == DECISION_ID
    assert close["feature_snapshot_sha256"] == _snapshot_hash(output)
    assert close["available_data_cutoff"] == CUTOFF

    unavailable = by_name["hbm_supply_tightness"]
    assert unavailable["value"] is None
    assert unavailable["status"] == "LICENSE_BLOCKED"
    assert unavailable["missing_reason"]

    group_items = [i for i in catalog if i["kind"] == "group_contribution"]
    assert group_items
    for item in group_items:
        assert isinstance(item["contribution_bps"], int)
        assert isinstance(item["cap_bps"], int)
        assert abs(item["contribution_bps"]) <= item["cap_bps"] <= 10000

    ids = [i["id"] for i in catalog]
    assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# 닫힌 스키마: 추가/누락 키, lineage 주입 차단
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update({"order_id": "ord-1"}),
        lambda p: p.update({"quantity": 10}),
        lambda p: p.update({"ksf_run_id": "forged-run"}),
        lambda p: p.update({"ksf_decision_id": "forged-decision"}),
        lambda p: p.update({"feature_snapshot_sha256": "b" * 64}),
        lambda p: p.pop("thesis"),
        lambda p: p.pop("abstain_reason"),
        lambda p: p.pop("data_gaps"),
        lambda p: p.pop("symbol"),
    ],
)
def test_extra_or_missing_keys_and_lineage_injection_abstain(ledger, mutate):
    output = _output(ledger)
    request = _request(output)
    payload = _valid_payload(request)
    mutate(payload)

    with pytest.raises(ValueError, match="keys"):
        validate_proposal(payload, request)

    proposal = _propose(output, Recorder(result=payload))
    assert proposal["action"] == "ABSTAIN"
    assert proposal["abstain_reason"] == "MODEL_OUTPUT_INVALID"


# ---------------------------------------------------------------------------
# B9: identity/version 필드는 요청과 정확히 일치해야 하고 provider 가 바꿀 수 없다
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "over",
    [
        {"symbol": "000660"},
        {"response_schema_version": "spoofed-schema-v9"},
        {"prompt_template_version": "spoofed-prompt"},
        {"model_policy_version": "spoofed-policy"},
        {"model_provider": "other-provider"},
        {"model_name": "other-model"},
    ],
)
def test_provider_cannot_alter_identity_or_version_fields(ledger, over):
    output = _output(ledger)
    request = _request(output)
    payload = _valid_payload(request, **over)

    with pytest.raises(ValueError, match="exactly equal"):
        validate_proposal(payload, request)

    proposal = _propose(output, Recorder(result=payload))
    assert proposal["abstain_reason"] == "MODEL_OUTPUT_INVALID"


# ---------------------------------------------------------------------------
# 모델 호출 전 자격 실패 → transport 미호출 + 결정론적 ABSTAIN
# ---------------------------------------------------------------------------


def test_missing_required_data_abstains_without_calling_transport(ledger):
    add_complete_inputs(ledger, "005930")
    ledger.execute("DELETE FROM ksf_normalized_features WHERE feature_name='close'")
    output = _build(ledger)
    transport = _valid_transport()

    proposal = _propose(output, transport)

    assert transport.calls == []
    assert set(proposal) == EXPECTED_KEYS
    assert proposal["action"] == "ABSTAIN"
    assert proposal["abstain_reason"] == "DATA_MISSING_REQUIRED"


def test_stale_required_data_abstains_without_calling_transport(ledger):
    add_complete_inputs(ledger, "005930")
    ledger.execute(
        "UPDATE ksf_normalized_features SET feature_status='STALE' WHERE feature_name='close'"
    )
    output = _build(ledger)
    transport = _valid_transport()

    proposal = _propose(output, transport)

    assert transport.calls == []
    assert proposal["action"] == "ABSTAIN"
    assert proposal["abstain_reason"] == "DATA_STALE_REQUIRED"


def test_restricted_required_data_abstains_without_calling_transport(ledger):
    add_complete_inputs(ledger, "005930")
    ledger.execute(
        "UPDATE ksf_normalized_features SET feature_status='LICENSE_BLOCKED',"
        " missing_reason='license review' WHERE feature_name='close'"
    )
    output = _build(ledger)
    transport = _valid_transport()

    proposal = _propose(output, transport)

    assert transport.calls == []
    assert proposal["action"] == "ABSTAIN"
    assert proposal["abstain_reason"] == "DATA_RESTRICTED_REQUIRED"


def test_cutoff_mismatch_between_lineage_and_features_abstains_pre_model(ledger):
    output = _output(ledger)
    transport = _valid_transport()

    proposal = _propose(
        output, transport, available_data_cutoff="2026-07-31T15:59:00+09:00"
    )

    assert transport.calls == []
    assert proposal["action"] == "ABSTAIN"
    assert proposal["abstain_reason"] == "DATA_CUTOFF_INCONSISTENT"


def test_future_dated_feature_relative_to_cutoff_abstains_pre_model(ledger):
    output = _output(ledger)
    early_cutoff = "2026-07-31T15:00:00+09:00"  # 피처 source_as_of(15:30) 이후 관측 금지
    output = _forge(output, available_data_cutoff=early_cutoff)
    transport = _valid_transport()

    proposal = _propose(output, transport)

    assert transport.calls == []
    assert proposal["action"] == "ABSTAIN"
    assert proposal["abstain_reason"] == "DATA_CUTOFF_INCONSISTENT"


def test_non_derivable_run_state_snapshot_abstains_pre_model(ledger):
    # FeatureEngine 은 4개 유도 상태만 산출하므로 BLOCKED_REVIEW 스냅샷은
    # 이 경계에서 표준 유도와 불일치 → DATA_SNAPSHOT_INVALID 로 fail-closed.
    output = _forge(_output(ledger), state="BLOCKED_REVIEW", scoring_allowed=False)
    transport = _valid_transport()

    proposal = _propose(output, transport)

    assert transport.calls == []
    assert proposal["action"] == "ABSTAIN"
    assert proposal["abstain_reason"] == "DATA_SNAPSHOT_INVALID"


# ---------------------------------------------------------------------------
# B2: 타임스탬프 정합성 — 위조/기형 타임스탬프는 예외 없이 fail-closed
# ---------------------------------------------------------------------------


def test_as_of_kst_earlier_than_cutoff_abstains_pre_model(ledger):
    output = _forge(_output(ledger), as_of_kst="2026-07-31T15:00:00+09:00")
    transport = _valid_transport()

    proposal = _propose(output, transport)

    assert transport.calls == []
    assert proposal["abstain_reason"] == "DATA_CUTOFF_INCONSISTENT"


def test_naive_as_of_kst_abstains_pre_model(ledger):
    output = _forge(_output(ledger), as_of_kst="2026-07-31T16:10:00")
    transport = _valid_transport()

    proposal = _propose(output, transport)

    assert transport.calls == []
    assert proposal["abstain_reason"] == "DATA_CUTOFF_INCONSISTENT"


def test_malformed_feature_timestamp_never_escapes_and_abstains(ledger):
    output = _replace_feature(
        _output(ledger), "price_volume", "close", source_as_of="not-a-timestamp"
    )
    transport = _valid_transport()

    proposal = _propose(output, transport)  # 예외로 탈출하면 테스트 실패

    assert transport.calls == []
    assert set(proposal) == EXPECTED_KEYS
    assert proposal["abstain_reason"] == "DATA_CUTOFF_INCONSISTENT"


def test_naive_feature_timestamp_abstains_pre_model(ledger):
    output = _replace_feature(
        _output(ledger), "price_volume", "close", source_as_of="2026-07-31T15:30:00"
    )
    transport = _valid_transport()

    proposal = _propose(output, transport)

    assert transport.calls == []
    assert proposal["abstain_reason"] == "DATA_CUTOFF_INCONSISTENT"


def test_malformed_output_cutoff_abstains_instead_of_raising(ledger):
    output = _forge(_output(ledger), available_data_cutoff="not-a-timestamp")
    transport = _valid_transport()

    proposal = _propose(output, transport, available_data_cutoff=CUTOFF)

    assert transport.calls == []
    assert proposal["abstain_reason"] == "DATA_CUTOFF_INCONSISTENT"


def test_cutoff_representation_mismatch_fails_closed(ledger):
    output = _output(ledger)
    transport = _valid_transport()

    # 같은 순간이라도 요청/출력 컷오프 문자열은 정확히 일치해야 한다.
    proposal = _propose(output, transport, available_data_cutoff="2026-07-31T07:00:00+00:00")

    assert transport.calls == []
    assert proposal["abstain_reason"] == "DATA_CUTOFF_INCONSISTENT"


# ---------------------------------------------------------------------------
# B8: 스냅샷 표준 계약 위반 → DATA_SNAPSHOT_INVALID, transport 미호출, 크래시 금지
# ---------------------------------------------------------------------------


SNAPSHOT_FORGERIES = [
    ("forged_output_id", lambda o: replace(o, output_id="ksf_engine_" + "f" * 24)),
    ("scoring_flag_inconsistent_with_state", lambda o: _forge(o, scoring_allowed=False)),
    ("state_not_canonical", lambda o: _forge(o, state="NOT_A_STATE")),
    ("missing_required_inconsistent_with_state", lambda o: _forge(o, missing_required=("close",))),
    ("nan_feature_value", lambda o: _replace_feature(o, "price_volume", "close", value=float("nan"))),
    ("inf_feature_value", lambda o: _replace_feature(o, "price_volume", "close", value=float("inf"))),
    ("bool_feature_value", lambda o: _replace_feature(o, "price_volume", "close", value=True)),
    ("ready_feature_with_null_value", lambda o: _replace_feature(o, "price_volume", "close", value=None)),
    ("noncanonical_feature_status", lambda o: _replace_feature(o, "price_volume", "close", status="BOGUS")),
    ("float_group_contribution", lambda o: _replace_group(o, "price_volume", contribution_bps=12.5)),
    ("contribution_exceeds_cap", lambda o: _replace_group(o, "price_volume", contribution_bps=9999)),
    ("cap_out_of_range", lambda o: _replace_group(o, "price_volume", cap_bps=20000)),
    ("group_name_mismatch", lambda o: _replace_group(o, "event", name="price_volume")),
    ("dropped_group", _drop_group),
    ("duplicate_feature_name", _with_duplicate_close),
]


@pytest.mark.parametrize(
    "forge", [f for _, f in SNAPSHOT_FORGERIES], ids=[n for n, _ in SNAPSHOT_FORGERIES]
)
def test_malformed_snapshot_abstains_without_transport_or_crash(ledger, forge):
    output = _output(ledger)
    forged = forge(output)
    transport = _valid_transport()

    proposal = _propose(forged, transport, feature_snapshot_sha256=_safe_hash(forged))

    assert transport.calls == []
    assert set(proposal) == EXPECTED_KEYS
    assert proposal["action"] == "ABSTAIN"
    assert proposal["abstain_reason"] == "DATA_SNAPSHOT_INVALID"


# ---------------------------------------------------------------------------
# B3: 스냅샷 계보 결속 — 공급된 해시는 표준 스냅샷 해시와 일치해야 한다
# ---------------------------------------------------------------------------


def test_forged_snapshot_hash_is_rejected(ledger):
    output = _output(ledger)

    with pytest.raises(ValueError, match="canonical feature snapshot hash"):
        _request(output, feature_snapshot_sha256="b" * 64)

    with pytest.raises(ValueError, match="canonical feature snapshot hash"):
        _propose(output, _valid_transport(), feature_snapshot_sha256="b" * 64)


def test_snapshot_hash_binds_to_snapshot_content(ledger):
    output = _output(ledger)
    other = _output(ledger, "000660")

    assert _snapshot_hash(output) != _snapshot_hash(other)
    # 올바른 해시는 그대로 통과한다.
    request = _request(output)
    assert request["feature_snapshot_sha256"] == _snapshot_hash(output)


# ---------------------------------------------------------------------------
# provider 실패 → ABSTAIN, 원문 비유출
# ---------------------------------------------------------------------------


def test_transport_timeout_abstains_without_leaking_exception_text(ledger):
    output = _output(ledger)
    proposal = _propose(output, Recorder(exc=TimeoutError("DEADLINE-SECRET-42")))

    assert proposal["action"] == "ABSTAIN"
    assert proposal["abstain_reason"] == "MODEL_TIMEOUT"
    assert "DEADLINE-SECRET-42" not in json.dumps(proposal, ensure_ascii=False)


def test_transport_exception_abstains_without_leaking_exception_text(ledger):
    output = _output(ledger)
    proposal = _propose(output, Recorder(exc=RuntimeError("VENDOR-TOKEN-XYZ")))

    assert proposal["action"] == "ABSTAIN"
    assert proposal["abstain_reason"] == "MODEL_PROVIDER_ERROR"
    assert "VENDOR-TOKEN-XYZ" not in json.dumps(proposal, ensure_ascii=False)


@pytest.mark.parametrize(
    "raw",
    [
        "RAW-VENDOR-PAYLOAD not json {{{",
        None,
        b"raw-bytes",
        json.dumps([1, 2, 3]),
        {"refusal": "I cannot comply with this request."},
    ],
)
def test_malformed_or_refused_output_abstains_without_leaking_raw(ledger, raw):
    output = _output(ledger)
    proposal = _propose(output, Recorder(result=lambda req: raw))

    assert set(proposal) == EXPECTED_KEYS
    assert proposal["action"] == "ABSTAIN"
    assert proposal["abstain_reason"] == "MODEL_OUTPUT_INVALID"
    dumped = json.dumps(proposal, ensure_ascii=False)
    assert "RAW-VENDOR-PAYLOAD" not in dumped
    assert "cannot comply" not in dumped


# ---------------------------------------------------------------------------
# B7: 검증기 일관성 — 최종 검증기는 모든 결정론적 ABSTAIN 수용, 모델 위조는 거부
# ---------------------------------------------------------------------------


def test_model_cannot_masquerade_as_any_internal_abstain_reason(ledger):
    output = _output(ledger)
    for forged_reason in sorted(DATA_ABSTAIN_REASONS | MODEL_FAILURE_ABSTAIN_REASONS):
        transport = Recorder(result=lambda req, r=forged_reason: _abstain_payload(req, r))
        proposal = _propose(output, transport)
        assert proposal["action"] == "ABSTAIN"
        assert proposal["abstain_reason"] == "MODEL_OUTPUT_INVALID", forged_reason


def test_model_declared_no_edge_abstain_is_accepted(ledger):
    output = _output(ledger)
    transport = Recorder(result=lambda req: _abstain_payload(req, "NO_EDGE"))

    proposal = _propose(output, transport)

    assert proposal["action"] == "ABSTAIN"
    assert proposal["abstain_reason"] == "NO_EDGE"
    assert proposal["target_exposure_pct"] == 0.0
    assert proposal["confidence_bps"] == 0


def test_every_deterministic_abstain_round_trips_through_final_validator(ledger):
    request = _request(_output(ledger))

    for reason in sorted(ABSTAIN_REASONS):
        proposal = build_abstain_proposal(request, reason)
        validated = validate_proposal(proposal, request, mode="final")
        assert validated == proposal, reason


def test_internal_abstain_reasons_stay_rejected_in_model_mode(ledger):
    request = _request(_output(ledger))

    for reason in sorted(DATA_ABSTAIN_REASONS | MODEL_FAILURE_ABSTAIN_REASONS):
        proposal = build_abstain_proposal(request, reason)
        with pytest.raises(ValueError, match="allowlist"):
            validate_proposal(proposal, request, mode="model")


def test_propose_data_abstain_passes_final_validator(ledger):
    add_complete_inputs(ledger, "005930")
    ledger.execute("DELETE FROM ksf_normalized_features WHERE feature_name='close'")
    output = _build(ledger)

    proposal = _propose(output, Recorder(result=None))
    request = _request(output)

    assert proposal["abstain_reason"] == "DATA_MISSING_REQUIRED"
    assert validate_proposal(proposal, request, mode="final") == proposal


def test_propose_model_failure_abstain_passes_final_validator(ledger):
    output = _output(ledger)

    proposal = _propose(output, Recorder(exc=TimeoutError()))
    request = _request(output)

    assert proposal["abstain_reason"] == "MODEL_TIMEOUT"
    assert validate_proposal(proposal, request, mode="final") == proposal


def test_validate_proposal_rejects_unknown_mode(ledger):
    request = _request(_output(ledger))

    with pytest.raises(ValueError, match="mode"):
        validate_proposal(_valid_payload(request), request, mode="trusted")


# ---------------------------------------------------------------------------
# 텍스트 안전성: 비교/발명 금지 (B6: 숫자 토큰 전면 금지 포함)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "000660보다 더 낫습니다.",
        "SK하이닉스 대비 우월합니다.",
        "다른 종목보다 상대적으로 우수합니다.",
        "업종 평균 대비 우수합니다.",
        "This is better than peers.",
        "Compared with the market, it is stronger.",
    ],
)
def test_cross_symbol_or_peer_market_comparison_abstains(ledger, text):
    output = _output(ledger)
    request = _request(output)
    payload = _valid_payload(request, thesis=text)

    with pytest.raises(ValueError, match="cross-symbol|comparison"):
        validate_proposal(payload, request)

    proposal = _propose(output, Recorder(result=payload))
    assert proposal["action"] == "ABSTAIN"
    assert proposal["abstain_reason"] == "MODEL_OUTPUT_INVALID"


@pytest.mark.parametrize(
    "text",
    [
        "목표가는 90000원입니다.",
        "상승 확률 70%입니다.",
        "임의 점수는 8점입니다.",
        "target price is attractive",
    ],
)
def test_invented_target_price_probability_or_calculation_abstains(ledger, text):
    output = _output(ledger)
    request = _request(output)
    payload = _valid_payload(request)
    payload["invalidation_conditions"][0]["text"] = text

    with pytest.raises(ValueError, match="numeric advice|calculation"):
        validate_proposal(payload, request)

    proposal = _propose(output, Recorder(result=payload))
    assert proposal["action"] == "ABSTAIN"
    assert proposal["abstain_reason"] == "MODEL_OUTPUT_INVALID"


@pytest.mark.parametrize(
    "text",
    [
        "현재가는 999999원입니다.",
        "거래량은 123456주로 확인됩니다.",
        "수급이 +3 포인트 개선되었습니다.",
        "노출을 15.5 수준으로 조정합니다.",
        "１２３４５６원 규모의 흐름이 보입니다.",  # 전각 숫자
    ],
)
def test_unbound_numeric_tokens_in_thesis_abstain(ledger, text):
    output = _output(ledger)
    request = _request(output)
    payload = _valid_payload(request, thesis=text)

    with pytest.raises(ValueError, match="numeric"):
        validate_proposal(payload, request)

    proposal = _propose(output, Recorder(result=payload))
    assert proposal["abstain_reason"] == "MODEL_OUTPUT_INVALID"


def test_unbound_numeric_tokens_in_invalidation_prose_abstain(ledger):
    output = _output(ledger)
    request = _request(output)
    payload = _valid_payload(request)
    payload["invalidation_conditions"][0]["text"] = "외국인 순매수가 5000 아래로 내려가면 무효입니다."

    with pytest.raises(ValueError, match="numeric"):
        validate_proposal(payload, request)

    proposal = _propose(output, Recorder(result=payload))
    assert proposal["abstain_reason"] == "MODEL_OUTPUT_INVALID"


# ---------------------------------------------------------------------------
# 근거(evidence) 규칙 + B4 재사용(replay) 저항
# ---------------------------------------------------------------------------


def test_unknown_duplicate_and_empty_evidence_ids_abstain(ledger):
    output = _output(ledger)
    request = _request(output)
    anchor = request["evidence_catalog"][0]["id"]
    bad_lists = [
        ["paper_evidence_" + "0" * 24],  # unknown
        [anchor, anchor],  # duplicate
        [],  # empty
        [42],  # non-string
    ]
    for ids in bad_lists:
        payload = _valid_payload(request, evidence_ids=ids)
        with pytest.raises(ValueError, match="evidence"):
            validate_proposal(payload, request)
        proposal = _propose(output, Recorder(result=payload))
        assert proposal["abstain_reason"] == "MODEL_OUTPUT_INVALID"


def test_foreign_evidence_from_other_symbol_request_abstains(ledger):
    output = _output(ledger)
    other_output = _output(ledger, "000660")
    other_request = _request(other_output, _holdings("000660"))
    foreign_id = other_request["evidence_catalog"][0]["id"]

    request = _request(output)
    payload = _valid_payload(request, evidence_ids=[foreign_id])

    with pytest.raises(ValueError, match="evidence"):
        validate_proposal(payload, request)

    proposal = _propose(output, Recorder(result=payload))
    assert proposal["abstain_reason"] == "MODEL_OUTPUT_INVALID"


def test_evidence_identity_differs_across_decisions(ledger):
    output = _output(ledger)

    base = _request(output)
    other = _request(output, ksf_decision_id="decision_other")

    base_ids = {i["id"] for i in base["evidence_catalog"]}
    other_ids = {i["id"] for i in other["evidence_catalog"]}
    assert base_ids.isdisjoint(other_ids)
    assert base["proposal_request_id"] != other["proposal_request_id"]


def test_replayed_response_from_other_decision_fails_closed(ledger):
    output = _output(ledger)
    first = _request(output)
    captured = _valid_payload(first)
    assert validate_proposal(captured, first)["action"] == "ENTER"

    second = _request(output, ksf_decision_id="decision_replay_target")
    with pytest.raises(ValueError, match="evidence"):
        validate_proposal(captured, second)

    proposal = _propose(
        output, Recorder(result=captured), ksf_decision_id="decision_replay_target"
    )
    assert proposal["abstain_reason"] == "MODEL_OUTPUT_INVALID"


@pytest.mark.parametrize(
    "conditions",
    [
        [],  # 비어 있음
        ["문자열 조건"],  # 구조화되지 않은 프로즈
        [{"text": "인용 없는 조건"}],  # evidence_ids 누락
        [{"text": "빈 인용", "evidence_ids": []}],
        [{"text": "여분 키", "evidence_ids": ["X"], "note": "extra"}],
    ],
)
def test_uncited_or_malformed_invalidation_conditions_abstain(ledger, conditions):
    output = _output(ledger)
    request = _request(output)
    if conditions and isinstance(conditions[0], dict) and conditions[0].get("evidence_ids") == ["X"]:
        conditions[0]["evidence_ids"] = [request["evidence_catalog"][0]["id"]]
    payload = _valid_payload(request, invalidation_conditions=conditions)

    with pytest.raises(ValueError, match="invalidation"):
        validate_proposal(payload, request)

    proposal = _propose(output, Recorder(result=payload))
    assert proposal["abstain_reason"] == "MODEL_OUTPUT_INVALID"


# ---------------------------------------------------------------------------
# B9: data_gaps 구조/인용 규칙
# ---------------------------------------------------------------------------


def _unavailable_evidence(request, name="hbm_supply_tightness"):
    return next(
        i for i in request["evidence_catalog"]
        if i.get("feature_name") == name
    )


def test_valid_cited_data_gap_item_round_trips(ledger):
    output = _output(ledger)
    request = _request(output)
    hbm = _unavailable_evidence(request)
    gap = {
        "feature_name": hbm["feature_name"],
        "feature_group": hbm["feature_group"],
        "status": hbm["status"],
        "as_of": hbm["as_of"],
        # B6: 갭 텍스트는 인용 증거에서 결정론적으로 유도된 문안과 정확히 일치해야 한다.
        "text": f"{hbm['feature_name']} 데이터 상태가 {hbm['status']}입니다.",
        "evidence_ids": [hbm["id"]],
    }
    payload = _valid_payload(request, data_gaps=[gap])

    result = validate_proposal(payload, request)

    assert result["data_gaps"][0]["text"] == gap["text"]
    assert result["data_gaps"][0]["evidence_ids"] == [hbm["id"]]


@pytest.mark.parametrize(
    "make_gap",
    [
        lambda req: "문자열 갭",  # 구조화되지 않음
        lambda req: {"text": "키 누락", "evidence_ids": [_unavailable_evidence(req)["id"]]},
        lambda req: {  # 여분 키
            **{k: _unavailable_evidence(req)[k] for k in ("feature_name", "feature_group", "status", "as_of")},
            "text": "여분 키", "evidence_ids": [_unavailable_evidence(req)["id"]], "note": "x",
        },
        lambda req: {  # 이용 가능한 증거를 갭으로 인용
            "feature_name": "close", "feature_group": "price_volume", "status": "READY",
            "as_of": "2026-07-31T15:30:00+09:00", "text": "정상 입력을 갭으로 주장",
            "evidence_ids": [next(i["id"] for i in req["evidence_catalog"] if i.get("feature_name") == "close")],
        },
        lambda req: {  # 인용 증거와 필드 불일치
            **{k: _unavailable_evidence(req)[k] for k in ("feature_name", "feature_group", "as_of")},
            "status": "READY", "text": "상태 위조", "evidence_ids": [_unavailable_evidence(req)["id"]],
        },
    ],
)
def test_malformed_or_miscited_data_gaps_abstain(ledger, make_gap):
    output = _output(ledger)
    request = _request(output)
    payload = _valid_payload(request, data_gaps=[make_gap(request)])

    with pytest.raises(ValueError, match="data_gaps"):
        validate_proposal(payload, request)

    proposal = _propose(output, Recorder(result=payload))
    assert proposal["abstain_reason"] == "MODEL_OUTPUT_INVALID"


def test_empty_data_gaps_are_normalized_to_reflect_unavailable_evidence(ledger):
    output = _output(ledger)
    request = _request(output)
    payload = _valid_payload(request, data_gaps=[])

    result = validate_proposal(payload, request)

    names = {g["feature_name"] for g in result["data_gaps"]}
    assert "hbm_supply_tightness" in names


# ---------------------------------------------------------------------------
# action / range / type 의미론 + B9 정책 한도
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "over",
    [
        {"action": "BUY"},
        {"action": "enter"},
        {"target_exposure_pct": True},
        {"target_exposure_pct": float("nan")},
        {"target_exposure_pct": float("inf")},
        {"target_exposure_pct": -1},
        {"target_exposure_pct": 101},
        {"target_exposure_pct": "15"},
        {"confidence_bps": True},
        {"confidence_bps": 42.5},
        {"confidence_bps": -1},
        {"confidence_bps": 10001},
        {"thesis": ""},
        {"thesis": 5},
        {"abstain_reason": "NO_EDGE"},  # ENTER 인데 사유 존재
    ],
)
def test_invalid_action_range_or_type_abstains(ledger, over):
    output = _output(ledger)
    request = _request(output)
    payload = _valid_payload(request, **over)

    with pytest.raises(ValueError):
        validate_proposal(payload, request)

    proposal = _propose(output, Recorder(result=payload))
    assert proposal["action"] == "ABSTAIN"
    assert proposal["abstain_reason"] == "MODEL_OUTPUT_INVALID"


def test_target_exposure_above_policy_limit_abstains(ledger):
    output = _output(ledger)
    request = _request(output)
    payload = _valid_payload(request, target_exposure_pct=30.0)  # POLICY 25 초과

    with pytest.raises(ValueError, match="policy limit"):
        validate_proposal(payload, request)

    proposal = _propose(output, Recorder(result=payload))
    assert proposal["abstain_reason"] == "MODEL_OUTPUT_INVALID"


def test_target_exposure_at_policy_limit_is_accepted(ledger):
    output = _output(ledger)
    request = _request(output)
    payload = _valid_payload(request, target_exposure_pct=25.0)

    assert validate_proposal(payload, request)["target_exposure_pct"] == 25.0


@pytest.mark.parametrize(
    "over",
    [
        {"action": "HOLD", "target_exposure_pct": 12.0},  # HOLD 는 현재 노출 유지
        {"action": "ENTER", "target_exposure_pct": 10.0},  # 현재 노출 초과 필요
        {"action": "ENTER", "target_exposure_pct": 5.0},
        {"action": "REDUCE", "target_exposure_pct": 10.0},  # 현재보다 작아야 함
        {"action": "REDUCE", "target_exposure_pct": 0.0},  # 0 이면 EXIT
        {"action": "EXIT", "target_exposure_pct": 1.0},  # EXIT 은 0 이어야 함
        {"action": "ABSTAIN", "target_exposure_pct": 5.0, "abstain_reason": "NO_EDGE"},
        {"action": "ABSTAIN", "confidence_bps": 100, "target_exposure_pct": 0.0,
         "abstain_reason": "NO_EDGE"},
    ],
)
def test_action_semantics_inconsistent_with_holdings_abstains(ledger, over):
    output = _output(ledger)
    request = _request(output)  # current_exposure_pct == 10.0
    payload = _valid_payload(request, **over)

    with pytest.raises(ValueError):
        validate_proposal(payload, request)

    proposal = _propose(output, Recorder(result=payload))
    assert proposal["abstain_reason"] == "MODEL_OUTPUT_INVALID"


@pytest.mark.parametrize(
    "over",
    [
        {"action": "REDUCE", "target_exposure_pct": 4.0},
        {"action": "EXIT", "target_exposure_pct": 0.0},
    ],
)
def test_reduce_or_exit_without_position_abstains(ledger, over):
    output = _output(ledger)
    holdings = _flat_holdings()
    request = _request(output, holdings)
    payload = _valid_payload(request, **over)

    with pytest.raises(ValueError):
        validate_proposal(payload, request)

    proposal = _propose(output, Recorder(result=payload), holdings)
    assert proposal["abstain_reason"] == "MODEL_OUTPUT_INVALID"


@pytest.mark.parametrize(
    "over",
    [
        {"action": "HOLD", "target_exposure_pct": 10.0},
        {"action": "REDUCE", "target_exposure_pct": 4.0},
        {"action": "EXIT", "target_exposure_pct": 0.0},
        {"action": "ENTER", "target_exposure_pct": 15.0},
    ],
)
def test_valid_action_semantics_pass(ledger, over):
    output = _output(ledger)
    request = _request(output)
    payload = _valid_payload(request, **over)

    result = validate_proposal(payload, request)

    assert result["action"] == over["action"]
    assert result["target_exposure_pct"] == over["target_exposure_pct"]


def test_validate_proposal_does_not_mutate_caller_payload(ledger):
    request = _request(_output(ledger))
    payload = _valid_payload(request)
    original = deepcopy(payload)

    validate_proposal(payload, request)

    assert payload == original


# ---------------------------------------------------------------------------
# holdings / policy_limits 닫힌 스키마
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "holdings",
    [
        _holdings(broker_token="secret"),  # 여분 키
        {k: v for k, v in _holdings().items() if k != "total_equity_krw"},  # 키 누락
        _holdings(symbol="000660"),  # 다른 종목
        _holdings(cash_krw=-1),
        _holdings(cash_krw=float("nan")),
        _holdings(total_equity_krw=float("inf")),
        _holdings(quantity=True),
        _holdings(quantity=10.5),
        _holdings(quantity=-1),
        _holdings(cash_krw={"amount": 1}),  # 중첩 객체
        _holdings(market_value_krw="1000000"),
        _holdings(total_equity_krw=11_000_000),  # cash+mv != total
        _holdings(current_exposure_pct=50.0),  # 노출 불일치
        _holdings(quantity=0),  # 수량 0 인데 평가금액 > 0
        _holdings(cash_krw=0, market_value_krw=0, total_equity_krw=0,
                  quantity=0, current_exposure_pct=0.0),  # 자본 0
        "not-a-mapping",
    ],
)
def test_holdings_closed_schema_violations_raise(ledger, holdings):
    output = _output(ledger)

    with pytest.raises(ValueError):
        _request(output, holdings)


def test_holdings_rejects_callable_value(ledger):
    output = _output(ledger)

    with pytest.raises(ValueError):
        _request(output, _holdings(market_value_krw=lambda: 1_000_000))


@pytest.mark.parametrize(
    "policy",
    [
        "not-a-mapping",
        {},
        {"max_target_exposure_pct": 25.0, "extra": 1},
        {"max_position_pct": 25.0},
        {"max_target_exposure_pct": True},
        {"max_target_exposure_pct": float("nan")},
        {"max_target_exposure_pct": float("inf")},
        {"max_target_exposure_pct": 0},
        {"max_target_exposure_pct": -5},
        {"max_target_exposure_pct": 101},
        {"max_target_exposure_pct": "25"},
    ],
)
def test_policy_limits_closed_schema_violations_raise(ledger, policy):
    output = _output(ledger)

    with pytest.raises(ValueError, match="policy_limits"):
        _request(output, policy=policy)


def test_caller_inputs_are_not_mutated(ledger):
    output = _output(ledger)
    holdings = _holdings()
    policy = dict(POLICY)
    before_holdings = deepcopy(holdings)
    before_policy = deepcopy(policy)

    _request(output, holdings, policy)
    _propose(output, _valid_transport(), holdings, policy)

    assert holdings == before_holdings
    assert policy == before_policy


# ---------------------------------------------------------------------------
# 요청 계약: lineage 검증 + 결정론 + B5 깊은 불변성
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "over",
    [
        {"ksf_run_id": ""},
        {"ksf_decision_id": "  "},
        {"feature_snapshot_sha256": "XYZ"},
        {"feature_snapshot_sha256": "A" * 64},  # 대문자 hex 금지
        {"available_data_cutoff": "not-a-timestamp"},
        {"available_data_cutoff": "2026-07-31T16:00:00"},  # naive 금지
    ],
)
def test_invalid_lineage_inputs_raise(ledger, over):
    output = _output(ledger)

    with pytest.raises(ValueError):
        _request(output, **over)


def test_request_ids_and_hashes_are_deterministic(ledger):
    output = _output(ledger)

    first = _request(output)
    second = _request(output)

    assert first == second
    assert first["proposal_request_id"] == second["proposal_request_id"]
    assert [item["id"] for item in first["evidence_catalog"]] == [
        item["id"] for item in second["evidence_catalog"]
    ]

    different = _request(output, ksf_run_id="run_other")
    assert different["proposal_request_id"] != first["proposal_request_id"]


def test_request_nested_collections_are_frozen(ledger):
    request = _request(_output(ledger))

    assert isinstance(request["allowed_actions"], tuple)
    assert isinstance(request["proposal_keys"], tuple)
    assert isinstance(request["evidence_catalog"], tuple)
    with pytest.raises(TypeError):
        request["holdings"]["cash_krw"] = 0
    with pytest.raises(TypeError):
        request["policy_limits"]["max_target_exposure_pct"] = 99.0
    with pytest.raises(TypeError):
        request["evidence_catalog"][0]["id"] = "forged"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda req: req["allowed_actions"].append("BUY"),
        lambda req: req["proposal_keys"].append("order_id"),
        lambda req: req["holdings"].__setitem__("cash_krw", 0),
        lambda req: req["policy_limits"].__setitem__("max_target_exposure_pct", 100.0),
        lambda req: req["evidence_catalog"][0].__setitem__("id", "forged"),
        lambda req: req["evidence_catalog"].append({"id": "forged"}),
    ],
    ids=[
        "allowed_actions_append",
        "proposal_keys_append",
        "holdings_setitem",
        "policy_limits_setitem",
        "evidence_item_setitem",
        "evidence_catalog_append",
    ],
)
def test_nested_request_mutation_by_transport_fails_closed(ledger, mutate):
    output = _output(ledger)

    def evil(request):
        mutate(request)
        return json.dumps(_valid_payload(request), ensure_ascii=False)

    proposal = _propose(output, evil)

    assert proposal["action"] == "ABSTAIN"
    assert proposal["abstain_reason"] == "MODEL_PROVIDER_ERROR"


# ---------------------------------------------------------------------------
# 결정론적 ABSTAIN 생성기
# ---------------------------------------------------------------------------


def test_build_abstain_proposal_is_valid_for_every_allowlisted_reason(ledger):
    request = _request(_output(ledger))

    for reason in sorted(ABSTAIN_REASONS):
        proposal = build_abstain_proposal(request, reason)
        assert set(proposal) == EXPECTED_KEYS
        assert proposal["action"] == "ABSTAIN"
        assert proposal["abstain_reason"] == reason
        assert proposal["target_exposure_pct"] == 0.0
        assert proposal["confidence_bps"] == 0
        assert proposal["thesis"].strip()
        assert proposal["evidence_ids"]
        assert proposal["invalidation_conditions"][0]["evidence_ids"]
        assert all(set(g) == GAP_ITEM_KEYS for g in proposal["data_gaps"])
        assert proposal["symbol"] == request["symbol"]


def test_build_abstain_proposal_rejects_unlisted_reason(ledger):
    request = _request(_output(ledger))

    with pytest.raises(ValueError, match="allowlist"):
        build_abstain_proposal(request, "MADE_UP_REASON")


def test_propose_requires_callable_transport(ledger):
    output = _output(ledger)

    with pytest.raises(ValueError, match="callable"):
        _propose(output, transport=None)


# ---------------------------------------------------------------------------
# 재수정 B8/B2: 피처 상태 ↔ 표준 리스트/상태/스코어링 정합(FeatureEngine 유도 미러링)
# ---------------------------------------------------------------------------


RECONCILIATION_FORGERIES = [
    (
        "required_stale_omitted_from_stale_required",
        lambda o: _replace_feature(o, "price_volume", "close", status="STALE"),
    ),
    (
        "required_license_blocked_omitted_from_lists",
        lambda o: _replace_feature(
            o, "price_volume", "close",
            status="LICENSE_BLOCKED", value=None, missing_reason="license hold",
        ),
    ),
    (
        "required_missing_status_omitted_from_missing_required",
        lambda o: _replace_feature(
            o, "price_volume", "close",
            status="MISSING_REQUIRED", value=None, missing_reason="collector gap",
        ),
    ),
    (
        "optional_null_omitted_from_missing_optional",
        lambda o: _replace_feature(o, "fx_market", "usdkrw", value=None),
    ),
    (
        "optional_missing_status_omitted_from_missing_optional",
        lambda o: _replace_feature(
            o, "fx_market", "usdkrw",
            status="MISSING_OPTIONAL", value=None, missing_reason="vendor gap",
        ),
    ),
    (
        "available_feature_falsely_listed_missing_optional",
        lambda o: _forge(o, missing_optional=("usdkrw",)),
    ),
    (
        "available_feature_falsely_listed_stale_required",
        lambda o: _forge(o, stale_required=("close",), state="STALE_DATA", scoring_allowed=False),
    ),
    (
        "license_blocked_row_omitted_from_license_blocked",
        lambda o: _forge(o, license_blocked=()),
    ),
    (
        "state_forged_partial_without_missing_optional",
        lambda o: _forge(o, state="PARTIAL_DATA"),
    ),
]


@pytest.mark.parametrize(
    "forge",
    [f for _, f in RECONCILIATION_FORGERIES],
    ids=[n for n, _ in RECONCILIATION_FORGERIES],
)
def test_status_list_state_reconciliation_fails_closed_pre_model(ledger, forge):
    output = _output(ledger)
    forged = forge(output)
    transport = _valid_transport()

    proposal = _propose(forged, transport, feature_snapshot_sha256=_safe_hash(forged))

    assert transport.calls == []
    assert set(proposal) == EXPECTED_KEYS
    assert proposal["action"] == "ABSTAIN"
    assert proposal["abstain_reason"] == "DATA_SNAPSHOT_INVALID"


def test_consistent_license_blocked_required_snapshot_gets_specific_reason(ledger):
    # 리스트/상태가 표준 유도와 '일치'하는 스냅샷은 더 구체적인 데이터 사유를 받는다.
    output = _output(ledger)
    forged = _replace_feature(
        output, "price_volume", "close",
        status="LICENSE_BLOCKED", value=None, missing_reason="license hold",
    )
    forged = _forge(
        forged,
        state="MISSING_REQUIRED_DATA",
        scoring_allowed=False,
        missing_required=("close",),
        license_blocked=("close", "hbm_supply_tightness"),
    )
    transport = _valid_transport()

    proposal = _propose(forged, transport, feature_snapshot_sha256=_safe_hash(forged))

    assert transport.calls == []
    assert proposal["abstain_reason"] == "DATA_RESTRICTED_REQUIRED"


# ---------------------------------------------------------------------------
# 최종 B8: 그룹 라우팅/캡/정렬/기여도 표준 계약 — 해시 일관 위조도 fail-closed
# ---------------------------------------------------------------------------


def _assert_snapshot_invalid_without_transport(forged):
    transport = _valid_transport()
    proposal = _propose(forged, transport, feature_snapshot_sha256=_safe_hash(forged))
    assert transport.calls == []
    assert set(proposal) == EXPECTED_KEYS
    assert proposal["action"] == "ABSTAIN"
    assert proposal["abstain_reason"] == "DATA_SNAPSHOT_INVALID"


# 라우팅 검증이 유일한 판별점이 되도록 '투표하지 않는' 피처만 이동한다:
# 기여도/정렬/ID/해시는 이동 후에도 전부 표준과 일치하는 내부 일관 위조다.
ROUTING_FORGERIES = [
    ("close_moved_price_volume_to_event", "price_volume", "event", "close"),
    ("usdkrw_moved_fx_market_to_event", "fx_market", "event", "usdkrw"),
    ("event_days_moved_event_to_fx_market", "event", "fx_market", "earnings_event_days"),
]


@pytest.mark.parametrize(
    "from_group,to_group,feature_name",
    [f[1:] for f in ROUTING_FORGERIES],
    ids=[f[0] for f in ROUTING_FORGERIES],
)
def test_feature_moved_off_canonical_group_fails_closed(
    ledger, from_group, to_group, feature_name
):
    output = _output(ledger)
    forged = _move_feature(output, from_group, to_group, feature_name)
    # 이동한 피처는 투표하지 않으므로 두 그룹의 기여도는 표준값 그대로다.
    assert forged.groups[from_group].contribution_bps == output.groups[from_group].contribution_bps
    assert forged.groups[to_group].contribution_bps == output.groups[to_group].contribution_bps

    _assert_snapshot_invalid_without_transport(forged)


def test_unroutable_feature_present_in_any_group_fails_closed(ledger):
    # FeatureEngine 라우팅이 None 을 반환하는 이름/출처 조합은 어느 그룹에도
    # 존재할 수 없다(엔진은 해당 행을 그룹에서 제외한다).
    output = _output(ledger)
    stray = EngineFeature(
        name="mystery_metric",
        value=1.0,
        status="READY",
        source_as_of="2026-07-31T15:30:00+09:00",
        source_feature_group="unknown_context",
        unit=None,
        missing_reason=None,
    )
    forged = _append_feature(output, "event", stray)

    _assert_snapshot_invalid_without_transport(forged)


@pytest.mark.parametrize("group_name", GROUP_ORDER)
def test_noncanonical_group_cap_fails_closed_even_when_in_generic_range(ledger, group_name):
    # 이 경계는 프로덕션 기본 캡 구성(DEFAULT_GROUP_CAPS_BPS)을 고정한다:
    # 0..10000 범위 안의 커스텀 캡이라도 표준 기본값과 다르면 차단.
    output = _output(ledger)
    assert DEFAULT_GROUP_CAPS_BPS[group_name] != 9999
    forged = _replace_group(output, group_name, cap_bps=9999)

    _assert_snapshot_invalid_without_transport(forged)


@pytest.mark.parametrize("group_name", ["price_volume", "semiconductor_regime"])
def test_reversed_multi_feature_group_tuple_fails_closed(ledger, group_name):
    output = _output(ledger)
    features = output.groups[group_name].features
    assert len(features) >= 2
    forged = _replace_group(output, group_name, features=tuple(reversed(features)))
    # 같은 피처 집합이므로 기여도는 그대로 표준값이다.
    assert forged.groups[group_name].contribution_bps == output.groups[group_name].contribution_bps

    _assert_snapshot_invalid_without_transport(forged)


# 표준 기여도(요청 픽스처 기준): 수급 (1.0+0.5)/2=0.75→1, 가격 125 원시 투표,
# 메모리 900, FX/이벤트는 무투표 0. 캡 이내의 임의 기여도는 전부 차단되어야 한다.
CONTRIBUTION_TAMPERS = [
    ("return_vote_group_off_by_one", "price_volume", 125, 124),
    ("net_buy_vote_group_rounded_up", "domestic_investor_flow", 1, 2),
    ("no_vote_fx_context_forged_positive", "fx_market", 0, 100),
    ("no_vote_event_context_forged_negative", "event", 0, -1),
    ("return_vote_group_off_by_minus_one", "global_memory_price", 900, 899),
]


@pytest.mark.parametrize(
    "group_name,canonical_bps,forged_bps",
    [t[1:] for t in CONTRIBUTION_TAMPERS],
    ids=[t[0] for t in CONTRIBUTION_TAMPERS],
)
def test_in_range_contribution_tamper_fails_closed(ledger, group_name, canonical_bps, forged_bps):
    output = _output(ledger)
    assert output.groups[group_name].contribution_bps == canonical_bps
    assert abs(forged_bps) <= output.groups[group_name].cap_bps
    forged = _replace_group(output, group_name, contribution_bps=forged_bps)

    _assert_snapshot_invalid_without_transport(forged)


def test_engine_output_with_clipped_net_buy_vote_reaches_transport(ledger):
    # 극단 수급값은 엔진 표준 클리핑(값/1000 을 ±1000 으로 제한)을 거친다:
    # (1000.0 + 0.5) / 2 = 500.25 → 500. 실제 엔진 산출물은 transport 에 도달해야 한다.
    add_complete_inputs(ledger, "005930")
    add_feature(
        ledger, symbol="005930", name="foreigner_net_buy_quantity",
        group="alternate_flow_feed", value=5_000_000,
        source_as_of="2026-07-31T15:40:00+09:00", ingested_at="2026-07-31T15:50:00+09:00",
    )
    output = _build(ledger)
    assert output.groups["domestic_investor_flow"].contribution_bps == 500
    transport = _valid_transport()

    proposal = _propose(output, transport)

    assert len(transport.calls) == 1
    assert proposal["action"] == "ENTER"


def test_engine_output_with_capped_return_vote_reaches_transport(ledger):
    # 원시 리턴 투표가 그룹 캡을 넘으면 엔진은 캡(1500)으로 절단한다.
    add_complete_inputs(ledger, "005930")
    add_feature(
        ledger, symbol="005930", name="mu_one_day_return_bps",
        group="alternate_global_feed", value=5000,
        source_as_of="2026-07-31T15:40:00+09:00", ingested_at="2026-07-31T15:50:00+09:00",
    )
    output = _build(ledger)
    assert output.groups["global_memory_price"].contribution_bps == 1500
    transport = _valid_transport()

    proposal = _propose(output, transport)

    assert len(transport.calls) == 1
    assert proposal["action"] == "ENTER"


def test_engine_half_vote_average_uses_python_rounding_and_forgery_fails(ledger):
    # 투표 1.0, 0.0 → 평균 0.5 → 파이썬 round(0.5)=0 (round-half-even).
    add_complete_inputs(ledger, "005930")
    add_feature(
        ledger, symbol="005930", name="institution_total_net_buy_quantity",
        group="alternate_flow_feed", value=0,
        source_as_of="2026-07-31T15:40:00+09:00", ingested_at="2026-07-31T15:50:00+09:00",
    )
    output = _build(ledger)
    assert output.groups["domestic_investor_flow"].contribution_bps == 0
    transport = _valid_transport()
    assert _propose(output, transport)["action"] == "ENTER"
    assert len(transport.calls) == 1

    # 0.5 를 1 로 올림한 위조(naive rounding)는 표준 반올림과 달라 차단된다.
    forged = _replace_group(output, "domestic_investor_flow", contribution_bps=1)
    _assert_snapshot_invalid_without_transport(forged)


# 최종 B8/E: license_blocked 리스트는 그룹에 존재하는 LICENSE_BLOCKED 행과
# 정확히 1:1 로 대응해야 한다(그룹에서 관측 불가능한 이름은 보수적으로 차단).
LICENSE_LIST_FORGERIES = [
    (
        "license_blocked_names_unobservable_row",
        lambda o: _forge(o, license_blocked=("ghost_input", "hbm_supply_tightness")),
    ),
    (
        "license_blocked_list_empty_despite_blocked_row",
        lambda o: _forge(o, license_blocked=()),
    ),
    (
        "license_blocked_lists_extra_ready_row",
        lambda o: _forge(o, license_blocked=("close", "hbm_supply_tightness")),
    ),
    (
        "blocked_status_changed_but_list_kept",
        lambda o: _replace_feature(
            o, "ai_hbm_demand", "hbm_supply_tightness",
            status="MISSING_OPTIONAL", missing_reason="vendor gap",
        ),
    ),
]


@pytest.mark.parametrize(
    "forge",
    [f for _, f in LICENSE_LIST_FORGERIES],
    ids=[n for n, _ in LICENSE_LIST_FORGERIES],
)
def test_license_blocked_list_must_match_grouped_rows_exactly(ledger, forge):
    output = _output(ledger)
    forged = forge(output)

    _assert_snapshot_invalid_without_transport(forged)


# ---------------------------------------------------------------------------
# 재수정 B4: 요청 전체 문맥(holdings/policy 포함) 결속 — 통째 replay 차단
# ---------------------------------------------------------------------------


def test_request_exposes_binding_hash_in_every_evidence_body(ledger):
    request = _request(_output(ledger))

    binding = request["request_binding_sha256"]
    assert isinstance(binding, str) and len(binding) == 64
    assert set(binding) <= set("0123456789abcdef")
    for item in request["evidence_catalog"]:
        assert item["request_binding_sha256"] == binding


def test_captured_response_fails_when_only_holdings_change(ledger):
    output = _output(ledger)
    first = _request(output)
    captured = _valid_payload(first)
    assert validate_proposal(captured, first)["action"] == "ENTER"

    low_exposure = _holdings(
        cash_krw=9_500_000, quantity=5, market_value_krw=500_000, current_exposure_pct=5.0
    )
    second = _request(output, low_exposure)
    # ENTER 15% 는 두 번째 요청의 holdings/policy 만으로도 유효한 행동이지만,
    # 증거 결속이 요청 문맥 전체를 커버하므로 replay 는 실패해야 한다.
    assert second["request_binding_sha256"] != first["request_binding_sha256"]
    with pytest.raises(ValueError, match="evidence"):
        validate_proposal(captured, second)

    proposal = _propose(output, Recorder(result=captured), low_exposure)
    assert proposal["abstain_reason"] == "MODEL_OUTPUT_INVALID"


def test_captured_response_fails_when_only_policy_limits_change(ledger):
    output = _output(ledger)
    first = _request(output)
    captured = _valid_payload(first)
    assert validate_proposal(captured, first)["action"] == "ENTER"

    wider_policy = {"max_target_exposure_pct": 50.0}
    second = _request(output, policy=wider_policy)
    assert second["request_binding_sha256"] != first["request_binding_sha256"]
    with pytest.raises(ValueError, match="evidence"):
        validate_proposal(captured, second)

    proposal = _propose(output, Recorder(result=captured), policy=wider_policy)
    assert proposal["abstain_reason"] == "MODEL_OUTPUT_INVALID"


# ---------------------------------------------------------------------------
# 재수정 B6: 정성 프로즈는 요청이 공표한 표준 문안 허용목록과 정확히 일치
# ---------------------------------------------------------------------------


def test_request_publishes_frozen_canonical_prose_contract(ledger):
    request = _request(_output(ledger))

    theses = request["canonical_theses"]
    assert set(theses) == set(paper_agent.ACTIONS)
    for text in theses.values():
        assert isinstance(text, str) and text.strip()
        assert not any(ch.isdigit() for ch in text)
        assert "005930" not in text and "000660" not in text
    assert request["canonical_invalidation_text"].strip()
    assert request["canonical_abstain_invalidation_text"].strip()
    with pytest.raises(TypeError):
        theses["ENTER"] = "forged"


def test_fabricated_nonnumeric_thesis_claim_is_rejected(ledger):
    output = _output(ledger)
    request = _request(output)
    payload = _valid_payload(request, thesis="기관 수급이 강하고 추세가 개선되었습니다.")

    with pytest.raises(ValueError, match="canonical"):
        validate_proposal(payload, request)

    proposal = _propose(output, Recorder(result=payload))
    assert proposal["abstain_reason"] == "MODEL_OUTPUT_INVALID"


def test_canonical_thesis_for_wrong_action_is_rejected(ledger):
    output = _output(ledger)
    request = _request(output)
    payload = _valid_payload(request, thesis=_canonical_thesis(request, "EXIT"))  # action ENTER

    with pytest.raises(ValueError, match="canonical"):
        validate_proposal(payload, request)

    proposal = _propose(output, Recorder(result=payload))
    assert proposal["abstain_reason"] == "MODEL_OUTPUT_INVALID"


def test_noncanonical_invalidation_prose_is_rejected(ledger):
    output = _output(ledger)
    request = _request(output)
    payload = _valid_payload(request)
    payload["invalidation_conditions"][0]["text"] = "수급 상태가 악화되면 이 제안을 재검토합니다."

    with pytest.raises(ValueError, match="canonical"):
        validate_proposal(payload, request)

    proposal = _propose(output, Recorder(result=payload))
    assert proposal["abstain_reason"] == "MODEL_OUTPUT_INVALID"


def test_arbitrary_data_gap_prose_is_rejected(ledger):
    output = _output(ledger)
    request = _request(output)
    hbm = _unavailable_evidence(request)
    gap = {
        "feature_name": hbm["feature_name"],
        "feature_group": hbm["feature_group"],
        "status": hbm["status"],
        "as_of": hbm["as_of"],
        "text": "라이선스 제한으로 이 입력을 사용할 수 없습니다.",
        "evidence_ids": [hbm["id"]],
    }
    payload = _valid_payload(request, data_gaps=[gap])

    with pytest.raises(ValueError, match="data_gaps"):
        validate_proposal(payload, request)

    proposal = _propose(output, Recorder(result=payload))
    assert proposal["abstain_reason"] == "MODEL_OUTPUT_INVALID"


def test_lineage_anchor_alone_cannot_support_non_abstain_action(ledger):
    output = _output(ledger)
    request = _request(output)
    anchor = request["evidence_catalog"][0]["id"]
    payload = _valid_payload(request, evidence_ids=[anchor])

    with pytest.raises(ValueError, match="feature or group"):
        validate_proposal(payload, request)

    proposal = _propose(output, Recorder(result=payload))
    assert proposal["abstain_reason"] == "MODEL_OUTPUT_INVALID"


def test_provider_abstain_uses_canonical_contract(ledger):
    output = _output(ledger)
    request = _request(output)
    transport = Recorder(result=lambda req: _abstain_payload(req, "NO_EDGE"))

    proposal = _propose(output, transport)

    assert proposal["action"] == "ABSTAIN"
    assert proposal["abstain_reason"] == "NO_EDGE"
    assert proposal["thesis"] == request["canonical_theses"]["ABSTAIN"]
    assert (
        proposal["invalidation_conditions"][0]["text"]
        == request["canonical_abstain_invalidation_text"]
    )


def test_provider_abstain_with_arbitrary_prose_is_rejected(ledger):
    output = _output(ledger)
    request = _request(output)
    payload = _abstain_payload(request)
    payload["thesis"] = "판단 근거가 부족해 보류합니다."

    with pytest.raises(ValueError, match="canonical"):
        validate_proposal(payload, request)

    proposal = _propose(output, Recorder(result=payload))
    assert proposal["abstain_reason"] == "MODEL_OUTPUT_INVALID"


# ---------------------------------------------------------------------------
# 재수정 B3: 구조 무효 스냅샷도 실제 표준 해시만 증명(공급 해시 위조 차단)
# ---------------------------------------------------------------------------


def test_invalid_serializable_snapshot_never_attests_supplied_hash(ledger):
    output = _output(ledger)
    forged = _forge(output, state="PARTIAL_DATA")  # 직렬화 가능하지만 구조 무효
    actual = _snapshot_hash(forged)

    request = _request(forged, feature_snapshot_sha256="b" * 64)

    assert request["feature_snapshot_sha256"] == actual
    assert request["evidence_catalog"][0]["feature_snapshot_sha256"] == actual

    transport = _valid_transport()
    proposal = _propose(forged, transport, feature_snapshot_sha256="b" * 64)
    assert transport.calls == []
    assert proposal["abstain_reason"] == "DATA_SNAPSHOT_INVALID"


def test_unserializable_snapshot_uses_deterministic_digest_not_supplied_hash(ledger):
    forged = _drop_group(_output(ledger))
    with pytest.raises(KeyError):
        forged.stable_json()  # 전제: 표준 직렬화 자체가 불가능한 스냅샷

    first = _request(forged, feature_snapshot_sha256="b" * 64)
    second = _request(forged, feature_snapshot_sha256="c" * 64)

    digest = first["feature_snapshot_sha256"]
    assert digest == second["feature_snapshot_sha256"]
    assert digest not in ("b" * 64, "c" * 64)
    assert len(digest) == 64 and set(digest) <= set("0123456789abcdef")
    assert first["evidence_catalog"][0]["feature_snapshot_sha256"] == digest

    transport = _valid_transport()
    proposal = _propose(forged, transport, feature_snapshot_sha256="b" * 64)
    assert transport.calls == []
    assert proposal["abstain_reason"] == "DATA_SNAPSHOT_INVALID"


# ---------------------------------------------------------------------------
# 재수정 B2: KST(+09:00) 오프셋 의미론 교차 검증
# ---------------------------------------------------------------------------


_BAD_KST_TIMESTAMPS = {
    "as_of_kst": {
        "utc_z": "2026-07-31T07:10:00Z",
        "utc_offset": "2026-07-31T07:10:00+00:00",
        "naive": "2026-07-31T16:10:00",
        "malformed": "not-a-timestamp",
        "other_offset": "2026-07-31T12:40:00+05:30",
    },
    "available_data_cutoff": {
        "utc_z": "2026-07-31T07:00:00Z",
        "utc_offset": "2026-07-31T07:00:00+00:00",
        "naive": "2026-07-31T16:00:00",
        "malformed": "not-a-timestamp",
        "other_offset": "2026-07-31T12:30:00+05:30",
    },
    "feature_source_as_of": {
        "utc_z": "2026-07-31T06:30:00Z",
        "utc_offset": "2026-07-31T06:30:00+00:00",
        "naive": "2026-07-31T15:30:00",
        "malformed": "not-a-timestamp",
        "other_offset": "2026-07-31T12:00:00+05:30",
    },
}


def _forge_timestamp(output, field, ts):
    if field == "as_of_kst":
        return _forge(output, as_of_kst=ts)
    if field == "available_data_cutoff":
        return _forge(output, available_data_cutoff=ts)
    return _replace_feature(output, "price_volume", "close", source_as_of=ts)


@pytest.mark.parametrize("style", ("utc_z", "utc_offset", "naive", "malformed", "other_offset"))
@pytest.mark.parametrize("field", tuple(_BAD_KST_TIMESTAMPS))
def test_non_kst_timestamp_cross_product_fails_closed(ledger, field, style):
    # 동일 순간을 가리키는 UTC/타 오프셋 표기라도 KST(+09:00) 가 아니면 차단된다.
    output = _output(ledger)
    forged = _forge_timestamp(output, field, _BAD_KST_TIMESTAMPS[field][style])
    transport = _valid_transport()

    proposal = _propose(forged, transport, available_data_cutoff=CUTOFF)

    assert transport.calls == []
    assert set(proposal) == EXPECTED_KEYS
    assert proposal["abstain_reason"] == "DATA_CUTOFF_INCONSISTENT"


def test_matching_utc_cutoff_pair_still_fails_closed(ledger):
    # 요청 컷오프와 스냅샷 컷오프가 '서로 일치'하는 UTC 표기여도 KST 의미론 위반이다.
    utc = "2026-07-31T07:00:00+00:00"
    forged = _forge(_output(ledger), available_data_cutoff=utc)
    transport = _valid_transport()

    proposal = _propose(forged, transport, available_data_cutoff=utc)

    assert transport.calls == []
    assert proposal["abstain_reason"] == "DATA_CUTOFF_INCONSISTENT"


# ---------------------------------------------------------------------------
# 주문/브로커/네트워크 경로 부재
# ---------------------------------------------------------------------------


def test_proposal_contains_no_order_quantity_or_broker_instruction(ledger):
    output = _output(ledger)

    proposal = _propose(output, _valid_transport())

    dumped = json.dumps(proposal, ensure_ascii=False)
    for banned in ("order_id", "order_qty", "broker", "shares", "quantity", "주문"):
        assert banned not in dumped


def test_module_has_no_network_broker_ledger_or_sdk_dependency():
    source = inspect.getsource(paper_agent)
    for banned in (
        "socket", "urllib", "requests", "httpx", "aiohttp", "http.client",
        "pykis", "kis_", "paper_trading", "redis", "telegram", "subprocess",
        "langchain", "crewai", "autogen", "anthropic", "openai",
        "_validate_text_safety",  # ksf 내부(private) 심볼 재사용 금지
    ):
        assert banned not in source, f"forbidden dependency reference: {banned}"


# ---------------------------------------------------------------------------
# 최종 B8: 커스텀 서브클래스/상태 보존 컨테이너 신뢰 경계
# — 스냅샷은 FeatureEngine 이 실제로 방출하는 '정확한' 타입만 통과하고,
#   수용된 스냅샷은 정확히 1회 표준 복사되어 이후 모든 단계가 복사본만 읽는다.
# ---------------------------------------------------------------------------


class _LyingStr(str):
    """직렬화는 실제 값 그대로, 동등 비교만 pretend 값으로 거짓말하는 str."""

    def __new__(cls, actual, pretend):
        self = super().__new__(cls, actual)
        self.pretend = pretend
        return self

    def __eq__(self, other):
        if isinstance(other, str) and str.__eq__(self.pretend, str(other)) is True:
            return True
        return str.__eq__(self, other)

    def __ne__(self, other):
        result = self.__eq__(other)
        return NotImplemented if result is NotImplemented else not result

    __hash__ = str.__hash__


class _LyingInt(int):
    """직렬화는 실제 값, 비교는 pretend 표준값과 같다고 주장하는 int."""

    def __new__(cls, actual, pretend):
        self = super().__new__(cls, actual)
        self.pretend = pretend
        return self

    def __eq__(self, other):
        return int.__eq__(int(self), other) is True or int.__eq__(self.pretend, other) is True

    def __ne__(self, other):
        return not self.__eq__(other)

    __hash__ = int.__hash__


class _LyingFloat(float):
    """나눗셈 결과를 부풀려 경계의 표준 재계산을 속이는 float."""

    def __truediv__(self, other):
        return float(float.__truediv__(self, other)) * 2000.0


class _LyingTuple(tuple):
    """실제 내용과 무관하게 어떤 tuple 과도 같다고 주장하는 tuple."""

    def __eq__(self, other):
        return isinstance(other, tuple)

    def __ne__(self, other):
        return not self.__eq__(other)

    __hash__ = tuple.__hash__


class _SneakyGroups(dict):
    """Mapping 계약만 흉내 내는 비-mappingproxy 컨테이너."""


class _ShiftyGroups(MappingABC):
    """arm() 이후 특정 키의 늦은(재)조회에서만 다른 그룹을 돌려주는 상태 보존 매핑."""

    def __init__(self, genuine, shifty_key, late_group, threshold):
        self._genuine = dict(genuine)
        self._shifty_key = shifty_key
        self._late_group = late_group
        self._threshold = threshold
        self._armed = False
        self._reads = 0

    def arm(self):
        self._armed = True

    def __getitem__(self, key):
        if self._armed and key == self._shifty_key:
            self._reads += 1
            if self._reads >= self._threshold:
                return self._late_group
        return self._genuine[key]

    def __iter__(self):
        return iter(self._genuine)

    def __len__(self):
        return len(self._genuine)


class _EvilGroup(FeatureGroupOutput):
    pass


class _EvilFeature(EngineFeature):
    pass


def _swap_group_object(output, group_name, new_group):
    groups = dict(output.groups)
    groups[group_name] = new_group
    forged = replace(output, groups=MappingProxyType(groups))
    return replace(forged, output_id=_recomputed_output_id(forged))


def test_lying_str_subclass_source_group_cannot_smuggle_routing(ledger):
    # 직렬화된 source_feature_group 은 "event"(엔진 의미론상 event 그룹 행)지만,
    # 비교 연산은 domestic_investor_flow 라고 거짓말하는 str 서브클래스.
    # 정렬/기여도/output_id/해시가 전부 표준과 일치하는 내부 일관 위조다.
    output = _output(ledger)
    stray = EngineFeature(
        name="mystery_flow",
        value=1.0,
        status="READY",
        source_as_of="2026-07-31T15:30:00+09:00",
        source_feature_group=_LyingStr("event", "domestic_investor_flow"),
        unit=None,
        missing_reason=None,
    )
    forged = _append_feature(output, "domestic_investor_flow", stray)
    assert forged.groups["domestic_investor_flow"].contribution_bps == 1  # 무투표 유지

    _assert_snapshot_invalid_without_transport(forged)


def test_lying_int_subclass_contribution_cannot_forge_group_vote(ledger):
    output = _output(ledger)
    canonical = output.groups["price_volume"].contribution_bps
    assert canonical == 125
    forged = _replace_group(
        output, "price_volume", contribution_bps=_LyingInt(999, canonical)
    )

    _assert_snapshot_invalid_without_transport(forged)


def test_lying_float_subclass_arithmetic_cannot_forge_contribution(ledger):
    # 기저 값 1000.0 은 표준 산술로 기여도 1 이지만, 왜곡된 __truediv__ 로
    # 경계의 재계산만 500 이 되도록 만든 위조(직렬화 값은 그대로 1000.0).
    output = _output(ledger)
    forged = _replace_feature(
        output, "domestic_investor_flow", "foreigner_net_buy_quantity",
        value=_LyingFloat(1000.0),
    )
    forged = _replace_group(forged, "domestic_investor_flow", contribution_bps=500)

    _assert_snapshot_invalid_without_transport(forged)


def test_lying_tuple_subclass_availability_list_fails_closed(ledger):
    # 표준 유도와 '같다'고 거짓말하면서 유령 항목을 직렬화하는 tuple 서브클래스.
    output = _output(ledger)
    forged = _forge(output, missing_optional=_LyingTuple(("ghost_optional",)))

    _assert_snapshot_invalid_without_transport(forged)


def test_tuple_subclass_group_features_fails_closed(ledger):
    output = _output(ledger)
    features = output.groups["price_volume"].features
    forged = _replace_group(output, "price_volume", features=_LyingTuple(features))

    _assert_snapshot_invalid_without_transport(forged)


def test_non_mappingproxy_groups_container_fails_closed(ledger):
    # 내용이 전부 진품이어도 groups 가 FeatureEngine 이 방출하는 정확한
    # MappingProxyType 이 아니면(behavioral Mapping 여지) 경계를 넘을 수 없다.
    output = _output(ledger)
    forged = replace(output, groups=_SneakyGroups(dict(output.groups)))
    forged = replace(forged, output_id=_recomputed_output_id(forged))

    _assert_snapshot_invalid_without_transport(forged)


def test_stateful_groups_mapping_cannot_desync_validation_and_evidence(ledger):
    # 검증/해시 조회에는 진품을, 그 이후의 늦은 재조회에만 위조 그룹을 돌려주는
    # 상태 보존 매핑: 정규화 1회 복사 없이는 증거가 해시와 어긋난 채 전송된다.
    output = _output(ledger)
    late_group = replace(
        output.groups["price_volume"],
        features=tuple(
            replace(f, value=999999.0) if f.name == "close" else f
            for f in output.groups["price_volume"].features
        ),
    )
    shifty = _ShiftyGroups(dict(output.groups), "price_volume", late_group, threshold=5)
    snapshot = replace(output, groups=shifty)
    snapshot = replace(snapshot, output_id=_recomputed_output_id(snapshot))
    supplied = _safe_hash(snapshot)
    shifty.arm()
    transport = _valid_transport()

    proposal = propose(
        snapshot,
        _holdings(),
        policy_limits=dict(POLICY),
        model_provider="offline-test",
        model_name="fixture-model",
        transport=transport,
        ksf_run_id=RUN_ID,
        ksf_decision_id=DECISION_ID,
        feature_snapshot_sha256=supplied,
        available_data_cutoff=snapshot.available_data_cutoff,
    )

    assert transport.calls == []
    assert set(proposal) == EXPECTED_KEYS
    assert proposal["action"] == "ABSTAIN"
    assert proposal["abstain_reason"] == "DATA_SNAPSHOT_INVALID"


def test_symbol_output_subclass_overriding_stable_json_fails_closed(ledger):
    # 실제 필드는 변조(close=99999.0)됐지만 stable_json 오버라이드가 '진품'
    # 직렬화를 보고해 해시 검증을 통과시키려는 SymbolFeatureOutput 서브클래스.
    output = _output(ledger)
    tampered = _replace_feature(output, "price_volume", "close", value=99999.0)
    genuine_json = output.stable_json()

    class EvilOutput(SymbolFeatureOutput):
        def stable_json(self):
            return genuine_json

    evil = EvilOutput(
        symbol=tampered.symbol,
        state=tampered.state,
        scoring_allowed=tampered.scoring_allowed,
        available_data_cutoff=tampered.available_data_cutoff,
        as_of_kst=tampered.as_of_kst,
        groups=tampered.groups,
        missing_required=tampered.missing_required,
        missing_optional=tampered.missing_optional,
        stale_required=tampered.stale_required,
        license_blocked=tampered.license_blocked,
        output_id=tampered.output_id,
    )
    supplied = hashlib.sha256(genuine_json.encode("utf-8")).hexdigest()
    transport = _valid_transport()

    proposal = _propose(evil, transport, feature_snapshot_sha256=supplied)

    assert transport.calls == []
    assert proposal["action"] == "ABSTAIN"
    assert proposal["abstain_reason"] == "DATA_SNAPSHOT_INVALID"
    # 거부된 기만 스냅샷은 공격자가 공급한 해시를 절대 증명하지 않는다.
    request = _request(evil, feature_snapshot_sha256=supplied)
    assert request["feature_snapshot_sha256"] != supplied


def test_feature_group_output_subclass_fails_closed(ledger):
    output = _output(ledger)
    group = output.groups["event"]
    evil = _EvilGroup(group.name, group.cap_bps, group.contribution_bps, group.features)
    forged = _swap_group_object(output, "event", evil)

    _assert_snapshot_invalid_without_transport(forged)


def test_engine_feature_subclass_fails_closed(ledger):
    output = _output(ledger)
    group = output.groups["price_volume"]
    close = next(f for f in group.features if f.name == "close")
    evil = _EvilFeature(
        name=close.name,
        value=close.value,
        status=close.status,
        source_as_of=close.source_as_of,
        source_feature_group=close.source_feature_group,
        unit=close.unit,
        missing_reason=close.missing_reason,
    )
    features = tuple(
        sorted(
            (evil,) + tuple(f for f in group.features if f.name != "close"),
            key=lambda f: f.name,
        )
    )
    forged = _swap_group_object(output, "price_volume", replace(group, features=features))

    _assert_snapshot_invalid_without_transport(forged)


def test_lying_str_symbol_cannot_be_read_and_fails_closed_deterministically(ledger):
    # 최상위 symbol 이 기만 str 서브클래스여도 예외가 아니라 결정론적
    # DATA_SNAPSHOT_INVALID ABSTAIN 으로 닫힌다: 하부 불변 문자 버퍼에서
    # 지원 심볼("005930")을 정확한 내장 str 로 안전 복원해 요청/제안 계보에
    # 쓰고, transport 는 호출되지 않으며, 공급 해시는 절대 증명하지 않는다.
    output = _output(ledger)
    forged = replace(output, symbol=_LyingStr("005930", "000660"))
    transport = _valid_transport()
    supplied = _safe_hash(forged)

    proposal = _propose(forged, transport, feature_snapshot_sha256=supplied)

    assert transport.calls == []
    assert set(proposal) == EXPECTED_KEYS
    assert proposal["action"] == "ABSTAIN"
    assert proposal["abstain_reason"] == "DATA_SNAPSHOT_INVALID"
    assert type(proposal["symbol"]) is str
    assert proposal["symbol"] == "005930"

    request = _request(forged, feature_snapshot_sha256=supplied)
    assert type(request["symbol"]) is str
    assert request["symbol"] == "005930"
    # 거부된 기만 스냅샷은 공급 해시가 아닌 결정론적 무효 다이제스트만 증명한다.
    digest = request["feature_snapshot_sha256"]
    assert digest != supplied
    assert len(digest) == 64 and set(digest) <= set("0123456789abcdef")
    assert len(request["evidence_catalog"]) == 1
    assert request["evidence_catalog"][0]["feature_snapshot_sha256"] == digest


def test_rejected_deceptive_snapshot_attests_deterministic_sentinel_not_supplied(ledger):
    output = _output(ledger)
    forged = _forge(output, missing_optional=_LyingTuple(("ghost_optional",)))

    first = _request(forged, feature_snapshot_sha256="b" * 64)
    second = _request(forged, feature_snapshot_sha256="c" * 64)

    digest = first["feature_snapshot_sha256"]
    assert digest == second["feature_snapshot_sha256"]
    assert digest not in ("b" * 64, "c" * 64)
    assert len(digest) == 64 and set(digest) <= set("0123456789abcdef")
    assert first["evidence_catalog"][0]["feature_snapshot_sha256"] == digest
    # 기만 스냅샷의 내용은 증거로 노출되지 않는다(계보 앵커만 존재).
    assert len(first["evidence_catalog"]) == 1


def test_mappingproxy_over_retained_dict_is_isolated_after_request_build(ledger):
    # 정확한 MappingProxyType 이더라도 하부 dict 참조를 보유한 채 사후 변조하는
    # 변형: 요청이 이미 만든 표준 복사본은 격리되어 있어야 한다.
    output = _output(ledger)
    retained = dict(output.groups)
    snapshot = replace(output, groups=MappingProxyType(retained))
    snapshot = replace(snapshot, output_id=_recomputed_output_id(snapshot))

    request = _request(snapshot)
    close = next(i for i in request["evidence_catalog"] if i.get("feature_name") == "close")
    assert close["value"] == 70000
    assert request["feature_snapshot_sha256"] == _snapshot_hash(output)

    retained["price_volume"] = replace(retained["price_volume"], contribution_bps=2500)

    close_after = next(
        i for i in request["evidence_catalog"] if i.get("feature_name") == "close"
    )
    assert close_after["value"] == 70000
    assert request["feature_snapshot_sha256"] == _snapshot_hash(output)

    # 변조 이후의 스냅샷은 표준 유도와 어긋나므로 새 요청은 abstain 한다.
    transport = _valid_transport()
    proposal = _propose(snapshot, transport, feature_snapshot_sha256=_safe_hash(snapshot))
    assert transport.calls == []
    assert proposal["abstain_reason"] == "DATA_SNAPSHOT_INVALID"


def test_genuine_engine_output_request_hash_and_evidence_unchanged_by_boundary(ledger):
    # 진품 FeatureEngine 산출물은 정규화 복사 이후에도 요청/증거/해시가
    # 표준 그대로여야 하며 transport 에 정확히 1회 도달한다.
    output = _output(ledger)
    transport = _valid_transport()

    proposal = _propose(output, transport)

    assert proposal["action"] == "ENTER"
    assert len(transport.calls) == 1
    sent = transport.calls[0]
    assert sent["feature_snapshot_sha256"] == _snapshot_hash(output)
    assert sent["feature_output_id"] == output.output_id
    assert sent["feature_state"] == output.state
    assert sent["scoring_allowed"] is True
    close = next(i for i in sent["evidence_catalog"] if i.get("feature_name") == "close")
    assert close["value"] == 70000
    rebuilt = _request(output)
    assert rebuilt["proposal_request_id"] == sent["proposal_request_id"]
    assert [i["id"] for i in rebuilt["evidence_catalog"]] == [
        i["id"] for i in sent["evidence_catalog"]
    ]
