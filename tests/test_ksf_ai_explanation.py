from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from dataclasses import replace

import pytest

from ksf.ai_explanation import (
    MODEL_POLICY_VERSION,
    PROMPT_TEMPLATE_VERSION,
    build_explanation_request,
    build_fallback_explanation,
    insert_ai_request,
    normalize_explanation,
    validate_explanation,
)
from ksf.feature_engine import FeatureEngine
from ksf.global_collector import init_ledger_schema, stable_id
from tests.test_ksf_feature_engine import AS_OF, CUTOFF, add_complete_inputs


@pytest.fixture
def ledger():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_ledger_schema(conn)
    return conn


def _output(ledger, symbol="005930"):
    add_complete_inputs(ledger, symbol)
    return FeatureEngine(ledger).build_symbol(
        symbol, available_data_cutoff=CUTOFF, as_of_kst=AS_OF
    )


def _request(output):
    return build_explanation_request(
        output,
        model_provider="offline-test",
        model_name="fixture-model",
        as_of=AS_OF,
        cutoff=CUTOFF,
    )


def _valid_payload(request):
    link = request["source_catalog"][0]
    return {
        "symbol": request["symbol"],
        "response_schema_version": request["response_schema_version"],
        "prompt_template_version": request["prompt_template_version"],
        "model_policy_version": request["model_policy_version"],
        "model_provider": request["model_provider"],
        "model_name": request["model_name"],
        "summary": "단일 종목의 원장 근거를 요약했습니다.",
        "positive_evidence": [{"text": "입력 신호가 확인됩니다.", "source_link_ids": [link["id"]]}],
        "negative_evidence": [{"text": "반대 방향 위험도 남아 있습니다.", "source_link_ids": [link["id"]]}],
        "uncertainty": [{"text": "관측 범위가 제한적입니다.", "source_link_ids": [link["id"]]}],
        "data_gaps": [],
        "invalidation_conditions": [{"text": "해당 입력 상태가 바뀌면 판단을 무효화합니다.", "source_link_ids": [link["id"]]}],
        "fallback_copy": None,
        "source_links": [link],
        "not_investment_advice": "개인용 리서치 보조 정보이며 투자 조언이나 주문 지시가 아닙니다.",
    }


def test_valid_single_symbol_explanation_has_feature_source_links(ledger):
    request = _request(_output(ledger))
    result = validate_explanation(_valid_payload(request), request)

    assert result["symbol"] == "005930"
    assert result["source_links"][0]["feature_name"]
    assert result["source_links"][0]["feature_group"]
    assert result["source_links"][0]["as_of"]


@pytest.mark.parametrize("raw", ["not-json", {"refusal": "cannot comply"}, None])
def test_malformed_or_refused_output_gets_valid_safe_korean_fallback(ledger, raw):
    request = _request(_output(ledger))
    result = normalize_explanation(raw, request)

    assert result == validate_explanation(result, request)
    assert "검증" in result["fallback_copy"] or "사용" in result["fallback_copy"]
    assert "투자 조언" in result["not_investment_advice"]
    assert not any(word in json.dumps(result, ensure_ascii=False) for word in ("목표가", "확률", "매수", "매도"))


@pytest.mark.parametrize(
    "text",
    ["000660보다 더 낫습니다.", "SK하이닉스 대비 우월합니다.", "삼성전자와 SK하이닉스를 비교합니다."],
)
def test_cross_symbol_or_relative_comparison_is_rejected(ledger, text):
    request = _request(_output(ledger, "005930"))
    payload = _valid_payload(request)
    payload["summary"] = text
    with pytest.raises(ValueError, match="cross-symbol|comparison"):
        validate_explanation(payload, request)


@pytest.mark.parametrize(
    "text",
    [
        "다른 종목보다 상대적으로 우수합니다.",
        "타 종목보다 흐름이 강합니다.",
        "상대적으로 양호합니다.",
        "업종 평균 대비 우수합니다.",
        "다른 종목과 비교하면 안정적입니다.",
        "This is better than peers.",
        "The signal is strong relative to peers.",
        "Peer performance is weaker.",
    ],
)
def test_generic_relative_or_peer_comparison_is_rejected(ledger, text):
    request = _request(_output(ledger, "005930"))
    payload = _valid_payload(request)
    payload["summary"] = text

    with pytest.raises(ValueError, match="cross-symbol|comparison"):
        validate_explanation(payload, request)


@pytest.mark.parametrize(
    "text",
    [
        "두 종목 중 더 우수합니다.",
        "업종 평균보다 강합니다.",
        "Compared with the market, it is stronger.",
        "시장보다 강합니다.",
        "sector average보다 좋습니다.",
        "market comparison is favorable.",
    ],
)
def test_market_sector_or_multi_stock_comparison_is_rejected(ledger, text):
    request = _request(_output(ledger, "005930"))
    payload = _valid_payload(request)
    payload["summary"] = text

    with pytest.raises(ValueError, match="cross-symbol|comparison"):
        validate_explanation(payload, request)


@pytest.mark.parametrize(
    "text",
    [
        "stronger than the market",
        "outperforms the sector",
        "relative to the market",
        "relative to the sector",
        "better than the sector average",
        "두 회사 가운데 더 우수합니다",
        "업종 내 상위입니다",
    ],
)
def test_generic_market_sector_or_company_comparison_is_rejected(ledger, text):
    request = _request(_output(ledger, "005930"))
    payload = _valid_payload(request)
    payload["summary"] = text

    with pytest.raises(ValueError, match="cross-symbol|comparison"):
        validate_explanation(payload, request)


def test_model_authored_response_requires_non_empty_substantive_sections(ledger):
    request = _request(_output(ledger))
    payload = _valid_payload(request)
    payload.update({
        "positive_evidence": [],
        "negative_evidence": [],
        "uncertainty": [],
        "invalidation_conditions": [],
        "fallback_copy": None,
    })

    with pytest.raises(ValueError, match="substantive|non-empty"):
        validate_explanation(payload, request)


def test_model_cannot_disguise_success_as_fallback_to_skip_evidence(ledger):
    request = _request(_output(ledger))
    payload = _valid_payload(request)
    payload.update({
        "summary": "단일 종목 설명인데 fallback_copy만 채웁니다.",
        "positive_evidence": [],
        "negative_evidence": [],
        "fallback_copy": "임의 대체 문구",
    })

    with pytest.raises(ValueError, match="fallback|substantive|non-empty"):
        validate_explanation(payload, request)


@pytest.mark.parametrize("text", ["더 우수합니다", "더 강합니다", "superior signal"])
def test_bare_relative_quality_claims_are_rejected(ledger, text):
    request = _request(_output(ledger))
    payload = _valid_payload(request)
    payload["summary"] = text

    with pytest.raises(ValueError, match="cross-symbol|comparison"):
        validate_explanation(payload, request)


@pytest.mark.parametrize("text", ["목표가는 90000원입니다.", "상승 확률 70%입니다.", "임의 점수는 8점입니다."])
def test_invented_target_probability_or_calculation_is_rejected(ledger, text):
    request = _request(_output(ledger))
    payload = _valid_payload(request)
    payload["summary"] = text
    with pytest.raises(ValueError, match="numeric advice|calculation"):
        validate_explanation(payload, request)


def test_request_versions_hashes_and_sqlite_audit_logging(ledger):
    output = _output(ledger)
    request = _request(output)
    run_id = stable_id("ai_test_run", {"symbol": output.symbol})
    ledger.execute(
        """INSERT INTO ksf_runs
        (run_id, symbol, trading_date, run_status, as_of_kst, available_data_cutoff,
         scoring_ruleset_version) VALUES (?, ?, '2026-07-31', 'READY', ?, ?, 'v1')""",
        (run_id, output.symbol, AS_OF, CUTOFF),
    )
    insert_ai_request(ledger, request, run_id=run_id)
    row = ledger.execute("SELECT * FROM ksf_ai_requests WHERE ai_request_id=?", (request["ai_request_id"],)).fetchone()

    assert request["prompt_template_version"] == PROMPT_TEMPLATE_VERSION
    assert request["model_policy_version"] == MODEL_POLICY_VERSION
    assert len(request["prompt_hash_sha256"]) == len(request["input_ledger_hash_sha256"]) == 64
    assert (row["symbol"], row["model_provider"], row["model_name"]) == ("005930", "offline-test", "fixture-model")
    assert row["prompt_template_version"] == PROMPT_TEMPLATE_VERSION
    assert json.loads(row["request_metadata_json"])["model_policy_version"] == MODEL_POLICY_VERSION


def test_unavailable_features_are_forced_into_gaps_uncertainty_and_invalidation(ledger):
    output = _output(ledger)
    ledger.execute("DELETE FROM ksf_normalized_features WHERE symbol='005930' AND feature_name='volume'")
    ledger.execute("UPDATE ksf_normalized_features SET feature_status='STALE' WHERE feature_name='close'")
    output = FeatureEngine(ledger).build_symbol("005930", available_data_cutoff=CUTOFF, as_of_kst=AS_OF)
    request = _request(output)
    payload = _valid_payload(request)

    result = validate_explanation(payload, request)
    gap_names = {item["feature_name"] for item in result["data_gaps"]}
    assert {"volume", "close", "hbm_supply_tightness"} <= gap_names
    assert result["uncertainty"]
    assert result["invalidation_conditions"]


def test_source_catalog_does_not_duplicate_known_missing_required_feature(ledger):
    output = _output(ledger)
    output = replace(output, missing_required=("close", "future_required_feature"))

    request = _request(output)

    assert [link["feature_name"] for link in request["source_catalog"]].count("close") == 1
    assert any(link["feature_name"] == "future_required_feature" for link in request["source_catalog"])


def test_unknown_symbol_fails_closed_with_clear_value_error(ledger):
    request = _request(_output(ledger))
    request["symbol"] = "future-symbol"
    payload = _valid_payload(request)

    with pytest.raises(ValueError, match="supported symbol|identity policy"):
        validate_explanation(payload, request)


def test_fallback_with_empty_source_catalog_fails_closed_clearly(ledger):
    request = _request(_output(ledger))
    request["source_catalog"] = []

    with pytest.raises(ValueError, match="source_catalog.*non-empty"):
        build_fallback_explanation(request)


def test_validation_does_not_mutate_caller_nested_lists(ledger):
    output = _output(ledger)
    ledger.execute("UPDATE ksf_normalized_features SET feature_status='STALE' WHERE feature_name='close'")
    output = FeatureEngine(ledger).build_symbol("005930", available_data_cutoff=CUTOFF, as_of_kst=AS_OF)
    request = _request(output)
    payload = _valid_payload(request)
    original = deepcopy(payload)

    validate_explanation(payload, request)

    assert payload == original


def test_model_authored_data_gap_is_normalized_to_consistent_schema(ledger):
    request = _request(_output(ledger))
    payload = _valid_payload(request)
    link = request["source_catalog"][0]
    payload["data_gaps"] = [{"text": "추가 확인이 필요합니다.", "source_link_ids": [link["id"]]}]

    result = validate_explanation(payload, request)

    assert result["data_gaps"][0] == {
        "feature_name": link["feature_name"],
        "feature_group": link["feature_group"],
        "status": link["status"],
        "as_of": link["as_of"],
        "text": "추가 확인이 필요합니다.",
        "source_link_ids": [link["id"]],
    }


@pytest.mark.parametrize("section", ["positive_evidence", "data_gaps"])
def test_citation_must_resolve_in_response_source_links(ledger, section):
    request = _request(_output(ledger))
    payload = _valid_payload(request)
    omitted_link = request["source_catalog"][1]
    payload[section] = [
        {"text": "응답 내부에 없는 출처를 인용합니다.", "source_link_ids": [omitted_link["id"]]}
    ]

    with pytest.raises(ValueError, match="response source_links"):
        validate_explanation(payload, request)
