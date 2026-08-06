"""Read-only presentation model for the K-Semiconductor Flow Desk."""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import quote


SUPPORTED_SYMBOLS = ("005930", "000660")
DEFAULT_LEDGER_PATH = Path(__file__).resolve().parents[1] / "data" / "ksf_ledger.sqlite3"

DOMESTIC_FEATURES = {
    "foreign": ("foreigner_net_buy_quantity", "foreign_net_flow", "foreign_net_buy_value"),
    "institution": ("institution_total_net_buy_quantity", "institution_net_flow", "institution_net_buy_value"),
    "individual": ("individual_net_buy_quantity", "individual_net_flow", "individual_net_buy_value"),
}
GLOBAL_FEATURES = {
    "nvda": ("nvda_adjusted_close", "nvda_close"),
    "soxx": ("soxx_adjusted_close", "soxx_close"),
    "tsm": ("tsm_twse_close", "tsm_adjusted_close", "tsm_close"),
    "usdkrw": ("usdkrw", "usdkrw_close"),
}
MEMORY_FEATURES = {
    "mu": ("mu_adjusted_close", "mu_close"),
    "dram": ("dram_spot_direction", "dram_price", "dram_spot_price"),
    "nand": ("nand_wafer_direction", "nand_price", "nand_spot_price"),
    "hbm": ("hbm_supply_tightness", "hbm_indicator"),
}

# 계약: LICENSE_BLOCKED taxonomy / display_policy / 정책 URL 은 HTML·JSON 응답에
# 절대 싣지 않는다. 외부로 나가는 상태는 일반화된 값으로만 표기한다.
_PUBLIC_UNAVAILABLE = "UNAVAILABLE"
_PUBLIC_RESTRICTED_SOURCE = "restricted_source"

# 공개 응답 금지 마커(소문자 비교). URL 은 스킴/`www.` 접두 형태만 매칭하는
# 보수적 전략 — trendforce_dramexchange_memory_license_review 같은
# 일반 벤더 라벨은 가리지 않는다.
_FORBIDDEN_PUBLIC_MARKERS = (
    "license_blocked",
    "display_policy",
    "do_not_display_or_score",
    "source_url",
    "terms_url",
    "http://",
    "https://",
    "www.",
)


def _has_forbidden_public_text(text: Any) -> bool:
    """공개 응답에 실리면 안 되는 taxonomy/정책/URL 마커 검사 (대소문자 무관)."""
    if not isinstance(text, str):
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in _FORBIDDEN_PUBLIC_MARKERS)


# 영숫자에 바로 이어지지 않는 private 차단 token. ``unblocked`` 같은 일반
# 단어에는 반응하지 않되 status 값과 ``vendor_blocked_feed`` 형태는 잡는다.
_PRIVATE_BLOCKED_TOKEN_RE = re.compile(r"(?<![0-9a-z])blocked", re.IGNORECASE)


def _has_private_marker(text: Any) -> bool:
    """모든 공개 경로가 공유하는 private taxonomy/policy marker predicate."""
    if not isinstance(text, str):
        return False
    return _has_forbidden_public_text(text) or bool(_PRIVATE_BLOCKED_TOKEN_RE.search(text))


def _is_blocked_marker(value: Any) -> bool:
    """Backward-compatible helper; 신규 코드는 통합 predicate를 직접 사용한다."""
    return _has_private_marker(value)


def _public_status(status: Any) -> Any:
    """원장 내부 상태를 공개 응답용 상태로 일반화한다 (대소문자·URL 변형 포함)."""
    if _has_private_marker(status):
        return _PUBLIC_UNAVAILABLE
    return status


def _public_source_name(name: Any) -> Any:
    """private marker를 담은 source_name은 원문 대신 일반 라벨로 대체한다."""
    return _PUBLIC_RESTRICTED_SOURCE if _has_private_marker(name) else name


def _is_restricted_source_row(row: sqlite3.Row) -> bool:
    """원시 소스 이름/상태/품질에서 차단 신호를 감지한다."""
    return any(
        _has_private_marker(value)
        for value in (row["source_name"], row["source_status"], row["quality_grade"])
    )


# 공개 허용 canonical 단위 (소문자 키 → canonical 표기). allowlist 밖 단위는
# 미상/악성으로 간주해 절대 노출하지 않는다.
_CANONICAL_UNITS = {
    "shares": "shares",
    "krw": "KRW",
    "usd": "USD",
    "twd": "TWD",
    "krw/usd": "KRW/USD",
    "bps": "bps",
}
# 원장 unit 이 NULL 일 때만 쓰는 보수적 이름 fallback — 단위가 자명한 별칭만.
# DRAM/NAND/HBM 지표, *_net_flow, TSMC ADR 계열(tsm_adjusted_close 등)은 추정 금지.
_UNIT_NAME_FALLBACKS = {
    "nvda_adjusted_close": "USD", "nvda_close": "USD",
    "soxx_adjusted_close": "USD", "soxx_close": "USD",
    "mu_adjusted_close": "USD", "mu_close": "USD",
    "tsm_twse_close": "TWD",
    "usdkrw": "KRW/USD", "usdkrw_close": "KRW/USD",
}


def _public_unit(unit: Any, feature_name: Any) -> str | None:
    """공개 가능한 canonical 단위를 고른다. 유효한 원장 단위 > 이름 fallback > None."""
    if unit is not None:
        # 단위 컬럼이 채워져 있으면 그 값만 판정한다 — allowlist 밖이면 행이 의심
        # 스럽다는 뜻이므로 이름 fallback 으로도 대체하지 않는다 (fail-closed).
        if not isinstance(unit, str):
            return None
        return _CANONICAL_UNITS.get(unit.strip().lower())
    name = feature_name.strip().lower() if isinstance(feature_name, str) else ""
    if name.endswith("_net_buy_quantity"):
        return "shares"
    if name.endswith("_net_buy_value"):
        return "KRW"
    return _UNIT_NAME_FALLBACKS.get(name)


def ledger_path() -> Path:
    return Path(os.environ.get("KSF_LEDGER_DB_PATH", str(DEFAULT_LEDGER_PATH)))


def _connect(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _json_list(raw: str | None) -> list[Any]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def _text_list(values: list[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        if isinstance(value, str) and value.strip():
            result.append(value.strip())
        elif isinstance(value, dict):
            text = value.get("text")
            if isinstance(text, str) and text.strip():
                result.append(text.strip())
    return result


def _safe_explanation(values: list[str]) -> list[str]:
    forbidden = ("rank", "relative", "pair", "order", "buy", "sell", "매수", "매도", "상대", "대비", "비교", "순위")
    return [
        value
        for value in values
        # taxonomy/정책/URL 마커가 섞인 설명은 문자열 전체를 폐기한다.
        if not _has_private_marker(value)
        and not any(term in value.lower() for term in forbidden)
    ]


def _feature_value(rows: dict[str, sqlite3.Row], aliases: tuple[str, ...]) -> dict[str, Any]:
    matched_name = next((name for name in aliases if name in rows), None)
    if matched_name is None:
        return {"value": None, "status": "MISSING"}
    row = rows[matched_name]
    status = row["feature_status"]
    if _has_private_marker(status):
        # 차단 항목(대소문자/rogue 상태 포함)은 value_json 을 파싱하지 않는다 —
        # 값/메타데이터 전부 비노출. 원시 taxonomy 도 계약 위반이므로 상태 역시
        # 일반화해서 내보낸다.
        return {"value": None, "status": _PUBLIC_UNAVAILABLE, "source_as_of": row["source_as_of"]}
    payload: Any = None
    if row["value_json"]:
        try:
            payload = json.loads(row["value_json"])
        except json.JSONDecodeError:
            if _has_private_marker(row["value_json"]):
                # 파싱은 안 되지만 정책/URL 마커가 보이는 payload — 스칼라 fallback
                # 없이 fail-closed 한다 (malformed 정책 JSON 으로 억제를 우회 금지).
                return {"value": None, "status": _PUBLIC_UNAVAILABLE, "source_as_of": row["source_as_of"]}
            # 정책과 무관하게 깨진 metadata — 아래 스칼라 컬럼 값은 그대로 유효.
            payload = None
    if isinstance(payload, dict):
        # display_policy 는 value_num/value_text 존재 여부와 무관하게 항상 먼저 평가한다.
        # 키/값 모두 대소문자 변형을 방어한다 — 서로 다른 케이스의 중복 키가 있어도
        # 하나라도 차단을 명시하면 fail-closed.
        if any(
            isinstance(key, str) and key.lower() == "display_policy"
            and isinstance(policy, str) and policy.lower() == "do_not_display_or_score"
            for key, policy in payload.items()
        ):
            return {"value": None, "status": status, "source_as_of": row["source_as_of"]}
        # dict payload 는 metadata 컨테이너 — URL/정책 문자열이 실릴 수 있으므로
        # 원본 dict 를 그대로 노출하지 않고 "value" 키만 꺼낸다.
        payload = payload.get("value")
    if not isinstance(payload, (int, float, str)):
        payload = None  # 스칼라만 노출 — 컨테이너형 payload 는 metadata 로 간주
    value: Any = row["value_num"]
    if value is None:
        value = row["value_text"]
    if value is None:
        value = payload
    if isinstance(value, float) and not math.isfinite(value):
        value = None  # inf/nan 은 JSON 직렬화 불가 — 값 없음으로 취급
    result: dict[str, Any] = {
        "value": value,
        "status": status,
        "source_as_of": row["source_as_of"],
    }
    # 단위는 실제 노출되는 값이 있을 때만 싣는다 — 차단/결측 항목은 단위도 숨긴다.
    unit = _public_unit(row["unit"], matched_name)
    if value is not None and unit is not None:
        result["unit"] = unit
    return result


def _empty_card(symbol: str, name: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "name": name,
        "data_quality": {
            "state": "no_data",
            "has_stale": False,
            "has_missing": True,
            "has_restricted": False,
            "source_statuses": [],
        },
        "baseline": {
            "as_of_kst": None,
            "available_data_cutoff": None,
            "source_as_of": None,
        },
        "domestic_flow": {},
        "global_environment": {},
        "memory_indicators": {},
        "evidence": [],
        "counterarguments": ["확인 가능한 기준 데이터가 없습니다."],
        "prior_performance": [],
    }


def _fallback_explanation(decision: sqlite3.Row | None) -> tuple[list[str], list[str]]:
    if decision is None:
        return [], ["판단 기록이 없습니다."]
    contributions = _json_list(decision["feature_contributions_json"])
    positive: list[str] = []
    negative: list[str] = []
    for item in contributions:
        if not isinstance(item, dict):
            continue
        value = item.get("contribution", item.get("contribution_bps", 0))
        try:
            destination = positive if float(value) >= 0 else negative
            destination.append(f"원장 지표 기여도 {value}")
        except (TypeError, ValueError):
            negative.append("원장 지표 기여도 확인 필요")
    return positive, negative or ["반대 근거가 별도로 기록되지 않았습니다."]


def _build_card(conn: sqlite3.Connection, symbol: str) -> dict[str, Any]:
    symbol_row = conn.execute(
        "SELECT name_ko FROM ksf_symbols WHERE symbol = ?", (symbol,)
    ).fetchone()
    name = symbol_row["name_ko"] if symbol_row else symbol
    run = conn.execute(
        """SELECT * FROM ksf_runs
           WHERE symbol = ? AND run_status != 'ARCHIVED'
           ORDER BY julianday(as_of_kst) DESC, run_id DESC LIMIT 1""",
        (symbol,),
    ).fetchone()
    if run is None:
        return _empty_card(symbol, name)

    feature_rows = conn.execute(
        """WITH eligible AS (
               SELECT f.feature_id, f.feature_name, f.feature_status,
                      f.value_num, f.value_text, f.value_json, f.unit, f.source_as_of,
                      julianday(f.source_as_of) AS source_as_of_jd,
                      ROW_NUMBER() OVER (
                          PARTITION BY f.feature_name
                          ORDER BY julianday(f.source_as_of) DESC,
                                   julianday(r.as_of_kst) DESC,
                                   julianday(f.ingested_at_kst) DESC,
                                   r.run_id DESC, f.feature_id DESC
                      ) AS precedence
               FROM ksf_normalized_features AS f
               JOIN ksf_runs AS r ON r.run_id = f.run_id
               WHERE f.symbol = ? AND r.symbol = ?
                 AND r.run_status != 'ARCHIVED'
                 AND julianday(r.available_data_cutoff) <= julianday(?)
                 AND julianday(f.available_data_cutoff) <= julianday(?)
                 AND julianday(f.source_as_of) <= julianday(?)
                 AND julianday(f.ingested_at_kst) <= julianday(?)
           )
           SELECT feature_name, feature_status, value_num, value_text, value_json,
                  unit, source_as_of, source_as_of_jd
           FROM eligible WHERE precedence = 1""",
        (symbol, symbol, *([run["available_data_cutoff"]] * 4)),
    ).fetchall()
    features = {row["feature_name"]: row for row in feature_rows}
    decision = conn.execute(
        """SELECT * FROM ksf_decisions WHERE run_id = ? AND symbol = ?
           ORDER BY horizon_days LIMIT 1""",
        (run["run_id"], symbol),
    ).fetchone()
    ai = conn.execute(
        """SELECT drivers_json, risks_json FROM ksf_ai_responses
           WHERE run_id = ? AND symbol = ? AND response_status = 'OK'
           ORDER BY created_at_kst DESC LIMIT 1""",
        (run["run_id"], symbol),
    ).fetchone()
    source_rows = conn.execute(
        """WITH eligible AS (
               SELECT s.source_name, s.source_status, s.quality_grade, s.source_as_of,
                      ROW_NUMBER() OVER (
                          PARTITION BY s.source_name
                          ORDER BY julianday(s.source_as_of) DESC,
                                   julianday(s.ingested_at_kst) DESC,
                                   s.snapshot_id DESC
                      ) AS precedence
               FROM ksf_source_snapshots AS s
               LEFT JOIN ksf_runs AS r ON r.run_id = s.run_id
               WHERE (s.run_id IS NULL AND (s.symbol = ? OR s.symbol IS NULL)
                      OR r.symbol = ? AND r.run_status != 'ARCHIVED')
                 AND julianday(s.available_data_cutoff) <= julianday(?)
                 AND julianday(s.source_as_of) <= julianday(?)
                 AND julianday(s.ingested_at_kst) <= julianday(?)
           )
           SELECT source_name, source_status, quality_grade, source_as_of
           FROM eligible WHERE precedence = 1 ORDER BY source_name""",
        (symbol, symbol, *([run["available_data_cutoff"]] * 3)),
    ).fetchall()
    settlements = conn.execute(
        """SELECT horizon_days, settlement_status, direction_hit, return_bps
           FROM ksf_performance_settlements WHERE symbol = ?
           ORDER BY base_trade_date DESC, horizon_days LIMIT 30""",
        (symbol,),
    ).fetchall()

    fallback_evidence, fallback_counters = _fallback_explanation(decision)
    raw_evidence = _text_list(_json_list(ai["drivers_json"])) if ai else []
    raw_counters = _text_list(_json_list(ai["risks_json"])) if ai else []
    explanation_restricted = any(
        _has_private_marker(value) for value in (*raw_evidence, *raw_counters)
    )
    evidence = _safe_explanation(raw_evidence)
    counters = _safe_explanation(raw_counters)
    # rogue 대소문자 변형이 들어와도 결측/신선도 집계는 정직하게 유지한다.
    run_status = str(run["run_status"] or "").strip().upper()
    statuses = [
        status.strip().upper() if isinstance(status, str) else status
        for status in (row["feature_status"] for row in feature_rows)
    ]
    has_stale = run_status == "STALE_DATA" or "STALE" in statuses
    has_missing = any(
        status in {"MISSING_OPTIONAL", "MISSING_REQUIRED", "LICENSE_BLOCKED"}
        or _has_private_marker(status)
        for status in statuses
    )
    if run_status in {"PARTIAL_DATA", "MISSING_REQUIRED_DATA"}:
        has_missing = True
    # 차단 항목 존재 여부는 boolean 으로만 정직하게 노출한다. run/피처/소스/
    # 설명 경로 모두 같은 predicate를 사용해 raw taxonomy 우회를 막는다.
    has_restricted = (
        _has_private_marker(run["run_status"])
        or any(
            _has_private_marker(row["feature_status"])
            or _has_private_marker(row["value_json"])
            for row in feature_rows
        )
        or any(_is_restricted_source_row(row) for row in source_rows)
        or explanation_restricted
    )
    # 공개 상태 우선순위: stale > missing(필수) > partial > restricted > ready.
    missing_required = (
        run_status in {"MISSING_REQUIRED_DATA", "MISSING_REQUIRED"}
        or "MISSING_REQUIRED" in statuses
    )
    partial = run_status == "PARTIAL_DATA" or "MISSING_OPTIONAL" in statuses
    if has_stale:
        state = "stale"
    elif missing_required:
        state = "missing"
    elif partial:
        state = "partial"
    elif has_restricted:
        state = "restricted"
    else:
        state = "ready"
    source_as_of_values = [row for row in feature_rows if row["source_as_of"]]

    return {
        "symbol": symbol,
        "name": name,
        "data_quality": {
            "state": state,
            "has_stale": has_stale,
            "has_missing": has_missing,
            "has_restricted": has_restricted,
            "run_status": _public_status(run["run_status"]),
            "source_statuses": [
                {
                    # 금지 텍스트를 담은 source_name 은 원문 대신 일반 라벨만 노출.
                    "source": _public_source_name(row["source_name"]),
                    # rogue/미래 상태값이 원장에 들어와도 taxonomy 는 새지 않는다.
                    "status": _public_status(row["source_status"]),
                    # 스키마 유효 차단 등급(BLOCKED)도 원문 대신 일반화해서 내보낸다.
                    "quality": _public_status(row["quality_grade"]),
                    "source_as_of": row["source_as_of"],
                }
                for row in source_rows
            ],
        },
        "baseline": {
            "as_of_kst": run["as_of_kst"],
            "available_data_cutoff": run["available_data_cutoff"],
            "source_as_of": max(source_as_of_values, key=lambda row: row["source_as_of_jd"])["source_as_of"]
            if source_as_of_values else None,
        },
        "domestic_flow": {
            key: _feature_value(features, aliases) for key, aliases in DOMESTIC_FEATURES.items()
        },
        "global_environment": {
            key: _feature_value(features, aliases) for key, aliases in GLOBAL_FEATURES.items()
        },
        "memory_indicators": {
            key: _feature_value(features, aliases) for key, aliases in MEMORY_FEATURES.items()
        },
        "evidence": evidence or fallback_evidence,
        "counterarguments": counters or fallback_counters,
        "prior_performance": [
            {
                "horizon_days": row["horizon_days"],
                "status": row["settlement_status"],
                "direction_hit": None if row["direction_hit"] is None else bool(row["direction_hit"]),
                "return_bps": row["return_bps"],
            }
            for row in settlements
        ],
    }


def get_cards(path: Path | None = None) -> list[dict[str, Any]]:
    db_path = path or ledger_path()
    try:
        with closing(_connect(db_path)) as conn:
            return [_build_card(conn, symbol) for symbol in SUPPORTED_SYMBOLS]
    except (sqlite3.Error, OSError):
        return [_empty_card(symbol, symbol) for symbol in SUPPORTED_SYMBOLS]


def get_detail(symbol: str, path: Path | None = None) -> dict[str, Any] | None:
    if symbol not in SUPPORTED_SYMBOLS:
        return None
    return next(card for card in get_cards(path) if card["symbol"] == symbol)
