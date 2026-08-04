"""Read-only presentation model for the K-Semiconductor Flow Desk."""
from __future__ import annotations

import json
import os
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
    return [value for value in values if not any(term in value.lower() for term in forbidden)]


def _feature_value(rows: dict[str, sqlite3.Row], aliases: tuple[str, ...]) -> dict[str, Any]:
    row = next((rows[name] for name in aliases if name in rows), None)
    if row is None:
        return {"value": None, "status": "MISSING"}
    value: Any = row["value_num"]
    if value is None:
        value = row["value_text"]
    if value is None and row["value_json"]:
        try:
            value = json.loads(row["value_json"])
        except json.JSONDecodeError:
            value = None
    return {
        "value": value,
        "status": row["feature_status"],
        "source_as_of": row["source_as_of"],
    }


def _empty_card(symbol: str, name: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "name": name,
        "data_quality": {
            "state": "no_data",
            "has_stale": False,
            "has_missing": True,
            "source_statuses": [],
        },
        "baseline": {
            "as_of_kst": None,
            "available_data_cutoff": None,
            "source_as_of": None,
        },
        "domestic_flow": {},
        "global_environment": {},
        "memory_indicators": {"license_blocked": False},
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
                      f.value_num, f.value_text, f.value_json, f.source_as_of,
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
                  source_as_of, source_as_of_jd
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
    evidence = _safe_explanation(_text_list(_json_list(ai["drivers_json"]))) if ai else []
    counters = _safe_explanation(_text_list(_json_list(ai["risks_json"]))) if ai else []
    statuses = [row["feature_status"] for row in feature_rows]
    has_stale = run["run_status"] == "STALE_DATA" or "STALE" in statuses
    has_missing = any(status in {"MISSING_OPTIONAL", "MISSING_REQUIRED", "LICENSE_BLOCKED"} for status in statuses)
    if run["run_status"] in {"PARTIAL_DATA", "MISSING_REQUIRED_DATA"}:
        has_missing = True
    state = "stale" if has_stale else "missing" if has_missing else "ready"
    source_as_of_values = [row for row in feature_rows if row["source_as_of"]]
    license_blocked = any(row["feature_status"] == "LICENSE_BLOCKED" for row in feature_rows)

    return {
        "symbol": symbol,
        "name": name,
        "data_quality": {
            "state": state,
            "has_stale": has_stale,
            "has_missing": has_missing,
            "run_status": run["run_status"],
            "source_statuses": [
                {
                    "source": row["source_name"],
                    "status": row["source_status"],
                    "quality": row["quality_grade"],
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
            **{key: _feature_value(features, aliases) for key, aliases in MEMORY_FEATURES.items()},
            "license_blocked": license_blocked,
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
