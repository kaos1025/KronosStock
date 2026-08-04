"""Offline-safe, per-symbol AI explanation boundary for KSF.

This module prepares deterministic model inputs and validates model outputs.  It
does not contain a model, network, broker, order, or external-write client.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from copy import deepcopy
from typing import Any, Mapping

from ksf.feature_engine import GROUP_ORDER, SUPPORTED_SYMBOLS, SymbolFeatureOutput
from ksf.global_collector import stable_id, stable_json


RESPONSE_SCHEMA_VERSION = "ksf-ai-explanation-v1"
PROMPT_TEMPLATE_VERSION = "ksf-ai-explanation-prompt-v1"
MODEL_POLICY_VERSION = "ksf-ai-single-symbol-no-invention-v1"
REDACTION_POLICY_VERSION = "ksf-ai-redacted-preview-v1"
NOT_INVESTMENT_ADVICE = "개인용 리서치 보조 정보이며 투자 조언이나 주문 지시가 아닙니다."
FALLBACK_SUMMARY = "설명 데이터를 현재 사용할 수 없습니다."
FALLBACK_COPY = "모델 응답 또는 프롬프트 검증이 실패했거나 설명을 현재 사용할 수 없습니다."

# Kept as a JSON-schema-shaped public contract while validation below remains
# stdlib-only and deliberately stricter (closed keys and linked citations).
EXPLANATION_JSON_SCHEMA: Mapping[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": RESPONSE_SCHEMA_VERSION,
    "type": "object",
    "additionalProperties": False,
    "required": [
        "symbol", "response_schema_version", "prompt_template_version",
        "model_policy_version", "model_provider", "model_name", "summary",
        "positive_evidence", "negative_evidence", "uncertainty", "data_gaps",
        "invalidation_conditions", "fallback_copy", "source_links",
        "not_investment_advice",
    ],
    "properties": {
        "symbol": {"type": "string", "enum": sorted(SUPPORTED_SYMBOLS)},
        "response_schema_version": {"const": RESPONSE_SCHEMA_VERSION},
        "prompt_template_version": {"type": "string"},
        "model_policy_version": {"type": "string"},
        "model_provider": {"type": "string"},
        "model_name": {"type": "string"},
        "summary": {"type": "string", "minLength": 1},
        "positive_evidence": {"type": "array"},
        "negative_evidence": {"type": "array"},
        "uncertainty": {"type": "array"},
        "data_gaps": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["feature_name", "feature_group", "status", "as_of", "text", "source_link_ids"],
            },
        },
        "invalidation_conditions": {"type": "array"},
        "fallback_copy": {"type": ["string", "null"]},
        "source_links": {"type": "array", "minItems": 1},
        "not_investment_advice": {"const": NOT_INVESTMENT_ADVICE},
    },
}

_TOP_KEYS = frozenset(EXPLANATION_JSON_SCHEMA["required"])
_CITED_KEYS = ("positive_evidence", "negative_evidence", "uncertainty", "invalidation_conditions")
_OTHER_IDENTITIES = {
    "005930": ("000660", "SK하이닉스", "에스케이하이닉스"),
    "000660": ("005930", "삼성전자"),
}
if set(_OTHER_IDENTITIES) != set(SUPPORTED_SYMBOLS):
    raise ValueError("cross-symbol identity policy must cover every supported symbol")
_FORBIDDEN_FIELDS = re.compile(r"(?:target[_ -]?price|probability|chance|confidence[_ -]?score|arbitrary[_ -]?score)", re.I)
_FORBIDDEN_TEXT = re.compile(
    r"(?:목표\s*(?:주가|가격|가)|target\s*price|확률|chance|probability|\d+(?:\.\d+)?\s*%|(?:임의|AI)?\s*점수\D{0,8}\d+|(?:임의\s*)?계산\s*결과|arbitrary\s*(?:score|calculation))",
    re.I,
)
_FORBIDDEN_COMPARISON_TEXT = re.compile(
    r"(?:다른\s*종목|타\s*종목|두\s*(?:종목\s*중|회사\s*(?:중|가운데))|상대적으로|대비|비교|"
    r"업종\s*내\s*상위|더\s*(?:우수|강)|superior|"
    r"(?:업종\s*평균|시장|sector\s+average)\s*보다|"
    r"compared\s+(?:with|to)\s+(?:the\s+)?market|market\s+comparison|"
    r"(?:stronger|better)\s+than\s+(?:the\s+)?(?:market|sector(?:\s+average)?)|"
    r"outperforms?\s+(?:the\s+)?(?:market|sector)|"
    r"relative\s+to\s+(?:the\s+)?(?:market|sector)|"
    r"better\s+than\s+peers?|relative\s+to\s+peers?|\bpeers?\b)",
    re.I,
)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _source_catalog(output: SymbolFeatureOutput) -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []
    known: set[str] = set()
    for group_name in GROUP_ORDER:
        for feature in output.groups[group_name].features:
            item = {
                "feature_name": feature.name,
                "feature_group": group_name,
                "status": feature.status,
                "as_of": feature.source_as_of,
                "source_feature_group": feature.source_feature_group,
                "run_id": None,
                "source_id": None,
            }
            item["id"] = stable_id("ai_source_link", {"symbol": output.symbol, **item})
            links.append(item)
            known.add(feature.name)
    for name in sorted((set(output.missing_required) | set(output.missing_optional)) - known):
        item = {
            "feature_name": name,
            "feature_group": "unavailable",
            "status": "MISSING",
            "as_of": output.available_data_cutoff,
            "source_feature_group": None,
            "run_id": None,
            "source_id": None,
        }
        item["id"] = stable_id("ai_source_link", {"symbol": output.symbol, **item})
        links.append(item)
    return links


def build_explanation_request(
    feature_output: SymbolFeatureOutput,
    *,
    model_provider: str,
    model_name: str,
    as_of: str,
    cutoff: str,
    prompt_template_version: str = PROMPT_TEMPLATE_VERSION,
    model_policy_version: str = MODEL_POLICY_VERSION,
) -> dict[str, Any]:
    """Build one request from exactly one symbol feature output."""
    if as_of != feature_output.as_of_kst or cutoff != feature_output.available_data_cutoff:
        raise ValueError("request as_of/cutoff must match the feature output")
    if not all(isinstance(x, str) and x.strip() for x in (model_provider, model_name, prompt_template_version, model_policy_version)):
        raise ValueError("model and version identifiers must be non-empty strings")

    feature_json = feature_output.stable_json()
    prompt = stable_json({
        "instruction": "Explain only this symbol using cited inputs; include counterarguments, uncertainty, gaps, and invalidation conditions. Never compare symbols or invent calculations, prices, or probabilities.",
        "response_schema_version": RESPONSE_SCHEMA_VERSION,
        "symbol": feature_output.symbol,
        "features": feature_output.to_dict(),
    })
    core = {
        "symbol": feature_output.symbol,
        "as_of_kst": as_of,
        "available_data_cutoff": cutoff,
        "response_schema_version": RESPONSE_SCHEMA_VERSION,
        "prompt_template_version": prompt_template_version,
        "model_policy_version": model_policy_version,
        "model_provider": model_provider,
        "model_name": model_name,
        "input_ledger_hash_sha256": _sha256(feature_json),
        "prompt_hash_sha256": _sha256(prompt),
        "prompt_redacted_preview": f"single-symbol explanation: {feature_output.symbol}; schema={RESPONSE_SCHEMA_VERSION}",
        "source_catalog": _source_catalog(feature_output),
    }
    core["ai_request_id"] = stable_id("ai_request", core)
    return core


def insert_ai_request(conn: sqlite3.Connection, request: Mapping[str, Any], *, run_id: str) -> None:
    """Record request metadata in the existing local ledger schema."""
    metadata = stable_json({
        "response_schema_version": request["response_schema_version"],
        "model_policy_version": request["model_policy_version"],
        "source_link_ids": [link["id"] for link in request["source_catalog"]],
    })
    conn.execute(
        """INSERT INTO ksf_ai_requests
        (ai_request_id, run_id, symbol, purpose, as_of_kst, available_data_cutoff,
         prompt_template_version, redaction_policy_version, input_ledger_hash_sha256,
         prompt_hash_sha256, prompt_redacted_preview, model_provider, model_name,
         request_metadata_json)
        VALUES (?, ?, ?, 'explain_decision', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            request["ai_request_id"], run_id, request["symbol"], request["as_of_kst"],
            request["available_data_cutoff"], request["prompt_template_version"],
            REDACTION_POLICY_VERSION, request["input_ledger_hash_sha256"],
            request["prompt_hash_sha256"], request["prompt_redacted_preview"],
            request["model_provider"], request["model_name"], metadata,
        ),
    )


def _walk(value: Any):
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key)
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)
    elif isinstance(value, str):
        yield value


def _validate_text_safety(payload: Mapping[str, Any], symbol: str) -> None:
    identities = _OTHER_IDENTITIES.get(symbol)
    if identities is None:
        raise ValueError(f"cross-symbol identity policy does not cover supported symbol {symbol!r}")
    for text in _walk(payload):
        if _FORBIDDEN_FIELDS.search(text) or _FORBIDDEN_TEXT.search(text):
            raise ValueError("invented numeric advice or arbitrary calculation is forbidden")
        if _FORBIDDEN_COMPARISON_TEXT.search(text):
            raise ValueError("cross-symbol comparison/reference is forbidden")
        if any(identity.casefold() in text.casefold() for identity in identities):
            raise ValueError("cross-symbol comparison/reference is forbidden")


def _gap_item(link: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "feature_name": link["feature_name"],
        "feature_group": link["feature_group"],
        "status": link["status"],
        "as_of": link["as_of"],
        "text": f"{link['feature_name']} 데이터 상태가 {link['status']}입니다.",
        "source_link_ids": [link["id"]],
    }


def validate_explanation(raw: str | Mapping[str, Any], request: Mapping[str, Any]) -> dict[str, Any]:
    """Strictly validate and normalize a model response for one request."""
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("model output is not valid JSON") from exc
    elif isinstance(raw, Mapping):
        payload = deepcopy(dict(raw))
    else:
        raise ValueError("model output must be a JSON object or string")
    if set(payload) != _TOP_KEYS:
        raise ValueError(f"response keys must exactly match schema; got {sorted(payload)}")
    if payload["symbol"] != request["symbol"]:
        raise ValueError("response symbol does not match single-symbol request")
    for key in ("response_schema_version", "prompt_template_version", "model_policy_version", "model_provider", "model_name"):
        if payload[key] != request[key]:
            raise ValueError(f"response {key} does not match request")
    if not isinstance(payload["summary"], str) or not payload["summary"].strip():
        raise ValueError("summary must be a non-empty string")
    if payload["fallback_copy"] is not None and not isinstance(payload["fallback_copy"], str):
        raise ValueError("fallback_copy must be null or a string")
    if payload["not_investment_advice"] != NOT_INVESTMENT_ADVICE:
        raise ValueError("not_investment_advice must use the approved copy")

    catalog = {item["id"]: dict(item) for item in request["source_catalog"]}
    if not isinstance(payload["source_links"], list) or not payload["source_links"]:
        raise ValueError("source_links must be a non-empty list")
    supplied: dict[str, dict[str, Any]] = {}
    for link in payload["source_links"]:
        if not isinstance(link, Mapping) or link.get("id") not in catalog or dict(link) != catalog.get(link.get("id")):
            raise ValueError("source link must exactly match the request source catalog")
        supplied[link["id"]] = dict(link)
    for key in (*_CITED_KEYS, "data_gaps"):
        if not isinstance(payload[key], list):
            raise ValueError(f"{key} must be a list")
        for item in payload[key]:
            if not isinstance(item, Mapping) or not isinstance(item.get("text"), str):
                raise ValueError(f"{key} items must contain text")
            ids = item.get("source_link_ids")
            if not isinstance(ids, list) or not ids or any(link_id not in catalog for link_id in ids):
                raise ValueError(f"{key} items must cite request source links")
            if any(link_id not in supplied for link_id in ids):
                raise ValueError(f"{key} items must cite response source_links")

    is_fallback = payload["fallback_copy"] is not None
    if is_fallback and (payload["summary"] != FALLBACK_SUMMARY or payload["fallback_copy"] != FALLBACK_COPY):
        raise ValueError("fallback responses must use the approved fallback copy")
    required_non_empty = ["uncertainty", "invalidation_conditions"]
    if not is_fallback:
        required_non_empty.extend(["positive_evidence", "negative_evidence"])
    if any(not payload[key] for key in required_non_empty):
        raise ValueError("required substantive sections must be non-empty")

    normalized_gaps: list[dict[str, Any]] = []
    for item in payload["data_gaps"]:
        ids = item["source_link_ids"]
        if len(ids) != 1:
            raise ValueError("data_gaps items must cite exactly one request source link")
        link = catalog[ids[0]]
        normalized_gaps.append({
            "feature_name": link["feature_name"],
            "feature_group": link["feature_group"],
            "status": link["status"],
            "as_of": link["as_of"],
            "text": item["text"],
            "source_link_ids": list(ids),
        })
    payload["data_gaps"] = normalized_gaps

    unavailable = [link for link in catalog.values() if link["status"] in {"MISSING", "STALE", "LICENSE_BLOCKED"}]
    cited_gap_ids = {link_id for item in payload["data_gaps"] for link_id in item["source_link_ids"]}
    for link in unavailable:
        if link["id"] not in supplied:
            payload["source_links"].append(link)
            supplied[link["id"]] = link
        if link["id"] not in cited_gap_ids:
            payload["data_gaps"].append(_gap_item(link))
        if not any(link["id"] in item["source_link_ids"] for item in payload["uncertainty"]):
            payload["uncertainty"].append({"text": f"{link['feature_name']}의 이용 불가로 판단에 불확실성이 있습니다.", "source_link_ids": [link["id"]]})
        if not any(link["id"] in item["source_link_ids"] for item in payload["invalidation_conditions"]):
            payload["invalidation_conditions"].append({"text": f"{link['feature_name']} 데이터가 갱신되면 이 판단을 다시 검토합니다.", "source_link_ids": [link["id"]]})
    _validate_text_safety(payload, request["symbol"])
    return payload


def build_fallback_explanation(request: Mapping[str, Any], failed_output: Any = None) -> dict[str, Any]:
    """Return approved Korean copy when model output is malformed/refused/failed."""
    del failed_output  # Failure content is intentionally neither logged nor echoed.
    links = [dict(link) for link in request["source_catalog"]]
    if not links:
        raise ValueError("fallback source_catalog must be non-empty")
    anchor = links[0]
    payload = {
        "symbol": request["symbol"],
        "response_schema_version": request["response_schema_version"],
        "prompt_template_version": request["prompt_template_version"],
        "model_policy_version": request["model_policy_version"],
        "model_provider": request["model_provider"],
        "model_name": request["model_name"],
        "summary": FALLBACK_SUMMARY,
        "positive_evidence": [],
        "negative_evidence": [],
        "uncertainty": [{"text": "모델 응답 또는 프롬프트 검증이 실패하여 설명이 제한됩니다.", "source_link_ids": [anchor["id"]]}],
        "data_gaps": [],
        "invalidation_conditions": [{"text": "검증된 응답이 제공되면 이 대체 문구를 무효화하고 다시 검토합니다.", "source_link_ids": [anchor["id"]]}],
        "fallback_copy": FALLBACK_COPY,
        "source_links": links,
        "not_investment_advice": NOT_INVESTMENT_ADVICE,
    }
    return validate_explanation(payload, request)


def normalize_explanation(raw: Any, request: Mapping[str, Any]) -> dict[str, Any]:
    """Validate model JSON, falling back only for malformed/refused output.

    A response having the full schema is never silently downgraded: symbol,
    comparison, citation, and numeric-advice violations remain hard failures.
    """
    if raw is None:
        return build_fallback_explanation(request, raw)
    candidate: Any = raw
    if isinstance(raw, str):
        try:
            candidate = json.loads(raw)
        except json.JSONDecodeError:
            return build_fallback_explanation(request, raw)
    if not isinstance(candidate, Mapping) or set(candidate) != _TOP_KEYS:
        return build_fallback_explanation(request, raw)
    return validate_explanation(candidate, request)
