"""최소 FastAPI 대시보드 — 스캐폴드 단계의 헬스/상태 확인용.

비밀값은 절대 노출하지 않는다(설정 여부 불리언만 표시).
로컬 실행:  uvicorn dashboard.app:app --reload
"""
from __future__ import annotations

import base64
import binascii
import json
import html
import math
import os
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse

from common.config import settings
from common.redis_client import get_redis, key, ping as redis_ping
from common.symbols import symbol_name
from strategy.analyzer import analyze_forecast
from ksf.web_api import get_cards, get_detail

app = FastAPI(title="KronosStock", version="0.1.0")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/status")
def status() -> dict:
    """통합 구성 상태(비밀값 비노출)."""
    return {
        "kis_real_configured": settings.kis_configured,
        "kis_virtual_configured": settings.kis_virtual_configured,
        "kis_use_virtual": settings.kis_use_virtual,
        "telegram_configured": settings.telegram_configured,
        "redis_connected": redis_ping(),
        "watchlist": settings.watchlist,
        "model": settings.kronos_model_repo,
        "device": settings.kronos_device,
    }


@app.get("/forecast/{code}")
def forecast(code: str) -> dict:
    """Redis 에 저장된 forecast payload 조회(비밀값 노출 없음)."""
    raw = get_redis().get(key("forecast", "daily", code))
    if not raw:
        raise HTTPException(status_code=404, detail=f"forecast not found: {code}")
    return json.loads(raw)


@app.get("/signal/{code}")
def signal(code: str) -> dict:
    """저장된 forecast payload 를 BUY/HOLD/SELL 시그널로 변환."""
    payload = forecast(code)
    data = analyze_forecast(payload).as_dict()
    data["name"] = symbol_name(code)
    return data


# scheduler dry-run 이 저장하는 paper portfolio snapshot 키와 동일 네임스페이스.
PAPER_PORTFOLIO_KEY = key("paper", "portfolio")


@app.get("/paper/portfolio")
def paper_portfolio() -> dict:
    """scheduler dry-run 이 저장한 paper portfolio snapshot 조회.

    Redis 에 snapshot 이 없으면 404. 비밀값/토큰은 저장하지 않으므로 노출 위험 없음
    (현금·보유수량·체결기록만 반환).
    """
    raw = get_redis().get(PAPER_PORTFOLIO_KEY)
    if not raw:
        raise HTTPException(status_code=404, detail="paper portfolio snapshot not found")
    return json.loads(raw)


def _bearer_valid(supplied: str) -> bool:
    expected = os.environ.get("KSF_READ_TOKEN", "")
    try:
        return bool(expected and supplied) and secrets.compare_digest(
            supplied.encode("utf-8"), expected.encode("utf-8")
        )
    except UnicodeError:
        return False


def _basic_valid(payload: str) -> bool:
    expected_user = os.environ.get("KSF_READ_USERNAME", "")
    expected_password = os.environ.get("KSF_READ_PASSWORD", "")
    if not (expected_user and expected_password):
        return False  # 부분/미설정 구성이면 무조건 거부 (fail-closed)
    try:
        decoded = base64.b64decode(payload.encode("ascii"), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeError, ValueError):
        return False
    username, sep, password = decoded.partition(":")
    if not sep:
        return False
    try:
        # env 값에 서러게이트 등 인코딩 불가 문자가 있으면 500 대신 거부 (fail-closed)
        expected_user_bytes = expected_user.encode("utf-8")
        expected_password_bytes = expected_password.encode("utf-8")
    except UnicodeError:
        return False
    # 비단락 & 로 두 비교를 항상 수행 (타이밍 차이 최소화)
    return secrets.compare_digest(
        username.encode("utf-8"), expected_user_bytes
    ) & secrets.compare_digest(
        password.encode("utf-8"), expected_password_bytes
    )


def _require_ksf_auth(authorization: str | None = Header(default=None)) -> None:
    # RFC 9110: 스킴은 대소문자 무시. 스킴만 정규화하고 자격증명 파싱은 엄격 유지.
    scheme, _, credential = (authorization or "").partition(" ")
    scheme = scheme.lower()
    if scheme == "bearer" and _bearer_valid(credential):
        return
    if scheme == "basic" and _basic_valid(credential):
        return
    raise HTTPException(
        status_code=401,
        detail="authentication required",
        headers={
            "WWW-Authenticate": 'Basic realm="KronosStock KSF", charset="UTF-8", '
            'Bearer realm="KronosStock KSF"'
        },
    )


@app.get("/ksf/cards")
def ksf_cards(_: None = Depends(_require_ksf_auth)) -> dict:
    return {"cards": get_cards()}


@app.get("/ksf/cards/{symbol}")
def ksf_card_detail(symbol: str, _: None = Depends(_require_ksf_auth)) -> dict:
    detail = get_detail(symbol)
    if detail is None:
        raise HTTPException(status_code=404, detail="card not found")
    return detail


# --- B 리서치 데스크 (서버 렌더링, JS 없음) -----------------------------------
# 원장 내부 상태 문자열은 화면에 절대 싣지 않고 공개 한국어 라벨로만 표기한다.
# 주의: 공개 응답 금지어 계약 때문에 CSS 에서도 "border" 계열 속성을 쓰지 않는다
# (box-shadow inset 으로 대체). 원격 자산·그라데이션·폼·버튼도 금지.

_STATE_HEADLINES = {
    "ready": "필수 데이터 준비",
    "partial": "일부 데이터 준비",
    "missing": "필수 데이터 부족",
    "stale": "업데이트 필요",
    "restricted": "사용 제한",
    "no_data": "데이터 없음",
}
_STATE_CLASSES = {
    "ready": "st-ready",
    "partial": "st-partial",
    "missing": "st-missing",
    "stale": "st-stale",
    "restricted": "st-hold",
    "no_data": "st-none",
}
_RESTRICTED_QUALIFIER = "사용 제한 항목 있음"

# 항목 단위 공개 라벨: (라벨, 태그 클래스). 모르는 상태는 원문 대신 "수집 대기".
_ITEM_STATUS_LABELS = {
    "READY": ("준비됨", "ok"),
    "STALE": ("업데이트 필요", "warn"),
    "UNAVAILABLE": ("사용 제한", "hold"),
    "INTRADAY_ESTIMATE": ("장중 추정", "est"),
    "CLOSE_CONFIRMED": ("종가 확정", "ok"),
}
_ITEM_STATUS_FALLBACK = ("수집 대기", "wait")

_SETTLE_LABELS = {"SETTLED": "정산 완료", "PENDING": "정산 대기", "SKIPPED": "건너뜀"}

# (card 키, 그룹 라벨, ((항목 키, 항목 라벨), ...), 부호 표시 여부)
# 국내 수급 라벨은 단위 중립(순수급) — 단위는 값 옆의 명시적 단위 라벨로만 표기한다.
_GROUPS = (
    ("domestic_flow", "핵심 수급",
     (("foreign", "외국인 순수급"), ("institution", "기관 순수급"), ("individual", "개인 순수급")), True),
    ("global_environment", "글로벌 환경",
     (("nvda", "NVDA"), ("soxx", "SOXX"), ("tsm", "TSMC 2330"), ("usdkrw", "원/달러 환율")), False),
    ("memory_indicators", "메모리 지표",
     (("mu", "MU"), ("dram", "DRAM 현물"), ("nand", "NAND 현물"), ("hbm", "HBM")), False),
)

# web_api 가 노출하는 canonical 단위 → 화면 단위 라벨. 이 표 밖의 단위 문자열은
# 원장 유래 값이므로 그대로 렌더링하지 않는다 (단위 미제공으로 처리).
_UNIT_LABELS = {
    "shares": "주",
    "KRW": "원",
    "USD": "USD",
    "TWD": "TWD",
    "KRW/USD": "원/USD",
    "bps": "bp",
}
_NO_UNIT_LABEL = "단위 미제공"

_KST = timezone(timedelta(hours=9))

_DESK_CSS = """
body{margin:0;background:#14171c;color:#e8eaee;font:15px/1.6 "Apple SD Gothic Neo","Noto Sans KR","Malgun Gothic",system-ui,sans-serif}
.top,main,.foot{max-width:1060px;margin:0 auto;padding:14px 16px}
a{color:#6f9bd6;font-weight:600;text-decoration:none}
a:focus-visible{outline:2px solid #6f9bd6;outline-offset:2px}
nav a{display:inline-flex;align-items:center;min-height:44px;margin:0 16px 0 0}
h1{margin:6px 0 8px;font-size:1.45rem;letter-spacing:-0.01em}
h2{margin:0 0 10px;font-size:0.82rem;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;color:#626c7c}
h3{margin:0 0 6px;font-size:0.85rem;color:#9aa3b2}
.brand,.code,small{color:#626c7c;font-size:0.8rem}
.badge{display:inline-block;padding:3px 10px;background:#20252e;box-shadow:inset 0 0 0 1px #2c333e;color:#9aa3b2;font-size:0.78rem}
.headline{margin:4px 0;font-weight:700;font-size:1.05rem}
.qualifier{margin:2px 0;color:#ab8abd;font-size:0.85rem}
.st-ready{color:#5bb08c}.st-partial,.st-stale{color:#cfa457}
.st-missing{color:#e0707d}.st-hold{color:#ab8abd}.st-none{color:#9aa3b2}
.cutoff{color:#9aa3b2;font-size:0.82rem;overflow-wrap:anywhere}
section{margin:0 0 24px}
.covnum{font-weight:400;color:#9aa3b2;letter-spacing:0;text-transform:none}
.grid{list-style:none;margin:0;padding:0;display:grid;gap:10px}
.metric{background:#1b1f26;box-shadow:inset 0 0 0 1px #2c333e;padding:12px 14px;min-height:84px;display:flex;flex-direction:column;gap:4px;min-width:0;overflow-wrap:anywhere}
.metric .label{color:#9aa3b2;font-size:0.85rem;display:flex;justify-content:space-between;gap:8px;min-width:0}
.metric .value{margin-top:auto;font-family:ui-monospace,Consolas,monospace;font-size:1.3rem;font-weight:600;min-width:0;overflow-wrap:anywhere}
.metric .unit{color:#626c7c;font-size:0.78rem}
.tag{font-style:normal;font-size:0.72rem;padding:1px 7px;background:#20252e;white-space:nowrap}
.tag.ok{color:#5bb08c}.tag.warn{color:#cfa457}.tag.wait{color:#cfa457}
.tag.hold{color:#ab8abd}.tag.est{color:#9aa3b2}
.pos{color:#e0707d}.neg{color:#7ea3dd}
.cards{display:grid;gap:14px}
.card{background:#1b1f26;box-shadow:inset 0 0 0 1px #2c333e;padding:18px;min-width:0}
.card h2{font-size:1.15rem;letter-spacing:0;text-transform:none;color:#e8eaee;margin:0 0 6px}
.meta{display:grid;grid-template-columns:max-content 1fr;gap:4px 12px;margin:10px 0;font-size:0.85rem}
.meta dt{color:#626c7c}.meta dd{margin:0;color:#9aa3b2;overflow-wrap:anywhere}
.cov-list{list-style:none;margin:0 0 12px;padding:0}
.cov-list li{display:flex;justify-content:space-between;gap:10px;padding:6px 0;box-shadow:inset 0 1px 0 #2c333e;font-size:0.88rem;color:#9aa3b2;min-width:0}
.cov-list li span,.cov-list li strong{min-width:0;overflow-wrap:anywhere}
.cov-list strong{color:#e8eaee;font-family:ui-monospace,Consolas,monospace;font-weight:600}
.trio{display:grid;gap:10px}
.rec{background:#1b1f26;box-shadow:inset 0 0 0 1px #2c333e;padding:12px 14px;min-width:0}
.rec ul{margin:0;padding-left:16px;font-size:0.85rem}
.rec li{margin:4px 0;overflow-wrap:anywhere}
li.none{color:#626c7c;list-style:none;margin-left:-16px}
.note,.foot p{color:#626c7c;font-size:0.8rem}
@media (min-width:700px){.grid{grid-template-columns:repeat(auto-fill,minmax(200px,1fr))}.cards{grid-template-columns:1fr 1fr}.trio{grid-template-columns:repeat(3,1fr)}}
"""


def _fmt_value(value, *, signed: bool = False) -> str:
    """원장 스칼라를 스캔하기 좋은 문자열로 (천단위 구분, 수급은 부호 표기)."""
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "예" if value else "아니요"
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            return "—"
        text = f"{abs(number):,.2f}".rstrip("0").rstrip(".")
        if number < 0:
            return f"-{text}"
        if signed and number > 0:
            return f"+{text}"
        return text
    return str(value)


def _fmt_ts(value, *, full: bool = True) -> str:
    """ISO 시각을 KST 로 변환해 표기하고 KST 라벨을 정확히 1회 붙인다.

    tz 정보가 없거나 파싱이 안 되는 값은 KST 로 오인 표기하지 않는다
    (원문은 호출부에서 escape 되어 렌더링된다).
    """
    if not isinstance(value, str) or not value.strip():
        return "—"
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return text
    date_only = "T" not in text and " " not in text
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(_KST)
    date = parsed.strftime("%Y.%m.%d") if full else parsed.strftime("%m.%d")
    if date_only:
        return date
    suffix = " KST" if parsed.tzinfo is not None else ""
    return f"{date} {parsed.strftime('%H:%M')}{suffix}"


def _headline_html(quality: dict) -> str:
    state = str(quality.get("state") or "")
    headline = _STATE_HEADLINES.get(state, _STATE_HEADLINES["no_data"])
    state_cls = _STATE_CLASSES.get(state, "st-none")
    parts = [f'<p class="headline {state_cls}">{headline}</p>']
    if quality.get("has_restricted"):
        # 1차 상태와 분리된 2차 한정어 — 차단 항목 존재만 알리고 사유는 싣지 않는다.
        parts.append(f'<p class="qualifier">{_RESTRICTED_QUALIFIER}</p>')
    return "".join(parts)


def _coverage(card: dict, group_key: str, items: tuple) -> tuple[int, int]:
    values = card.get(group_key) or {}
    ready = sum(
        1
        for key, _ in items
        if isinstance(values.get(key), dict)
        and isinstance(values[key].get("status"), str)
        and values[key]["status"].strip().upper() == "READY"
    )
    return ready, len(items)


def _metric_html(values: dict, key: str, label: str, *, signed: bool) -> str:
    item = values.get(key) if isinstance(values, dict) else None
    item = item if isinstance(item, dict) else {}
    status = item.get("status")
    status_key = status.strip().upper() if isinstance(status, str) else ""
    status_label, tag_cls = _ITEM_STATUS_LABELS.get(status_key, _ITEM_STATUS_FALLBACK)
    value = item.get("value")
    finite_number = (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )
    value_cls = ""
    if signed and finite_number:
        value_cls = " pos" if value > 0 else " neg" if value < 0 else ""
    unit = item.get("unit")
    unit_label = _UNIT_LABELS.get(unit) if isinstance(unit, str) else None
    if finite_number:
        # 숫자값은 항상 단위를 명시한다 — 안전 단위가 없으면 암시하지 않고 알린다.
        unit_html = (
            f'<span class="unit">{html.escape(unit_label)}</span>'
            if unit_label else f'<span class="unit missing">{_NO_UNIT_LABEL}</span>'
        )
    else:
        unit_html = ""
    source = item.get("source_as_of")
    source_html = (
        f"<small>원천 기준 {html.escape(_fmt_ts(source, full=False))}</small>"
        if isinstance(source, str) and source else ""
    )
    return (
        f'<li class="metric"><span class="label">{html.escape(label)}'
        f'<em class="tag {tag_cls}">{html.escape(status_label)}</em></span>'
        f'<strong class="value{value_cls}">{html.escape(_fmt_value(value, signed=signed))}</strong>'
        f"{unit_html}{source_html}</li>"
    )


def _prior_html(records: list) -> str:
    rows = []
    for record in records:
        if not isinstance(record, dict):
            continue
        settle_label = _SETTLE_LABELS.get(
            str(record.get("status") or "").strip().upper(), "기록됨"
        )
        hit = record.get("direction_hit")
        hit_label = "방향 적중" if hit is True else "방향 어긋남" if hit is False else "판정 대기"
        return_bps = record.get("return_bps")
        return_text = f"{_fmt_value(return_bps, signed=True)}bp" if return_bps is not None else "—"
        rows.append(
            f"<li>T+{html.escape(_fmt_value(record.get('horizon_days')))} · "
            f"{html.escape(settle_label)} · {html.escape(hit_label)} · "
            f"{html.escape(return_text)}</li>"
        )
    return "".join(rows) or '<li class="none">기록 없음</li>'


def _text_items(values: list) -> str:
    rows = "".join(
        f"<li>{html.escape(str(value))}</li>" for value in values if isinstance(value, str)
    )
    return rows or '<li class="none">기록 없음</li>'


def _symbol_href(symbol, *, fragment: bool = False) -> str:
    """href/fragment 에 들어가는 심볼은 항상 URL 인코딩한다 (속성 escape 는 별도)."""
    encoded = quote(str(symbol or ""), safe="")
    return f"#sym-{encoded}" if fragment else f"/ksf/{encoded}"


def _symbol_link(card: dict, href: str) -> str:
    name = str(card.get("name") or card.get("symbol") or "")
    symbol = str(card.get("symbol") or "")
    return f'<a href="{html.escape(href)}">{html.escape(name)} {html.escape(symbol)}</a>'


def _document(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f'<!doctype html><html lang="ko"><head><meta charset="utf-8">\n'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{html.escape(title)}</title><style>{_DESK_CSS}</style></head>\n"
        f'<body>{body}<footer class="foot">'
        f"<p>KronosStock K-반도체 플로우 데스크 · 읽기 전용 리서치 화면이며 투자 판단이나 권유가 아닙니다.</p>"
        f"</footer></body></html>"
    )


def _landing_card(card: dict) -> str:
    quality = card.get("data_quality") or {}
    baseline = card.get("baseline") or {}
    symbol = str(card.get("symbol") or "")
    coverage_rows = "".join(
        f"<li><span>{label}</span><strong>{_coverage(card, key, items)[0]}/{len(items)} 준비</strong></li>"
        for key, label, items, _ in _GROUPS
    )
    return (
        f'<article class="card" id="sym-{html.escape(symbol)}">'
        f'<p class="code">{html.escape(symbol)} · KRX</p>'
        f"<h2>{html.escape(str(card.get('name') or symbol))}</h2>"
        f"{_headline_html(quality)}"
        f'<dl class="meta"><dt>판단 시각</dt><dd>{html.escape(_fmt_ts(baseline.get("as_of_kst")))}</dd>'
        f"<dt>데이터 컷오프</dt><dd>{html.escape(_fmt_ts(baseline.get('available_data_cutoff')))}</dd></dl>"
        f'<ul class="cov-list">{coverage_rows}</ul>'
        f'<p><a href="{html.escape(_symbol_href(symbol))}">상세 보기</a></p>'
        f"</article>"
    )


def _detail_sections(card: dict) -> str:
    sections = []
    for group_key, group_label, items, signed in _GROUPS:
        values = card.get(group_key) or {}
        ready, total = _coverage(card, group_key, items)
        metrics = "".join(
            _metric_html(values, key, label, signed=signed) for key, label in items
        )
        sections.append(
            f"<section><h2>{group_label} "
            f'<span class="covnum">{ready}/{total} 준비</span></h2>'
            f'<ul class="grid">{metrics}</ul></section>'
        )
    return "".join(sections)


@app.get("/ksf", response_class=HTMLResponse)
def ksf_dashboard(_: None = Depends(_require_ksf_auth)) -> HTMLResponse:
    cards = get_cards()
    nav = "".join(
        _symbol_link(card, _symbol_href(card.get("symbol"), fragment=True)) for card in cards
    )
    body = (
        '<header class="top">'
        '<p class="brand">KronosStock · K-반도체 플로우 데스크</p>'
        "<h1>리서치 데스크</h1>"
        '<p class="badge">읽기 전용 리서치 · 투자 권유 아님</p>'
        f'<nav aria-label="종목 이동">{nav}</nav>'
        "</header>"
        f'<main class="cards">{"".join(_landing_card(card) for card in cards)}</main>'
    )
    return _document("KSF 리서치 데스크", body)


@app.get("/ksf/{symbol}", response_class=HTMLResponse)
def ksf_detail_page(symbol: str, _: None = Depends(_require_ksf_auth)) -> HTMLResponse:
    cards = get_cards()
    card = next((entry for entry in cards if entry.get("symbol") == symbol), None)
    if card is None:
        raise HTTPException(status_code=404, detail="card not found")
    quality = card.get("data_quality") or {}
    baseline = card.get("baseline") or {}
    nav_links = ['<a href="/ksf">데스크 홈</a>'] + [
        _symbol_link(entry, _symbol_href(entry.get("symbol")))
        for entry in cards
        if entry.get("symbol") != symbol
    ]
    body = (
        '<header class="top">'
        f'<nav aria-label="종목 이동">{"".join(nav_links)}</nav>'
        f'<p class="code">{html.escape(symbol)} · KRX</p>'
        f"<h1>{html.escape(str(card.get('name') or symbol))}</h1>"
        f"{_headline_html(quality)}"
        f'<p class="cutoff">판단 시각 {html.escape(_fmt_ts(baseline.get("as_of_kst")))} · '
        f"데이터 컷오프 {html.escape(_fmt_ts(baseline.get('available_data_cutoff')))} · "
        f"원천 기준 {html.escape(_fmt_ts(baseline.get('source_as_of')))}</p>"
        '<p class="badge">읽기 전용 리서치</p>'
        "</header><main>"
        f"{_detail_sections(card)}"
        '<section class="records"><h2>판단 기록</h2><div class="trio">'
        f'<div class="rec"><h3>근거</h3><ul>{_text_items(card.get("evidence") or [])}</ul></div>'
        f'<div class="rec"><h3>반론</h3><ul>{_text_items(card.get("counterarguments") or [])}</ul></div>'
        f'<div class="rec"><h3>이전 기록</h3><ul>{_prior_html(card.get("prior_performance") or [])}</ul></div>'
        "</div>"
        '<p class="note">수집 대기 항목은 다음 수집 주기에 채워지며 오류가 아닙니다. '
        "사용 제한 항목은 제공 조건 확인 후 자동 표시됩니다.</p>"
        '<p class="note">본 화면은 읽기 전용 리서치 기록이며 투자 판단이나 권유가 아닙니다.</p>'
        "</section></main>"
    )
    return _document(f"KSF {symbol} 리서치", body)
