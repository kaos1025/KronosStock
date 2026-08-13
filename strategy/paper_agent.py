"""Single-symbol paper proposal boundary (Task 3).

This module prepares one deterministic, deeply frozen model-input mapping from
exactly one :class:`ksf.feature_engine.SymbolFeatureOutput` plus sanitized
caller-held holdings, a closed policy-limits mapping, and immutable KSF
lineage, then strictly validates model output against the closed proposal
schema required by the paper-trading plan.  It contains no model, network,
broker, order, or external-write path: the model transport is an injected
callable and every eligibility, provider, or validation failure collapses to a
deterministic ABSTAIN proposal that never echoes raw provider content.

Contract highlights (Codex blocker remediation, second pass):

- Evidence bodies are complete: canonical value/unit/status/source context,
  the immutable request lineage (run/decision/snapshot-hash/cutoff), and a
  ``request_binding_sha256`` over the full validated request context (symbol,
  versions, provider/model, lineage, sanitized holdings, policy limits) are
  part of every evidence identity, so evidence IDs cannot be replayed across
  decisions or across request variants that differ only in holdings or policy
  limits.
- Canonical snapshot hashing is always attempted: a valid snapshot with a
  mismatched supplied ``feature_snapshot_sha256`` raises; a structurally
  invalid but serializable snapshot abstains while the request attests the
  actually computed hash; an unserializable snapshot attests a deterministic
  invalid-snapshot digest and never the supplied value.
- The snapshot trust boundary accepts only the exact value types FeatureEngine
  emits (exact ``SymbolFeatureOutput``/``FeatureGroupOutput``/``EngineFeature``
  with plain built-in scalars/tuples and ``MappingProxyType`` groups); a
  behavioral subclass or arbitrary/stateful mapping is rejected pre-model as
  ``DATA_SNAPSHOT_INVALID``.  An accepted snapshot is copied exactly once into
  canonical trusted values, and every downstream step — structural validation,
  timestamp checks, hashing, evidence construction, request binding, and the
  transport-eligibility decision — reads only that canonical copy.
- One total, fail-closed eligibility validator mirrors FeatureEngine's exact
  availability derivation (missing/stale/optional/license lists, state, and
  scoring flag) plus its full grouped scoring structure — canonical
  feature-to-group routing, the pinned production default group caps, the
  engine's ascending feature-name ordering, and exact recomputed group
  contributions — and enforces KST (+09:00) timestamp semantics for the
  cutoff, ``as_of_kst``, and every feature ``source_as_of`` before transport;
  a malformed nominal snapshot yields a deterministic data abstention, never
  an exception and never a transport call.
- The request mapping is recursively frozen (read-only mappings and tuples);
  its ID is computed from the canonical plain representation before freezing.
- Qualitative prose is a closed contract: the request publishes canonical
  per-action theses and invalidation copy, data-gap text is derived
  deterministically from the cited evidence, and the validator rejects any
  other prose (numeric tokens, comparisons, and fabricated non-numeric claims
  included).  Non-ABSTAIN actions must cite at least one feature or group
  evidence item, never only the request-lineage anchor.
- ``validate_proposal(..., mode="final")`` is the canonical validator for
  everything this boundary returns, including every allowlisted deterministic
  ABSTAIN; ``mode="model"`` additionally restricts abstain reasons a provider
  may originate so internal DATA_*/MODEL_* reasons cannot be spoofed.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
from collections.abc import Mapping
from copy import deepcopy
from types import MappingProxyType
from typing import Any, Callable

from ksf.feature_engine import (
    CANONICAL_RUN_STATES,
    DEFAULT_GROUP_CAPS_BPS,
    GROUP_ORDER,
    OPTIONAL_NAMES,
    REQUIRED_NAMES,
    SUPPORTED_SYMBOLS,
    EngineFeature,
    FeatureGroupOutput,
    SymbolFeatureOutput,
)
from ksf.global_collector import stable_id, stable_json


RESPONSE_SCHEMA_VERSION = "kronos-paper-proposal-v2"
PROMPT_TEMPLATE_VERSION = "kronos-paper-proposal-prompt-v1"
MODEL_POLICY_VERSION = "kronos-paper-single-symbol-policy-v1"

#: Canonical proposal actions.  These are portfolio *intent* labels, never
#: broker instructions.
ACTIONS = ("ENTER", "HOLD", "REDUCE", "EXIT", "ABSTAIN")

#: Exact closed top-level schema of every proposal returned by this boundary.
#: Identity/version fields mirror the request exactly; run/decision IDs stay in
#: the immutable request/audit context and never appear in model output.
PROPOSAL_KEYS = (
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
)

#: Proposal fields that must exactly equal the request and that a provider can
#: therefore never alter.
IDENTITY_KEYS = (
    "symbol",
    "response_schema_version",
    "prompt_template_version",
    "model_policy_version",
    "model_provider",
    "model_name",
)

#: Pre-model, data-ineligibility abstention reasons (transport is never called).
DATA_ABSTAIN_REASONS = frozenset(
    {
        "DATA_MISSING_REQUIRED",
        "DATA_STALE_REQUIRED",
        "DATA_RESTRICTED_REQUIRED",
        "DATA_CUTOFF_INCONSISTENT",
        "DATA_SNAPSHOT_INVALID",
        "SCORING_NOT_ALLOWED",
    }
)

#: Boundary-detected provider/model failure reasons.
MODEL_FAILURE_ABSTAIN_REASONS = frozenset(
    {
        "MODEL_TIMEOUT",
        "MODEL_PROVIDER_ERROR",
        "MODEL_OUTPUT_INVALID",
    }
)

#: Reasons the model itself may declare in a well-formed ABSTAIN response.
OFFLINE_ABSTAIN_REASONS = frozenset({"OFFLINE_SHADOW_MODE"})
MODEL_ABSTAIN_REASONS = frozenset({"NO_EDGE"})

ABSTAIN_REASONS = (DATA_ABSTAIN_REASONS | MODEL_FAILURE_ABSTAIN_REASONS
    | MODEL_ABSTAIN_REASONS | OFFLINE_ABSTAIN_REASONS)

#: Exact closed holdings schema separating the current symbol from the rest of
#: the authoritative portfolio value.
HOLDINGS_KEYS = (
    "symbol",
    "cash_krw",
    "quantity",
    "market_value_krw",
    "other_position_value_krw",
    "total_equity_krw",
    "current_exposure_pct",
)

#: Exact closed policy-limits schema.  The later deterministic risk engine owns
#: final quantity and complete portfolio policy; this boundary only enforces
#: that a proposal cannot even ask for more than the configured target cap.
POLICY_LIMIT_KEYS = ("max_target_exposure_pct",)

#: Exact closed schema of one structured, cited data-gap item.
DATA_GAP_ITEM_KEYS = ("feature_name", "feature_group", "status", "as_of", "text", "evidence_ids")

_SHA256_LOWER_HEX = re.compile(r"[0-9a-f]{64}")
_EXPOSURE_TOLERANCE_PCT = 0.1
_PCT_TOLERANCE = 1e-9

_SCORING_STATES = frozenset({"READY", "PARTIAL_DATA"})
_FEATURE_STATUSES = frozenset(
    {"READY", "STALE", "MISSING_REQUIRED", "MISSING_OPTIONAL", "LICENSE_BLOCKED"}
)
#: Evidence statuses that count as unavailable data and must surface as gaps.
_GAP_STATUSES = frozenset(
    {"MISSING", "STALE", "MISSING_REQUIRED", "MISSING_OPTIONAL", "LICENSE_BLOCKED"}
)

_ABSTAIN_THESES = {
    "DATA_MISSING_REQUIRED": "필수 입력 데이터가 없어 단일 종목 제안을 보류합니다.",
    "DATA_STALE_REQUIRED": "필수 입력 데이터가 최신 상태가 아니어서 단일 종목 제안을 보류합니다.",
    "DATA_RESTRICTED_REQUIRED": "필수 입력 데이터 사용이 제한되어 단일 종목 제안을 보류합니다.",
    "DATA_CUTOFF_INCONSISTENT": "데이터 기준 시점이 일치하지 않아 단일 종목 제안을 보류합니다.",
    "DATA_SNAPSHOT_INVALID": "피처 스냅샷이 표준 계약 검증을 통과하지 못해 단일 종목 제안을 보류합니다.",
    "SCORING_NOT_ALLOWED": "스코어링이 허용되지 않는 상태여서 단일 종목 제안을 보류합니다.",
    "MODEL_TIMEOUT": "모델 응답 시간 초과로 단일 종목 제안을 보류합니다.",
    "MODEL_PROVIDER_ERROR": "모델 제공자 오류로 단일 종목 제안을 보류합니다.",
    "MODEL_OUTPUT_INVALID": "모델 출력이 계약 검증을 통과하지 못해 단일 종목 제안을 보류합니다.",
    "NO_EDGE": "모델이 판단 우위를 확인하지 못해 단일 종목 제안을 보류합니다.",
    "OFFLINE_SHADOW_MODE": "오프라인 결정론적 섀도 모드이므로 단일 종목 제안을 보류합니다.",
}
if set(_ABSTAIN_THESES) != set(ABSTAIN_REASONS):
    raise ValueError("abstain thesis copy must cover exactly the abstain reason allowlist")

_ABSTAIN_INVALIDATION_TEXT = "판단 자격이 회복되고 검증된 최신 근거가 제공되면 이 보류를 재검토합니다."

#: Canonical, allowlisted per-action theses (B6).  Free provider prose cannot
#: bind qualitative claims to evidence, so the only accepted thesis per action
#: is this fixed neutral single-symbol copy with no feature or numeric claim.
#: The ABSTAIN thesis a provider may use is ``_ABSTAIN_THESES["NO_EDGE"]``.
_ACTION_THESES = {
    "ENTER": "요청에 결속된 검증 근거에 따라 이 종목의 노출 확대를 제안합니다.",
    "HOLD": "요청에 결속된 검증 근거에 따라 이 종목의 현재 노출 유지를 제안합니다.",
    "REDUCE": "요청에 결속된 검증 근거에 따라 이 종목의 노출 축소를 제안합니다.",
    "EXIT": "요청에 결속된 검증 근거에 따라 이 종목의 노출 정리를 제안합니다.",
}
if set(_ACTION_THESES) != set(ACTIONS) - {"ABSTAIN"}:
    raise ValueError("action thesis copy must cover exactly the non-abstain actions")

#: Canonical shared invalidation copy for non-ABSTAIN proposals (B6).
_INVALIDATION_TEXT = "인용한 근거의 검증 상태가 변하면 이 제안을 무효로 재검토합니다."

#: Deterministic digest attested when a snapshot cannot be canonically
#: serialized at all (B3); the caller-supplied hash is never attested then.
_UNSERIALIZABLE_SNAPSHOT_SHA256 = hashlib.sha256(
    b"kronos-paper-unserializable-feature-snapshot-v1"
).hexdigest()


# ---------------------------------------------------------------------------
# Task-3-local prose safety policy.
#
# Deliberately aligned with the KSF explanation boundary policy (no
# cross-symbol comparison, no invented numeric advice) but implemented locally
# through public contract only, and strictly stronger: model-authored prose may
# not contain any numeric token at all, because free text cannot bind a number
# to validated evidence.  All structured numerics live in schema fields.
# ---------------------------------------------------------------------------

_PROSE_NUMERIC_TOKEN = re.compile(r"[0-9０-９]")
_PROSE_INVENTED_CLAIM = re.compile(
    r"(?:목표\s*(?:주가|가격|가)|target\s*price|확률|chance|probability|점수|계산"
    r"|confidence\s*score|arbitrary\s*(?:score|calculation))",
    re.I,
)
_PROSE_COMPARISON = re.compile(
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
_OTHER_SYMBOL_IDENTITIES = {
    "005930": ("000660", "SK하이닉스", "에스케이하이닉스"),
    "000660": ("005930", "삼성전자"),
}
if set(_OTHER_SYMBOL_IDENTITIES) != set(SUPPORTED_SYMBOLS):
    raise ValueError("cross-symbol identity policy must cover every supported symbol")


def _validate_prose(texts: list[str], symbol: str) -> None:
    identities = _OTHER_SYMBOL_IDENTITIES.get(symbol)
    if identities is None:
        raise ValueError(f"prose safety policy does not cover symbol {symbol!r}")
    for text in texts:
        if _PROSE_COMPARISON.search(text):
            raise ValueError("cross-symbol comparison/reference is forbidden")
        if any(identity.casefold() in text.casefold() for identity in identities):
            raise ValueError("cross-symbol comparison/reference is forbidden")
        if _PROSE_INVENTED_CLAIM.search(text):
            raise ValueError("invented numeric advice or arbitrary calculation is forbidden")
        if _PROSE_NUMERIC_TOKEN.search(text):
            raise ValueError(
                "invented numeric advice is forbidden: unbound numeric token in model prose"
            )


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


_KST_OFFSET = dt.timedelta(hours=9)


def _parse_aware(value: Any, field: str) -> dt.datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty ISO-8601 timestamp")
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone offset")
    return parsed


def _parse_kst(value: Any, field: str) -> dt.datetime:
    parsed = _parse_aware(value, field)
    if parsed.utcoffset() != _KST_OFFSET:
        raise ValueError(f"{field} must carry the +09:00 KST offset")
    return parsed


def _finite_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


def _sanitize_holdings(holdings: Any, symbol: str) -> dict[str, Any]:
    """Validate the closed same-symbol holdings schema and return a plain copy."""
    if not isinstance(holdings, Mapping):
        raise ValueError("holdings must be a mapping using the closed holdings schema")
    if set(holdings) != set(HOLDINGS_KEYS):
        raise ValueError("holdings keys must exactly match the closed holdings schema")
    if holdings["symbol"] != symbol:
        raise ValueError("holdings symbol must match the single-symbol feature input")
    numeric: dict[str, float] = {}
    for key in ("cash_krw", "market_value_krw", "total_equity_krw", "current_exposure_pct"):
        value = holdings[key]
        if not _finite_number(value) or value < 0:
            raise ValueError(f"holdings {key} must be a plain finite nonnegative number")
        numeric[key] = value
    quantity = holdings["quantity"]
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 0:
        raise ValueError("holdings quantity must be a nonnegative integer")
    other_position_value = holdings["other_position_value_krw"]
    if (isinstance(other_position_value, bool)
            or not isinstance(other_position_value, int) or other_position_value < 0):
        raise ValueError("holdings other_position_value_krw must be a nonnegative integer")
    total = numeric["total_equity_krw"]
    if total <= 0:
        raise ValueError("holdings total_equity_krw must be positive")
    if numeric["cash_krw"] + numeric["market_value_krw"] + other_position_value != total:
        raise ValueError("holdings cash, current market value, and other position value must equal total equity")
    if (quantity == 0) != (numeric["market_value_krw"] == 0):
        raise ValueError("holdings quantity and market_value_krw must be jointly zero or positive")
    expected_exposure = numeric["market_value_krw"] / total * 100.0
    if abs(numeric["current_exposure_pct"] - expected_exposure) > _EXPOSURE_TOLERANCE_PCT:
        raise ValueError("holdings current_exposure_pct must equal market value over total equity")
    return {
        "symbol": holdings["symbol"],
        "cash_krw": numeric["cash_krw"],
        "quantity": quantity,
        "market_value_krw": numeric["market_value_krw"],
        "other_position_value_krw": other_position_value,
        "total_equity_krw": numeric["total_equity_krw"],
        "current_exposure_pct": numeric["current_exposure_pct"],
    }


def _sanitize_policy_limits(policy_limits: Any) -> dict[str, float]:
    """Validate the closed policy-limits schema and return a plain copy."""
    if not isinstance(policy_limits, Mapping):
        raise ValueError("policy_limits must be a mapping using the closed policy limits schema")
    if set(policy_limits) != set(POLICY_LIMIT_KEYS):
        raise ValueError("policy_limits keys must exactly match the closed policy limits schema")
    value = policy_limits["max_target_exposure_pct"]
    if not _finite_number(value) or not 0 < value <= 100:
        raise ValueError(
            "policy_limits max_target_exposure_pct must be a plain finite number in (0, 100]"
        )
    return {"max_target_exposure_pct": float(value)}


# ---------------------------------------------------------------------------
# Total, fail-closed snapshot eligibility validation (runs before transport).
#
# The two helpers below deliberately re-state (never import) the engine's
# private ``_target_group`` and ``_signal_bps`` semantics so this boundary can
# verify the full grouped feature/scoring structure through public contract
# only (B8).  Group caps are additionally pinned to the production default
# configuration ``DEFAULT_GROUP_CAPS_BPS``: a custom-cap snapshot fails closed
# as DATA_SNAPSHOT_INVALID rather than reach the model.
# ---------------------------------------------------------------------------


def _canonical_target_group(name: str, source_group: str) -> str | None:
    """Exact local mirror of the engine's feature-to-group routing rule."""
    if source_group == "domestic_investor_flow" or name.endswith("_net_buy_quantity"):
        return "domestic_investor_flow"
    if source_group in {"domestic_price", "price_volume"} or name in {
        "close",
        "volume",
        "one_day_return_bps",
    }:
        return "price_volume"
    if name.startswith("mu_") or name.startswith(("dram_", "nand_")):
        return "global_memory_price"
    if name.startswith("nvda_") or name.startswith("hbm_"):
        return "ai_hbm_demand"
    if name.startswith(("soxx_", "sox_", "tsm_")):
        return "semiconductor_regime"
    if source_group in {"global_fx_context", "fx_market"} or name == "usdkrw":
        return "fx_market"
    if source_group in {"event", "event_group"} or name.endswith("_event_days"):
        return "event"
    return None


def _canonical_signal_bps(feature: EngineFeature) -> float | None:
    """Exact local mirror of the engine's per-feature vote rule."""
    if feature.status != "READY" or feature.value is None or not math.isfinite(feature.value):
        return None
    name = feature.name
    if name.endswith("_one_day_return_bps") or name == "one_day_return_bps":
        return feature.value
    if name.endswith("_net_buy_quantity"):
        return max(-1000.0, min(1000.0, feature.value / 1000.0))
    # Absolute price, volume, FX and event-distance values carry no direction
    # without a baseline: context only, no vote.
    return None


def _canonical_contribution_bps(features: tuple[EngineFeature, ...], cap: int) -> int:
    """Exact local mirror of the engine's group vote aggregation."""
    votes = [vote for feature in features if (vote := _canonical_signal_bps(feature)) is not None]
    raw_vote = sum(votes) / len(votes) if votes else 0.0
    return int(round(max(-cap, min(cap, raw_vote))))


def _is_exact_str(value: Any) -> bool:
    return type(value) is str


def _is_exact_optional_str(value: Any) -> bool:
    return value is None or type(value) is str


def _safe_symbol(feature_output: Any) -> str:
    """Symbol of a rejected snapshot, read without running overridable code.

    Attribute access on a hostile subclass can dispatch to overridden
    descriptors or ``__getattribute__``, so the value is taken from the real
    instance ``__dict__`` directly.  A ``str`` subclass symbol still owns an
    immutable underlying character buffer, so its exact built-in value is
    recovered with the unbound ``str.__str__`` — never a subclass method,
    equality, arithmetic, or property — letting the rejected snapshot anchor a
    same-symbol deterministic abstention.  Anything else yields no trusted
    request identity and fails closed deterministically.
    """
    try:
        fields = object.__getattribute__(feature_output, "__dict__")
    except Exception:
        fields = None
    symbol = fields.get("symbol") if type(fields) is dict else None
    if type(symbol) is str:
        return symbol
    if issubclass(type(symbol), str):
        try:
            extracted = str.__str__(symbol)
        except Exception:
            extracted = None
        if type(extracted) is str:
            return extracted
    raise ValueError("feature_output symbol cannot be determined safely")


def _normalize_feature_output(feature_output: Any) -> SymbolFeatureOutput | None:
    """Copy one exact-typed snapshot into canonical trusted values, or None.

    The trust boundary accepts only the exact value types FeatureEngine emits:
    exact ``SymbolFeatureOutput``/``FeatureGroupOutput``/``EngineFeature``,
    exact built-in ``str``/``bool``/``int``/``float``/``tuple`` fields, and
    groups behind an exact ``MappingProxyType``.  Behavioral subclasses (lying
    ``str``/numeric/tuple comparisons or arithmetic, overridden
    ``stable_json``) and arbitrary or stateful mappings are rejected without
    invoking their overridable behavior beyond one unavoidable mapping read.
    The accepted snapshot is copied here exactly once; downstream validation,
    hashing, evidence, and request construction read only the returned
    canonical copy, so a retained-reference mutation cannot desynchronize
    them afterwards.
    """
    try:
        if type(feature_output) is not SymbolFeatureOutput:
            return None
        if not (
            _is_exact_str(feature_output.symbol)
            and _is_exact_str(feature_output.state)
            and _is_exact_str(feature_output.available_data_cutoff)
            and _is_exact_str(feature_output.as_of_kst)
            and _is_exact_str(feature_output.output_id)
            and type(feature_output.scoring_allowed) is bool
        ):
            return None
        availability: dict[str, tuple[str, ...]] = {}
        for field_name in (
            "missing_required",
            "missing_optional",
            "stale_required",
            "license_blocked",
        ):
            values = getattr(feature_output, field_name)
            if type(values) is not tuple or any(not _is_exact_str(item) for item in values):
                return None
            availability[field_name] = tuple(values)
        if type(feature_output.groups) is not MappingProxyType:
            return None
        # The single read of the untrusted groups mapping.
        raw_groups = dict(feature_output.groups)
        canonical_groups: dict[str, FeatureGroupOutput] = {}
        for group_key, group in raw_groups.items():
            if not _is_exact_str(group_key) or type(group) is not FeatureGroupOutput:
                return None
            if not (
                _is_exact_str(group.name)
                and type(group.cap_bps) is int
                and type(group.contribution_bps) is int
                and type(group.features) is tuple
            ):
                return None
            canonical_features: list[EngineFeature] = []
            for feature in group.features:
                if type(feature) is not EngineFeature:
                    return None
                if not (
                    _is_exact_str(feature.name)
                    and _is_exact_str(feature.status)
                    and _is_exact_str(feature.source_as_of)
                    and _is_exact_str(feature.source_feature_group)
                    and _is_exact_optional_str(feature.unit)
                    and _is_exact_optional_str(feature.missing_reason)
                ):
                    return None
                value = feature.value
                if value is not None and not (
                    (type(value) is int or type(value) is float) and math.isfinite(value)
                ):
                    return None
                canonical_features.append(
                    EngineFeature(
                        name=feature.name,
                        value=value,
                        status=feature.status,
                        source_as_of=feature.source_as_of,
                        source_feature_group=feature.source_feature_group,
                        unit=feature.unit,
                        missing_reason=feature.missing_reason,
                    )
                )
            canonical_groups[group_key] = FeatureGroupOutput(
                name=group.name,
                cap_bps=group.cap_bps,
                contribution_bps=group.contribution_bps,
                features=tuple(canonical_features),
            )
        return SymbolFeatureOutput(
            symbol=feature_output.symbol,
            state=feature_output.state,
            scoring_allowed=feature_output.scoring_allowed,
            available_data_cutoff=feature_output.available_data_cutoff,
            as_of_kst=feature_output.as_of_kst,
            groups=MappingProxyType(canonical_groups),
            missing_required=availability["missing_required"],
            missing_optional=availability["missing_optional"],
            stale_required=availability["stale_required"],
            license_blocked=availability["license_blocked"],
            output_id=feature_output.output_id,
        )
    except Exception:
        return None


def _structural_problem(output: SymbolFeatureOutput) -> str | None:
    """Canonical-structure check for one nominal SymbolFeatureOutput.

    Mirrors :meth:`ksf.feature_engine.FeatureEngine.build_symbol` exactly: the
    snapshot's availability lists, state, and scoring flag must equal what the
    engine would derive from the per-feature rows, every grouped feature must
    sit in its canonical routed group in the engine's ascending name order,
    each group cap must equal the pinned production default, and each group
    contribution must equal the engine's recomputed vote aggregation — so a
    hash-consistent forgery can neither smuggle unavailable required data past
    the list-based eligibility checks nor carry forged group/scoring evidence
    to the model.  ``license_blocked`` is derived from grouped rows only; an
    engine row omitted from every group by its routing rule is unobservable
    here, so such snapshots conservatively fail closed instead of transporting.
    """
    if output.state not in CANONICAL_RUN_STATES:
        return "state is not canonical"
    for field_name in ("missing_required", "missing_optional", "stale_required", "license_blocked"):
        values = getattr(output, field_name)
        if (
            not isinstance(values, tuple)
            or any(not isinstance(item, str) or not item for item in values)
            or len(set(values)) != len(values)
        ):
            return f"{field_name} must be a tuple of unique non-empty names"
    groups = output.groups
    if not isinstance(groups, Mapping) or set(groups) != set(GROUP_ORDER):
        return "groups must cover exactly the canonical feature groups"
    rows: dict[str, EngineFeature] = {}
    for group_name in GROUP_ORDER:
        group = groups[group_name]
        if not isinstance(group, FeatureGroupOutput) or group.name != group_name:
            return "group name/type is not canonical"
        if (
            isinstance(group.cap_bps, bool)
            or not isinstance(group.cap_bps, int)
            or group.cap_bps != DEFAULT_GROUP_CAPS_BPS[group_name]
        ):
            return "group cap_bps must equal the canonical default group cap"
        if isinstance(group.contribution_bps, bool) or not isinstance(group.contribution_bps, int):
            return "group contribution_bps must be an integer"
        if not isinstance(group.features, tuple):
            return "group features must be a tuple"
        previous_name = None
        for feature in group.features:
            if not isinstance(feature, EngineFeature):
                return "feature type is not canonical"
            if not isinstance(feature.name, str) or not feature.name or feature.name in rows:
                return "feature names must be unique non-empty strings"
            if feature.status not in _FEATURE_STATUSES:
                return "feature status is not canonical"
            if feature.value is not None and not _finite_number(feature.value):
                return "feature value must be a plain finite number"
            if feature.unit is not None and not isinstance(feature.unit, str):
                return "feature unit must be null or a string"
            if feature.missing_reason is not None and not isinstance(feature.missing_reason, str):
                return "feature missing_reason must be null or a string"
            if not isinstance(feature.source_feature_group, str) or not feature.source_feature_group:
                return "feature source_feature_group must be a non-empty string"
            if not isinstance(feature.source_as_of, str) or not feature.source_as_of:
                return "feature source_as_of must be a non-empty string"
            if _canonical_target_group(feature.name, feature.source_feature_group) != group_name:
                return "feature is not in its canonical engine group"
            if previous_name is not None and previous_name >= feature.name:
                return "group features must be sorted ascending by name"
            previous_name = feature.name
            rows[feature.name] = feature
        if group.contribution_bps != _canonical_contribution_bps(group.features, group.cap_bps):
            return "group contribution_bps does not equal the canonical engine aggregation"

    # Exact FeatureEngine availability derivation (B8/B2).
    names = set(rows)
    unavailable_statuses = {"MISSING_REQUIRED", "MISSING_OPTIONAL", "LICENSE_BLOCKED"}
    derived_missing_required = tuple(
        sorted(
            (REQUIRED_NAMES - names)
            | {
                name
                for name in REQUIRED_NAMES & names
                if rows[name].status in unavailable_statuses or rows[name].value is None
            }
        )
    )
    derived_stale_required = tuple(
        sorted(name for name in REQUIRED_NAMES & names if rows[name].status == "STALE")
    )
    derived_missing_optional = tuple(
        sorted(
            name
            for name in OPTIONAL_NAMES
            if name not in names
            or rows[name].status in {"MISSING_OPTIONAL", "MISSING_REQUIRED"}
            or (rows[name].value is None and rows[name].status != "LICENSE_BLOCKED")
        )
    )
    derived_license_blocked = tuple(
        sorted(name for name, row in rows.items() if row.status == "LICENSE_BLOCKED")
    )
    if derived_missing_required:
        derived_state = "MISSING_REQUIRED_DATA"
    elif derived_stale_required:
        derived_state = "STALE_DATA"
    elif derived_missing_optional:
        derived_state = "PARTIAL_DATA"
    else:
        derived_state = "READY"

    if output.missing_required != derived_missing_required:
        return "missing_required does not equal the canonical engine derivation"
    if output.stale_required != derived_stale_required:
        return "stale_required does not equal the canonical engine derivation"
    if output.missing_optional != derived_missing_optional:
        return "missing_optional does not equal the canonical engine derivation"
    if output.license_blocked != derived_license_blocked:
        return "license_blocked does not equal the canonical engine derivation"
    if output.state != derived_state:
        return "state does not equal the canonical engine derivation"
    if not isinstance(output.scoring_allowed, bool) or output.scoring_allowed != (
        derived_state in _SCORING_STATES
    ):
        return "scoring_allowed disagrees with the canonical state"
    unavailable_names = (
        set(derived_missing_required)
        | set(derived_missing_optional)
        | set(derived_license_blocked)
    )
    for name, row in rows.items():
        if row.value is None and row.status in {"READY", "STALE"} and name not in unavailable_names:
            return "null feature value is only allowed for unavailable data"
    return None


def _timestamp_problem(output: SymbolFeatureOutput, supplied_cutoff: str) -> str | None:
    """One internally consistent timestamp view, or the first inconsistency."""
    try:
        cutoff = _parse_kst(output.available_data_cutoff, "feature output cutoff")
        as_of = _parse_kst(output.as_of_kst, "feature output as_of_kst")
    except ValueError:
        return "feature output timestamps are malformed"
    if output.available_data_cutoff != supplied_cutoff:
        return "supplied cutoff does not exactly equal the feature output cutoff"
    if cutoff > as_of:
        return "available_data_cutoff must not be after as_of_kst"
    for group_name in GROUP_ORDER:
        for feature in output.groups[group_name].features:
            try:
                observed = _parse_kst(feature.source_as_of, "feature source_as_of")
            except ValueError:
                return "feature source_as_of is malformed"
            if observed > cutoff:
                return "feature observed after the data cutoff"
    return None


def _canonical_output_id(output: SymbolFeatureOutput) -> str:
    """Recompute the engine's output_id from the canonical snapshot payload."""
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


def _pre_model_abstain_reason(
    output: SymbolFeatureOutput, supplied_cutoff: str
) -> str | None:
    """Total pre-model eligibility assessment.

    Never raises for a nominal SymbolFeatureOutput: every structural,
    timestamp, or data defect maps to a deterministic data abstain reason and
    guarantees the transport is never invoked for it.
    """
    try:
        if _structural_problem(output) is not None:
            return "DATA_SNAPSHOT_INVALID"
        if _timestamp_problem(output, supplied_cutoff) is not None:
            return "DATA_CUTOFF_INCONSISTENT"
        if set(output.license_blocked) & set(REQUIRED_NAMES):
            return "DATA_RESTRICTED_REQUIRED"
        if set(output.missing_required) - set(output.license_blocked):
            return "DATA_MISSING_REQUIRED"
        if output.stale_required:
            return "DATA_STALE_REQUIRED"
        if not output.scoring_allowed:
            return "SCORING_NOT_ALLOWED"
        if output.output_id != _canonical_output_id(output):
            return "DATA_SNAPSHOT_INVALID"
    except Exception:
        return "DATA_SNAPSHOT_INVALID"
    return None


# ---------------------------------------------------------------------------
# Evidence catalog: complete bodies bound to immutable request lineage.
# ---------------------------------------------------------------------------


def _evidence_item(lineage: Mapping[str, Any], **fields: Any) -> dict[str, Any]:
    body = {
        "kind": fields["kind"],
        "symbol": lineage["symbol"],
        "ksf_run_id": lineage["ksf_run_id"],
        "ksf_decision_id": lineage["ksf_decision_id"],
        "feature_snapshot_sha256": lineage["feature_snapshot_sha256"],
        "request_binding_sha256": lineage["request_binding_sha256"],
        "available_data_cutoff": lineage["available_data_cutoff"],
        "feature_name": fields.get("feature_name"),
        "feature_group": fields.get("feature_group"),
        "source_feature_group": fields.get("source_feature_group"),
        "status": fields["status"],
        "as_of": fields["as_of"],
        "value": fields.get("value"),
        "unit": fields.get("unit"),
        "missing_reason": fields.get("missing_reason"),
        "contribution_bps": fields.get("contribution_bps"),
        "cap_bps": fields.get("cap_bps"),
    }
    return {**body, "id": stable_id("paper_evidence", body)}


def _lineage_anchor(lineage: Mapping[str, Any]) -> dict[str, Any]:
    """Deterministic evidence item binding the request lineage itself.

    Always present as the first catalog item so a deterministic ABSTAIN can
    cite request-bound evidence even when the snapshot is unusable.
    """
    return _evidence_item(
        lineage,
        kind="request_lineage",
        status="LINEAGE",
        as_of=lineage["available_data_cutoff"],
    )


def _snapshot_evidence(
    output: SymbolFeatureOutput, lineage: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Deterministic evidence items derived only from this one feature output."""
    items: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for group_name in GROUP_ORDER:
        group = output.groups[group_name]
        for feature in group.features:
            items.append(
                _evidence_item(
                    lineage,
                    kind="feature",
                    feature_name=feature.name,
                    feature_group=group_name,
                    source_feature_group=feature.source_feature_group,
                    status=feature.status,
                    as_of=feature.source_as_of,
                    value=feature.value,
                    unit=feature.unit,
                    missing_reason=feature.missing_reason,
                )
            )
            seen_names.add(feature.name)
        items.append(
            _evidence_item(
                lineage,
                kind="group_contribution",
                feature_group=group_name,
                status="AGGREGATED",
                as_of=output.available_data_cutoff,
                contribution_bps=group.contribution_bps,
                cap_bps=group.cap_bps,
            )
        )
    unavailable = (
        set(output.missing_required)
        | set(output.missing_optional)
        | set(output.stale_required)
        | set(output.license_blocked)
    ) - seen_names
    for name in sorted(unavailable):
        items.append(
            _evidence_item(
                lineage,
                kind="data_gap",
                feature_name=name,
                feature_group="unavailable",
                status="MISSING",
                as_of=output.available_data_cutoff,
                missing_reason="unavailable at the data cutoff",
            )
        )
    return items


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    return value


def build_proposal_request(
    feature_output: SymbolFeatureOutput,
    holdings: Mapping[str, Any],
    *,
    policy_limits: Mapping[str, Any],
    model_provider: str,
    model_name: str,
    ksf_run_id: str,
    ksf_decision_id: str,
    feature_snapshot_sha256: str,
    available_data_cutoff: str,
) -> Mapping[str, Any]:
    """Build one deeply frozen, deterministic, auditable model-input mapping.

    Consumes exactly one symbol feature output, the sanitized closed-schema
    holdings for that same symbol, the closed policy-limits mapping, and the
    immutable KSF lineage identifiers.  The snapshot is normalized once into an
    exact-typed canonical copy (B8) and every later step reads only that copy.
    Caller inputs are validated and copied, never mutated;
    ``feature_snapshot_sha256`` must equal the canonical hash of the consumed
    snapshot (``sha256`` of the canonical copy's ``stable_json``) whenever the
    snapshot is canonically hashable, so forged lineage raises here.

    Pre-model eligibility is assessed with the total fail-closed validator and
    recorded under ``pre_model_abstain_reason`` (``None`` when the model may be
    called), so a request can still anchor a deterministic ABSTAIN proposal.
    The returned mapping is recursively read-only; ``proposal_request_id`` is
    computed from the canonical plain representation before freezing.
    """
    if not isinstance(feature_output, SymbolFeatureOutput):
        raise ValueError("feature_output must be a ksf SymbolFeatureOutput")
    # B8 trust boundary: copy the snapshot once into canonical trusted values.
    # Every later step reads only this copy, never the untrusted original; a
    # deceptive subclass/container snapshot yields no copy and is rejected
    # pre-model once its symbol can be read safely.
    canonical_output = _normalize_feature_output(feature_output)
    symbol = canonical_output.symbol if canonical_output is not None else _safe_symbol(
        feature_output
    )
    if symbol not in SUPPORTED_SYMBOLS:
        raise ValueError(f"unsupported symbol: {symbol!r}")
    _require_text(model_provider, "model_provider")
    _require_text(model_name, "model_name")
    _require_text(ksf_run_id, "ksf_run_id")
    _require_text(ksf_decision_id, "ksf_decision_id")
    if not isinstance(feature_snapshot_sha256, str) or _SHA256_LOWER_HEX.fullmatch(
        feature_snapshot_sha256
    ) is None:
        raise ValueError("feature_snapshot_sha256 must be 64 lowercase hex characters")
    _parse_aware(available_data_cutoff, "available_data_cutoff")

    sanitized_holdings = _sanitize_holdings(holdings, symbol)
    sanitized_policy = _sanitize_policy_limits(policy_limits)
    reason = (
        "DATA_SNAPSHOT_INVALID"
        if canonical_output is None
        else _pre_model_abstain_reason(canonical_output, available_data_cutoff)
    )

    # Truthful snapshot hashing (B3): always attempt the canonical hash so the
    # lineage anchor never attests an arbitrary supplied value.  A rejected
    # deceptive snapshot has no canonical serialization at this boundary.
    try:
        canonical_json = (
            canonical_output.stable_json() if canonical_output is not None else None
        )
    except Exception:
        canonical_json = None
    if canonical_json is None:
        # Unhashable content is never eligible; attest the deterministic
        # invalid-snapshot digest instead of the supplied hash.
        reason = "DATA_SNAPSHOT_INVALID"
        snapshot_sha256 = _UNSERIALIZABLE_SNAPSHOT_SHA256
    else:
        canonical_hash = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
        if reason != "DATA_SNAPSHOT_INVALID" and feature_snapshot_sha256 != canonical_hash:
            raise ValueError(
                "feature_snapshot_sha256 must equal the canonical feature snapshot hash"
            )
        snapshot_sha256 = canonical_hash

    # Whole-request binding (B4): a deterministic hash over the complete plain
    # validated request context, computed before evidence identities so every
    # evidence ID (and therefore every citable response) is bound to exactly
    # this holdings/policy/model/lineage combination without circular hashing.
    request_binding_sha256 = hashlib.sha256(
        stable_json(
            {
                "response_schema_version": RESPONSE_SCHEMA_VERSION,
                "prompt_template_version": PROMPT_TEMPLATE_VERSION,
                "model_policy_version": MODEL_POLICY_VERSION,
                "symbol": symbol,
                "model_provider": model_provider,
                "model_name": model_name,
                "ksf_run_id": ksf_run_id,
                "ksf_decision_id": ksf_decision_id,
                "feature_snapshot_sha256": snapshot_sha256,
                "available_data_cutoff": available_data_cutoff,
                "holdings": sanitized_holdings,
                "policy_limits": sanitized_policy,
            }
        ).encode("utf-8")
    ).hexdigest()

    lineage = {
        "symbol": symbol,
        "ksf_run_id": ksf_run_id,
        "ksf_decision_id": ksf_decision_id,
        "feature_snapshot_sha256": snapshot_sha256,
        "request_binding_sha256": request_binding_sha256,
        "available_data_cutoff": available_data_cutoff,
    }
    catalog: list[dict[str, Any]] = [_lineage_anchor(lineage)]
    if reason != "DATA_SNAPSHOT_INVALID" and canonical_output is not None:
        catalog.extend(_snapshot_evidence(canonical_output, lineage))
    catalog_ids = [item["id"] for item in catalog]
    if len(catalog_ids) != len(set(catalog_ids)):
        raise ValueError("evidence catalog ids must be unique")

    core: dict[str, Any] = {
        "response_schema_version": RESPONSE_SCHEMA_VERSION,
        "prompt_template_version": PROMPT_TEMPLATE_VERSION,
        "model_policy_version": MODEL_POLICY_VERSION,
        "symbol": symbol,
        "model_provider": model_provider,
        "model_name": model_name,
        "ksf_run_id": ksf_run_id,
        "ksf_decision_id": ksf_decision_id,
        "feature_snapshot_sha256": snapshot_sha256,
        "request_binding_sha256": request_binding_sha256,
        "available_data_cutoff": available_data_cutoff,
        "feature_output_id": canonical_output.output_id if canonical_output is not None else None,
        "feature_state": canonical_output.state if canonical_output is not None else None,
        "feature_as_of_kst": (
            canonical_output.as_of_kst if canonical_output is not None else None
        ),
        "scoring_allowed": (
            canonical_output.scoring_allowed if canonical_output is not None else False
        ),
        "pre_model_abstain_reason": reason,
        "allowed_actions": list(ACTIONS),
        "proposal_keys": list(PROPOSAL_KEYS),
        "canonical_theses": {**_ACTION_THESES, "ABSTAIN": _ABSTAIN_THESES["NO_EDGE"]},
        "canonical_invalidation_text": _INVALIDATION_TEXT,
        "canonical_abstain_invalidation_text": _ABSTAIN_INVALIDATION_TEXT,
        "policy_limits": sanitized_policy,
        "holdings": sanitized_holdings,
        "evidence_catalog": catalog,
    }
    core["proposal_request_id"] = stable_id("paper_proposal", core)
    return _deep_freeze(core)


def _validate_evidence_ids(ids: Any, catalog_ids: set[str], field: str) -> list[str]:
    if not isinstance(ids, list) or not ids:
        raise ValueError(f"{field} must be a non-empty evidence id list")
    if any(not isinstance(item, str) for item in ids):
        raise ValueError(f"{field} evidence ids must be strings")
    if len(set(ids)) != len(ids):
        raise ValueError(f"{field} evidence ids must be unique")
    if any(item not in catalog_ids for item in ids):
        raise ValueError(f"{field} evidence ids must resolve to this request's evidence catalog")
    return list(ids)


def _gap_text(item: Mapping[str, Any]) -> str:
    """Deterministic data-gap copy derived only from the cited evidence (B6)."""
    return f"{item['feature_name']} 데이터 상태가 {item['status']}입니다."


def _gap_from_evidence(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "feature_name": item["feature_name"],
        "feature_group": item["feature_group"],
        "status": item["status"],
        "as_of": item["as_of"],
        "text": _gap_text(item),
        "evidence_ids": [item["id"]],
    }


def _unavailable_evidence_ids(catalog: list[Mapping[str, Any]]) -> list[str]:
    return [
        item["id"]
        for item in catalog
        if item["kind"] == "data_gap"
        or (item["kind"] == "feature" and item["status"] in _GAP_STATUSES)
    ]


def _normalize_data_gaps(
    supplied: Any,
    catalog_by_id: Mapping[str, Mapping[str, Any]],
    unavailable_ids: list[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Validate supplied gap items and reflect every unavailable evidence item.

    Returns the normalized gap list plus the model-authored gap texts that must
    still pass the prose safety policy.
    """
    if not isinstance(supplied, list):
        raise ValueError("data_gaps must be a list")
    unavailable_set = set(unavailable_ids)
    covered: set[str] = set()
    normalized: list[dict[str, Any]] = []
    supplied_texts: list[str] = []
    for item in supplied:
        if not isinstance(item, Mapping) or set(item) != set(DATA_GAP_ITEM_KEYS):
            raise ValueError("data_gaps items must contain exactly the closed data gap keys")
        ids = item["evidence_ids"]
        if not isinstance(ids, list) or len(ids) != 1 or not isinstance(ids[0], str):
            raise ValueError("data_gaps items must cite exactly one evidence id")
        evidence_id = ids[0]
        if evidence_id not in unavailable_set:
            raise ValueError("data_gaps items must cite unavailable request evidence")
        if evidence_id in covered:
            raise ValueError("data_gaps items must cite each unavailable evidence at most once")
        evidence = catalog_by_id[evidence_id]
        text = item["text"]
        if not isinstance(text, str) or not text.strip():
            raise ValueError("data_gaps items must contain non-empty text")
        if text != _gap_text(evidence):
            raise ValueError(
                "data_gaps text must exactly equal the deterministic text for the cited evidence"
            )
        for key in ("feature_name", "feature_group", "status", "as_of"):
            if item[key] != evidence[key]:
                raise ValueError("data_gaps items must mirror the cited evidence fields")
        covered.add(evidence_id)
        supplied_texts.append(text)
        normalized.append(
            {
                "feature_name": evidence["feature_name"],
                "feature_group": evidence["feature_group"],
                "status": evidence["status"],
                "as_of": evidence["as_of"],
                "text": text,
                "evidence_ids": [evidence_id],
            }
        )
    for evidence_id in unavailable_ids:
        if evidence_id not in covered:
            normalized.append(_gap_from_evidence(catalog_by_id[evidence_id]))
    return normalized, supplied_texts


def validate_proposal(
    raw: Any, request: Mapping[str, Any], *, mode: str = "model"
) -> dict[str, Any]:
    """Strictly validate one proposal payload against one frozen request.

    ``mode="model"`` validates raw provider output: identity/version fields
    must exactly equal the request, and an ABSTAIN may only use the model
    abstain allowlist so internal DATA_*/MODEL_* reasons cannot be spoofed.
    ``mode="final"`` is the canonical validator for finished boundary output
    and accepts every allowlisted deterministic abstain reason as well.

    Accepts JSON text or a mapping, enforces the exact closed proposal schema,
    action/range/type semantics against the request's holdings and policy
    limits, the prose safety policy, structured cited data gaps, and citation
    of this request's evidence only.  Raises ValueError on every violation and
    never mutates the caller's payload.
    """
    if mode not in {"model", "final"}:
        raise ValueError("validation mode must be 'model' or 'final'")
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("model output is not valid JSON") from exc
    elif isinstance(raw, Mapping):
        payload = deepcopy(dict(raw))
    else:
        raise ValueError("model output must be a JSON object or JSON text")
    if not isinstance(payload, Mapping):
        raise ValueError("model output must be a JSON object")
    if set(payload) != set(PROPOSAL_KEYS):
        raise ValueError("proposal keys must exactly match the closed proposal schema")

    for key in IDENTITY_KEYS:
        if payload[key] != request[key]:
            raise ValueError(f"proposal {key} must exactly equal the request")

    action = payload["action"]
    if action not in ACTIONS:
        raise ValueError("action must be one of the canonical proposal actions")

    target = payload["target_exposure_pct"]
    if isinstance(target, bool) or not isinstance(target, (int, float)):
        raise ValueError("target_exposure_pct must be a plain number")
    if not math.isfinite(target) or not 0 <= target <= 100:
        raise ValueError("target_exposure_pct must be finite and between 0 and 100")
    target = float(target)

    confidence = payload["confidence_bps"]
    if isinstance(confidence, bool) or not isinstance(confidence, int):
        raise ValueError("confidence_bps must be an integer")
    if not 0 <= confidence <= 10000:
        raise ValueError("confidence_bps must be between 0 and 10000")

    thesis = payload["thesis"]
    if not isinstance(thesis, str) or not thesis.strip():
        raise ValueError("thesis must be a non-empty string")

    catalog = list(request["evidence_catalog"])
    catalog_ids = [item["id"] for item in catalog]
    if len(catalog_ids) != len(set(catalog_ids)):
        raise ValueError("request evidence catalog ids must be unique")
    catalog_by_id = {item["id"]: item for item in catalog}
    evidence_ids = _validate_evidence_ids(payload["evidence_ids"], set(catalog_ids), "evidence_ids")

    conditions = payload["invalidation_conditions"]
    if not isinstance(conditions, list) or not conditions:
        raise ValueError("invalidation_conditions must be a non-empty list")
    normalized_conditions: list[dict[str, Any]] = []
    for item in conditions:
        if not isinstance(item, Mapping) or set(item) != {"text", "evidence_ids"}:
            raise ValueError(
                "invalidation_conditions items must contain exactly text and evidence_ids"
            )
        text = item["text"]
        if not isinstance(text, str) or not text.strip():
            raise ValueError("invalidation_conditions items must contain non-empty text")
        cited = _validate_evidence_ids(
            item["evidence_ids"], set(catalog_ids), "invalidation_conditions"
        )
        normalized_conditions.append({"text": text, "evidence_ids": cited})

    normalized_gaps, gap_texts = _normalize_data_gaps(
        payload["data_gaps"], catalog_by_id, _unavailable_evidence_ids(catalog)
    )

    _validate_prose(
        [thesis, *(item["text"] for item in normalized_conditions), *gap_texts],
        request["symbol"],
    )

    reason = payload["abstain_reason"]
    if action == "ABSTAIN":
        allowed_reasons = MODEL_ABSTAIN_REASONS if mode == "model" else ABSTAIN_REASONS
        if reason not in allowed_reasons:
            raise ValueError(
                "abstain_reason must come from the abstain reason allowlist for this mode"
            )
        if thesis != _ABSTAIN_THESES[reason]:
            raise ValueError(
                "thesis must exactly equal the canonical abstain thesis for this reason"
            )
        for item in normalized_conditions:
            if item["text"] != _ABSTAIN_INVALIDATION_TEXT:
                raise ValueError(
                    "invalidation_conditions text must exactly equal the canonical abstain copy"
                )
        if target != 0.0:
            raise ValueError("ABSTAIN target_exposure_pct must be 0")
        if confidence != 0:
            raise ValueError("ABSTAIN confidence_bps must be 0")
    else:
        if reason is not None:
            raise ValueError("abstain_reason must be null unless action is ABSTAIN")
        if thesis != _ACTION_THESES[action]:
            raise ValueError("thesis must exactly equal the canonical thesis for this action")
        for item in normalized_conditions:
            if item["text"] != _INVALIDATION_TEXT:
                raise ValueError(
                    "invalidation_conditions text must exactly equal the canonical copy"
                )
        if not any(
            catalog_by_id[evidence_id]["kind"] in {"feature", "group_contribution"}
            for evidence_id in evidence_ids
        ):
            raise ValueError(
                "evidence_ids must cite at least one feature or group evidence item "
                "for a non-ABSTAIN action"
            )
        holdings = request["holdings"]
        current = float(holdings["current_exposure_pct"])
        quantity = holdings["quantity"]
        if target > request["policy_limits"]["max_target_exposure_pct"] + _PCT_TOLERANCE:
            raise ValueError(
                "target_exposure_pct must not exceed the policy limit max_target_exposure_pct"
            )
        if action == "HOLD" and abs(target - current) > _PCT_TOLERANCE:
            raise ValueError("HOLD target_exposure_pct must keep the current exposure")
        if action == "ENTER" and target <= current + _PCT_TOLERANCE:
            raise ValueError("ENTER target_exposure_pct must exceed the current exposure")
        if action in {"REDUCE", "EXIT"} and quantity <= 0:
            raise ValueError(f"{action} needs an existing position")
        if action == "REDUCE" and not _PCT_TOLERANCE < target < current - _PCT_TOLERANCE:
            raise ValueError("REDUCE target_exposure_pct must be strictly between 0 and current")
        if action == "EXIT" and target != 0.0:
            raise ValueError("EXIT target_exposure_pct must be 0")

    return {
        "symbol": request["symbol"],
        "response_schema_version": request["response_schema_version"],
        "prompt_template_version": request["prompt_template_version"],
        "model_policy_version": request["model_policy_version"],
        "model_provider": request["model_provider"],
        "model_name": request["model_name"],
        "action": action,
        "target_exposure_pct": target,
        "confidence_bps": confidence,
        "thesis": thesis,
        "evidence_ids": evidence_ids,
        "invalidation_conditions": normalized_conditions,
        "data_gaps": normalized_gaps,
        "abstain_reason": reason,
    }


def build_abstain_proposal(request: Mapping[str, Any], reason: str) -> dict[str, Any]:
    """Deterministic, schema-complete ABSTAIN proposal for an allowlisted reason.

    Copy is fixed per reason and cites the request lineage anchor evidence;
    unavailable evidence is reflected as structured data gaps.  Raw provider or
    exception content is never accepted here, so nothing can leak into the
    abstention.  Every result passes ``validate_proposal(..., mode="final")``.
    """
    if reason not in ABSTAIN_REASONS:
        raise ValueError("abstain reason must come from the abstain reason allowlist")
    catalog = list(request["evidence_catalog"])
    if not catalog:
        raise ValueError("request evidence catalog must be non-empty")
    anchor = catalog[0]["id"]
    catalog_by_id = {item["id"]: item for item in catalog}
    gaps = [
        _gap_from_evidence(catalog_by_id[evidence_id])
        for evidence_id in _unavailable_evidence_ids(catalog)
    ]
    return {
        "symbol": request["symbol"],
        "response_schema_version": request["response_schema_version"],
        "prompt_template_version": request["prompt_template_version"],
        "model_policy_version": request["model_policy_version"],
        "model_provider": request["model_provider"],
        "model_name": request["model_name"],
        "action": "ABSTAIN",
        "target_exposure_pct": 0.0,
        "confidence_bps": 0,
        "thesis": _ABSTAIN_THESES[reason],
        "evidence_ids": [anchor],
        "invalidation_conditions": [
            {"text": _ABSTAIN_INVALIDATION_TEXT, "evidence_ids": [anchor]}
        ],
        "data_gaps": gaps,
        "abstain_reason": reason,
    }


def propose(
    feature_output: SymbolFeatureOutput,
    holdings: Mapping[str, Any],
    *,
    policy_limits: Mapping[str, Any],
    model_provider: str,
    model_name: str,
    transport: Callable[[Mapping[str, Any]], Any],
    ksf_run_id: str,
    ksf_decision_id: str,
    feature_snapshot_sha256: str,
    available_data_cutoff: str,
) -> dict[str, Any]:
    """Produce one validated single-symbol proposal, fail-closed everywhere.

    The injected transport is called at most once with the deeply frozen
    request mapping and never when pre-model eligibility fails (missing, stale,
    restricted, cutoff-inconsistent, or non-canonical snapshot input).
    Timeout, provider exception, and invalid output each collapse to a
    deterministic ABSTAIN with a distinct allowlisted reason; only output that
    passes the closed-schema validation can escape this boundary, and every
    returned proposal satisfies ``validate_proposal(..., mode="final")``.
    """
    if not callable(transport):
        raise ValueError("transport must be a callable accepting one frozen request mapping")
    request = build_proposal_request(
        feature_output,
        holdings,
        policy_limits=policy_limits,
        model_provider=model_provider,
        model_name=model_name,
        ksf_run_id=ksf_run_id,
        ksf_decision_id=ksf_decision_id,
        feature_snapshot_sha256=feature_snapshot_sha256,
        available_data_cutoff=available_data_cutoff,
    )
    reason = request["pre_model_abstain_reason"]
    if reason is not None:
        return build_abstain_proposal(request, reason)
    try:
        raw = transport(request)
    except TimeoutError:
        return build_abstain_proposal(request, "MODEL_TIMEOUT")
    except Exception:
        # The transport is an untrusted third-party boundary: any escape
        # (including a mutation attempt against the frozen request) is a
        # provider failure.  Content is intentionally neither logged nor
        # echoed, so nothing can leak into the abstention.
        return build_abstain_proposal(request, "MODEL_PROVIDER_ERROR")
    try:
        if raw == {"offline_deterministic_abstain_reason": "OFFLINE_SHADOW_MODE"}:
            return build_abstain_proposal(request, "OFFLINE_SHADOW_MODE")
        return validate_proposal(raw, request, mode="model")
    except (ValueError, TypeError, KeyError):
        # Expected validation failure shapes for arbitrary raw output; the
        # deterministic abstention never carries the raw content.
        return build_abstain_proposal(request, "MODEL_OUTPUT_INVALID")


__all__ = [
    "ABSTAIN_REASONS",
    "ACTIONS",
    "DATA_ABSTAIN_REASONS",
    "DATA_GAP_ITEM_KEYS",
    "HOLDINGS_KEYS",
    "IDENTITY_KEYS",
    "MODEL_ABSTAIN_REASONS",
    "MODEL_FAILURE_ABSTAIN_REASONS",
    "MODEL_POLICY_VERSION",
    "OFFLINE_ABSTAIN_REASONS",
    "POLICY_LIMIT_KEYS",
    "PROMPT_TEMPLATE_VERSION",
    "PROPOSAL_KEYS",
    "RESPONSE_SCHEMA_VERSION",
    "build_abstain_proposal",
    "build_proposal_request",
    "propose",
    "validate_proposal",
]
