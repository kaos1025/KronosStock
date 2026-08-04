"""최소 FastAPI 대시보드 — 스캐폴드 단계의 헬스/상태 확인용.

비밀값은 절대 노출하지 않는다(설정 여부 불리언만 표시).
로컬 실행:  uvicorn dashboard.app:app --reload
"""
from __future__ import annotations

import json
import html
import os
import secrets

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


def _require_ksf_auth(authorization: str | None = Header(default=None)) -> None:
    expected = os.environ.get("KSF_READ_TOKEN", "")
    prefix = "Bearer "
    supplied = authorization[len(prefix):] if authorization and authorization.startswith(prefix) else ""
    try:
        valid = bool(expected and supplied) and secrets.compare_digest(
            supplied.encode("utf-8"), expected.encode("utf-8")
        )
    except UnicodeError:
        valid = False
    if not valid:
        raise HTTPException(
            status_code=401,
            detail="authentication required",
            headers={"WWW-Authenticate": "Bearer"},
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


def _items(values: dict) -> str:
    return "".join(
        f"<li><span>{html.escape(str(key))}</span><strong>{html.escape(str(value.get('value')))}</strong>"
        f"<small>{html.escape(str(value.get('status')))}</small></li>"
        for key, value in values.items() if isinstance(value, dict)
    ) or "<li>확인 가능한 항목이 없습니다.</li>"


def _card_html(card: dict, *, detail: bool) -> str:
    quality = card["data_quality"]
    baseline = card["baseline"]
    sections = ""
    if detail:
        evidence = "".join(f"<li>{html.escape(str(item))}</li>" for item in card["evidence"])
        counters = "".join(f"<li>{html.escape(str(item))}</li>" for item in card["counterarguments"])
        performance = "".join(
            f"<li>T+{html.escape(str(item['horizon_days']))} · {html.escape(str(item['status']))} · "
            f"hit {html.escape(str(item['direction_hit']))} · {html.escape(str(item['return_bps']))} bps</li>"
            for item in card["prior_performance"]
        ) or "<li>기록 없음</li>"
        sections = f"""
        <section><h2>국내 수급</h2><ul>{_items(card['domestic_flow'])}</ul></section>
        <section><h2>글로벌 환경</h2><ul>{_items(card['global_environment'])}</ul></section>
        <section><h2>메모리 지표</h2><ul>{_items(card['memory_indicators'])}</ul></section>
        <section><h2>근거</h2><ul>{evidence or '<li>기록 없음</li>'}</ul></section>
        <section><h2>반론과 위험</h2><ul>{counters}</ul></section>
        <section><h2>이전 판단 성과</h2><ul>{performance}</ul></section>"""
    href = f"/ksf/{card['symbol']}" if not detail else "/ksf"
    label = "상세 보기" if not detail else "목록으로"
    return f"""<article>
      <p class="eyebrow">{html.escape(card['symbol'])}</p>
      <h1>{html.escape(card['name'])}</h1>
      <p class="state">데이터 상태: {html.escape(str(quality['state']))}</p>
      <dl><dt>판단 시각</dt><dd>{html.escape(str(baseline['as_of_kst']))}</dd>
      <dt>사용 가능 기준</dt><dd>{html.escape(str(baseline['available_data_cutoff']))}</dd>
      <dt>출처 기준</dt><dd>{html.escape(str(baseline['source_as_of']))}</dd></dl>
      {sections}<a href="{href}">{label}</a>
    </article>"""


def _page(content: str, title: str) -> HTMLResponse:
    document = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{html.escape(title)}</title><style>
    *{{box-sizing:content-box}} body{{margin:0;background:#f4f6f8;color:#17202a;font:16px/1.55 system-ui,sans-serif}}
    main{{max-width:780px;margin:auto;padding:16px;display:grid;gap:16px}}
    article,section{{background:white;padding:18px;box-shadow:0 1px 8px #0002}}
    section{{margin:16px 0;box-shadow:none;background:#f7f8fa}} h1,h2,p{{margin-top:0}}
    h2{{font-size:1rem}} .eyebrow,small{{color:#52606d}} .state{{font-weight:700}}
    dl{{display:grid;grid-template-columns:max-content 1fr;gap:6px 12px}} dd{{margin:0;overflow-wrap:anywhere}}
    ul{{padding-left:20px}} li{{margin:6px 0}} li span,li strong,li small{{display:block}}
    a{{color:#075cab;font-weight:700}} @media (min-width:680px){{main.cards{{grid-template-columns:1fr 1fr}}}}
    </style></head><body><main class="{('cards' if title.endswith('카드') else 'detail')}">{content}</main></body></html>"""
    return HTMLResponse(document)


@app.get("/ksf", response_class=HTMLResponse)
def ksf_dashboard(_: None = Depends(_require_ksf_auth)) -> HTMLResponse:
    return _page("".join(_card_html(card, detail=False) for card in get_cards()), "KSF 독립 카드")


@app.get("/ksf/{symbol}", response_class=HTMLResponse)
def ksf_detail_page(symbol: str, _: None = Depends(_require_ksf_auth)) -> HTMLResponse:
    detail = get_detail(symbol)
    if detail is None:
        raise HTTPException(status_code=404, detail="card not found")
    return _page(_card_html(detail, detail=True), f"KSF {symbol}")
