"""최소 FastAPI 대시보드 — 스캐폴드 단계의 헬스/상태 확인용.

비밀값은 절대 노출하지 않는다(설정 여부 불리언만 표시).
로컬 실행:  uvicorn dashboard.app:app --reload
"""
from __future__ import annotations

import json

from fastapi import FastAPI, HTTPException

from common.config import settings
from common.redis_client import get_redis, key, ping as redis_ping
from strategy.analyzer import analyze_forecast

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
    return analyze_forecast(payload).as_dict()
