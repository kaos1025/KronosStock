"""Read-only, allowlisted presentation queries for the paper-trading ledger."""
from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator


SUPPORTED_SYMBOLS = frozenset({"005930", "000660"})
_SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")

ACTION = {
    "ENTER": ("enter", "진입 검토"), "HOLD": ("hold", "유지"),
    "REDUCE": ("reduce", "축소 검토"), "EXIT": ("exit", "청산 검토"),
    "ABSTAIN": ("abstain", "판단 보류"),
}
DECISION_REASON = {
    "FLOW_SIGNAL_POSITIVE": ("positive_flow", "긍정 흐름"),
    "FLOW_SIGNAL_NEGATIVE": ("negative_flow", "부정 흐름"),
    "NO_EDGE": ("no_edge", "뚜렷한 우위 없음"),
    "DATA_STALE": ("data_stale", "데이터 갱신 필요"),
    "DATA_MISSING": ("data_missing", "데이터 부족"),
    "DATA_RESTRICTED": ("data_restricted", "데이터 사용 제한"),
    "MODEL_UNAVAILABLE": ("model_unavailable", "분석 사용 불가"),
    "MODEL_OUTPUT_INVALID": ("model_output_invalid", "분석 결과 확인 불가"),
    "KILL_SWITCH_ACTIVE": ("safety_stop", "안전 중지 작동"),
    "RISK_LIMIT_REACHED": ("risk_limit", "위험 한도 도달"),
    "MARKET_CLOSED": ("market_closed", "시장 마감"),
}
RISK_REASON = {
    "TRADING_DISABLED": ("trading_disabled", "거래 비활성"),
    "KILL_SWITCH_ACTIVE": ("safety_stop", "안전 중지 작동"),
    "DUPLICATE_SESSION": ("duplicate_session", "이미 처리된 회차"),
    "STALE_DATA": ("data_stale", "데이터 갱신 필요"),
    "INVALID_ACTION": ("invalid_action", "처리할 수 없는 판단"),
    "INSUFFICIENT_CASH": ("insufficient_cash", "현금 부족"),
    "INSUFFICIENT_POSITION": ("insufficient_position", "보유 수량 부족"),
    "POSITION_LIMIT": ("position_limit", "종목 한도"),
    "EXPOSURE_LIMIT": ("exposure_limit", "노출 한도"),
    "DAILY_FILL_LIMIT": ("daily_fill_limit", "일일 체결 한도"),
    "DAILY_LOSS_STOP": ("daily_loss_stop", "일일 손실 한도"),
    "DRAWDOWN_STOP": ("drawdown_stop", "낙폭 한도"),
    "MIN_NOTIONAL": ("minimum_notional", "최소 금액 미달"),
}
VERDICT = {"APPROVE": ("approved", "승인"), "RESIZE": ("resized", "수량 조정"),
           "REJECT": ("rejected", "거절")}
LIFECYCLE_LABEL = {"ai_proposed": "AI 제안", "risk_rejected": "위험 검토 거절",
                   "queued": "체결 대기", "filled": "체결 완료", "not_filled": "미체결 종료",
                   "abstained": "판단 보류", "no_action": "조치 없음",
                   "unavailable": "확인 불가"}

_RUNTIME_COUNT_KEYS = ("nav_snapshots", "decisions", "cycle_commits", "proposals",
                       "reviews", "orders", "fills")


def _enum(mapping: dict[str, tuple[str, str]], value: object) -> tuple[str, str]:
    return mapping.get(value, ("unavailable", "확인 불가"))


def _scalar(value: object, *, sha: bool = False) -> str:
    if not isinstance(value, str):
        return "unavailable"
    pattern = _SHA if sha else _SAFE_ID
    return value if pattern.fullmatch(value) else "unavailable"


def _timestamp(value: object) -> str:
    if not isinstance(value, str) or len(value) > 40:
        return "unavailable"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return "unavailable"
    return value if parsed.tzinfo is not None and parsed.utcoffset() is not None else "unavailable"


@contextmanager
def _connection() -> Iterator[sqlite3.Connection | None]:
    raw_path = os.environ.get("PAPER_LEDGER_DB_PATH", "")
    account = os.environ.get("PAPER_ACCOUNT_ID", "")
    if not raw_path or not _SAFE_ID.fullmatch(account):
        yield None
        return
    path = Path(raw_path)
    if not path.is_file():
        yield None
        return
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
    except (OSError, sqlite3.Error):
        if conn is not None:
            conn.close()
        yield None
        return
    try:
        yield conn
    finally:
        if conn is not None:
            conn.close()


def _lifecycle(row: sqlite3.Row) -> str:
    if row["order_id"] is not None:
        event_type = row["latest_event_type"]
        has_fill = row["fill_id"] is not None
        if has_fill and event_type == "FILLED":
            return "filled"
        if not has_fill and event_type == "ACCEPTED":
            return "queued"
        if not has_fill and event_type in ("CANCELLED", "EXPIRED"):
            return "not_filled"
        return "unavailable"
    if row["verdict"] == "REJECT":
        return "risk_rejected"
    if row["proposal_id"] is not None:
        return "ai_proposed"
    if row["action"] == "ABSTAIN":
        return "abstained"
    if row["action"] == "HOLD":
        return "no_action"
    return "unavailable"


_LIFECYCLE_SQL = """
SELECT d.decision_id,d.symbol,d.created_at,d.action,d.reason_code,d.ksf_run_id,
 d.ksf_decision_id,d.feature_snapshot_sha256,d.available_data_cutoff,
 p.proposal_id,p.side,p.target_exposure_bp,p.proposed_at,
 r.verdict,r.reject_reason_code,r.reviewed_at,
 o.order_id,o.quantity,o.created_at AS order_created_at,
 (SELECT e.event_type FROM paper_order_events e WHERE e.order_id=o.order_id
  ORDER BY e.seq DESC LIMIT 1) AS latest_event_type,
 f.fill_id,f.fill_price_krw,f.quantity AS fill_quantity,f.fee_krw,f.tax_krw,f.filled_at
FROM paper_agent_decisions d
LEFT JOIN paper_order_proposals p ON p.decision_id=d.decision_id AND p.account_id=d.account_id
LEFT JOIN paper_risk_reviews r ON r.proposal_id=p.proposal_id
LEFT JOIN paper_orders o ON o.proposal_id=p.proposal_id AND o.account_id=d.account_id
LEFT JOIN paper_fills f ON f.order_id=o.order_id
WHERE d.account_id=? {extra}
ORDER BY julianday(d.created_at) DESC,d.created_at DESC,d.decision_id DESC
"""


def _public_row(row: sqlite3.Row) -> dict[str, object]:
    action, action_label = _enum(ACTION, row["action"])
    reason, reason_label = _enum(DECISION_REASON, row["reason_code"])
    status = _lifecycle(row)
    proposal = None if row["proposal_id"] is None else {
        "side": {"BUY": "buy", "SELL": "sell"}.get(row["side"], "unavailable"),
        "target_exposure_bps": row["target_exposure_bp"] if type(row["target_exposure_bp"]) is int else None,
        "proposed_at": _timestamp(row["proposed_at"]),
    }
    verdict, verdict_label = _enum(VERDICT, row["verdict"])
    risk_reason, risk_reason_label = _enum(RISK_REASON, row["reject_reason_code"])
    risk = None if row["verdict"] is None else {"verdict": verdict, "verdict_label": verdict_label,
        "reason": risk_reason if row["verdict"] == "REJECT" else None,
        "reason_label": risk_reason_label if row["verdict"] == "REJECT" else None,
        "reviewed_at": _timestamp(row["reviewed_at"])}
    order = None if row["order_id"] is None else {
        "quantity": row["quantity"] if type(row["quantity"]) is int else None,
        "created_at": _timestamp(row["order_created_at"])}
    fill = None if row["fill_id"] is None else {
        "price_krw": row["fill_price_krw"] if type(row["fill_price_krw"]) is int else None,
        "quantity": row["fill_quantity"] if type(row["fill_quantity"]) is int else None,
        "fee_krw": row["fee_krw"] if type(row["fee_krw"]) is int else None,
        "tax_krw": row["tax_krw"] if type(row["tax_krw"]) is int else None,
        "filled_at": _timestamp(row["filled_at"])}
    return {"decision_id": _scalar(row["decision_id"]), "symbol": row["symbol"] if row["symbol"] in SUPPORTED_SYMBOLS else "unavailable",
        "decided_at": _timestamp(row["created_at"]), "action": action, "action_label": action_label,
        "reason": reason, "reason_label": reason_label,
        "lineage": {"ksf_run_id": _scalar(row["ksf_run_id"]),
            "ksf_decision_id": _scalar(row["ksf_decision_id"]),
            "feature_snapshot_sha256": _scalar(row["feature_snapshot_sha256"], sha=True),
            "available_data_cutoff": _timestamp(row["available_data_cutoff"])},
        "proposal": proposal, "risk": risk, "order": order, "fill": fill,
        "lifecycle_status": status, "lifecycle_label": LIFECYCLE_LABEL[status]}


def get_orders() -> dict[str, object]:
    account = os.environ.get("PAPER_ACCOUNT_ID", "")
    with _connection() as conn:
        if conn is None:
            return {"status": "no_data", "orders": []}
        try:
            rows = conn.execute(_LIFECYCLE_SQL.format(extra="AND p.proposal_id IS NOT NULL"), (account,)).fetchall()
            return {"status": "ok", "orders": [_public_row(row) for row in rows]}
        except (sqlite3.Error, TypeError, ValueError, KeyError, json.JSONDecodeError):
            return {"status": "unavailable", "orders": []}


def get_decisions(symbol: str) -> dict[str, object]:
    account = os.environ.get("PAPER_ACCOUNT_ID", "")
    with _connection() as conn:
        if conn is None:
            return {"status": "no_data", "symbol": symbol, "decisions": []}
        try:
            rows = conn.execute(_LIFECYCLE_SQL.format(extra="AND d.symbol=?"), (account, symbol)).fetchall()
            return {"status": "ok", "symbol": symbol, "decisions": [_public_row(row) for row in rows]}
        except (sqlite3.Error, TypeError, ValueError, KeyError, json.JSONDecodeError):
            return {"status": "unavailable", "symbol": symbol, "decisions": []}


def _empty_runtime_status(status: str) -> dict[str, object]:
    counts = {key: 0 for key in _RUNTIME_COUNT_KEYS}
    return {"status": status, "latest_mode": None, "latest_session_id": None,
            "latest_committed_at": None, "latest_cycle_id": None,
            "latest_snapshot_at": None, "counts": counts,
            "safety": {"orders_zero": False, "fills_zero": False,
                       "proposals_zero": False, "reviews_zero": False,
                       "shadow_mode": None}, "recent_decisions": []}


def get_runtime_status() -> dict[str, object]:
    """Return a fixed, sanitized operational summary of the selected paper account."""
    account = os.environ.get("PAPER_ACCOUNT_ID", "")
    with _connection() as conn:
        if conn is None:
            return _empty_runtime_status("no_data")
        try:
            count_sql = {
                "nav_snapshots": "SELECT count(*) FROM paper_nav_snapshots WHERE account_id=?",
                "decisions": "SELECT count(*) FROM paper_agent_decisions WHERE account_id=?",
                "cycle_commits": "SELECT count(*) FROM paper_cycle_commits WHERE account_id=?",
                "proposals": "SELECT count(*) FROM paper_order_proposals WHERE account_id=?",
                "reviews": """SELECT count(*) FROM paper_risk_reviews r
                    JOIN paper_order_proposals p ON p.proposal_id=r.proposal_id WHERE p.account_id=?""",
                "orders": "SELECT count(*) FROM paper_orders WHERE account_id=?",
                "fills": """SELECT count(*) FROM paper_fills f
                    JOIN paper_orders o ON o.order_id=f.order_id WHERE o.account_id=?""",
            }
            counts = {key: conn.execute(sql, (account,)).fetchone()[0]
                      for key, sql in count_sql.items()}
            if any(type(value) is not int or value < 0 for value in counts.values()):
                raise ValueError("invalid count")
            cycle = conn.execute("""SELECT cycle_id,session_id,mode,committed_at
                FROM paper_cycle_commits WHERE account_id=?
                ORDER BY julianday(committed_at) DESC,committed_at DESC,cycle_id DESC LIMIT 1""",
                (account,)).fetchone()
            snapshot = conn.execute("""SELECT snapshot_at FROM paper_nav_snapshots WHERE account_id=?
                ORDER BY julianday(snapshot_at) DESC,snapshot_at DESC,nav_snapshot_id DESC LIMIT 1""",
                (account,)).fetchone()
            decision_rows = conn.execute("""SELECT decision_id,symbol,created_at,action,reason_code
                FROM paper_agent_decisions WHERE account_id=?
                ORDER BY julianday(created_at) DESC,created_at DESC,decision_id DESC LIMIT 10""",
                (account,)).fetchall()
            recent = []
            for row in decision_rows:
                action, action_label = _enum(ACTION, row["action"])
                reason, reason_label = _enum(DECISION_REASON, row["reason_code"])
                recent.append({"decision_id": _scalar(row["decision_id"]),
                    "symbol": row["symbol"] if row["symbol"] in SUPPORTED_SYMBOLS else "unavailable",
                    "decided_at": _timestamp(row["created_at"]), "action": action,
                    "action_label": action_label, "reason": reason, "reason_label": reason_label})
            mode = cycle["mode"] if cycle is not None and cycle["mode"] in ("shadow", "fills") else None
            result = _empty_runtime_status("ok" if any(counts.values()) else "no_data")
            result.update({"latest_mode": mode,
                "latest_session_id": _scalar(cycle["session_id"]) if cycle is not None else None,
                "latest_committed_at": _timestamp(cycle["committed_at"]) if cycle is not None else None,
                "latest_cycle_id": _scalar(cycle["cycle_id"]) if cycle is not None else None,
                "latest_snapshot_at": _timestamp(snapshot["snapshot_at"]) if snapshot is not None else None,
                "counts": counts, "safety": {"orders_zero": counts["orders"] == 0,
                    "fills_zero": counts["fills"] == 0, "proposals_zero": counts["proposals"] == 0,
                    "reviews_zero": counts["reviews"] == 0,
                    "shadow_mode": mode == "shadow" if mode is not None else None},
                "recent_decisions": recent})
            return result
        except (sqlite3.Error, TypeError, ValueError, KeyError):
            return _empty_runtime_status("unavailable")


def get_account() -> dict[str, object]:
    account = os.environ.get("PAPER_ACCOUNT_ID", "")
    with _connection() as conn:
        if conn is None:
            return {"status": "no_data"}
        try:
            acct = conn.execute("SELECT initial_cash_krw FROM paper_accounts WHERE account_id=?", (account,)).fetchone()
            nav = conn.execute("""SELECT * FROM paper_nav_snapshots WHERE account_id=?
                ORDER BY julianday(snapshot_at) DESC,
                CASE WHEN nav_snapshot_id LIKE 'paper-cycle-%:nav' THEN 1 ELSE 0 END DESC,
                snapshot_at DESC,nav_snapshot_id DESC LIMIT 1""", (account,)).fetchone()
            if acct is None or nav is None:
                return {"status": "no_data"}
            positions = {r["symbol"]: r["quantity"] for r in conn.execute("""SELECT symbol,SUM(quantity_delta) quantity
                FROM paper_position_events WHERE account_id=? AND julianday(event_at)<=julianday(?)
                GROUP BY symbol HAVING quantity != 0""", (account, nav["snapshot_at"]))}
            marks = json.loads(nav["position_marks_json"])
            if type(marks) is not dict or set(marks) != set(positions):
                raise ValueError("invalid marks")
            public_positions = []
            for symbol in sorted(positions):
                mark = marks[symbol]
                if symbol not in SUPPORTED_SYMBOLS or type(mark) is not dict or set(mark) != {"quantity", "price_krw", "feature_snapshot_sha256", "observed_at"}:
                    raise ValueError("invalid mark")
                quantity, price = positions[symbol], mark["price_krw"]
                if type(quantity) is not int or type(price) is not int or mark["quantity"] != quantity or quantity <= 0 or price <= 0:
                    raise ValueError("invalid position")
                public_positions.append({"symbol": symbol, "quantity": quantity, "mark_price_krw": price,
                                         "value_krw": quantity * price})
            exposure = sum(x["value_krw"] for x in public_positions)
            if exposure != nav["position_value_krw"]:
                raise ValueError("exposure mismatch")
            cash = conn.execute("""SELECT COALESCE(SUM(amount_krw),0) FROM paper_cash_events
                WHERE account_id=? AND julianday(event_at)<=julianday(?)""",
                (account, nav["snapshot_at"])).fetchone()[0]
            if type(cash) is not int or cash != nav["cash_krw"]:
                raise ValueError("cash mismatch")
            peak = conn.execute("""SELECT MAX(nav_krw) FROM (
                SELECT nav_krw,ROW_NUMBER() OVER (
                    PARTITION BY julianday(snapshot_at)
                    ORDER BY CASE WHEN nav_snapshot_id LIKE 'paper-cycle-%:nav' THEN 1 ELSE 0 END DESC,
                             snapshot_at DESC,nav_snapshot_id DESC) AS priority
                FROM paper_nav_snapshots
                WHERE account_id=? AND julianday(snapshot_at)<=julianday(?))
                WHERE priority=1""", (account, nav["snapshot_at"])).fetchone()[0]
            latest_nav, initial = nav["nav_krw"], acct["initial_cash_krw"]
            drawdown = peak - latest_nav if type(peak) is int and peak > latest_nav else 0
            drawdown_bps = drawdown * 10_000 // peak if type(peak) is int and peak > 0 else None
            kill = conn.execute("""SELECT event_type FROM paper_kill_switch_events WHERE account_id=?
                ORDER BY seq DESC LIMIT 1""", (account,)).fetchone()
            kill_state = "released" if kill is not None and kill[0] == "RELEASED" else "engaged"
            return {"status": "ok", "nav_krw": latest_nav, "cash_krw": nav["cash_krw"],
                "exposure_krw": exposure, "pnl_krw": latest_nav - initial,
                "drawdown_krw": drawdown, "drawdown_bps": drawdown_bps,
                "kill_switch": kill_state, "positions": public_positions,
                "snapshot_at": _timestamp(nav["snapshot_at"])}
        except (sqlite3.Error, TypeError, ValueError, KeyError, json.JSONDecodeError):
            return {"status": "unavailable"}
