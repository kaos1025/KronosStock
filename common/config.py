"""중앙 환경설정 (pydantic-settings v2 기반).

.env 파일에서 값을 로드한다. 민감정보(API 키 등)는 코드/깃에 절대 커밋하지 않으며
.gitignore 로 .env 를 차단한다.

한국투자증권(python-kis v2.x) 인증 설계 메모:
  - python-kis v2.x 는 '모의투자 단독' 모드를 지원하지 않는다.
  - PyKis 생성자는 반드시 실전(real) 도메인 자격증명을 1차 인증으로 요구한다.
  - 모의투자(virtual)는 virtual_* 자격증명을 '추가로' 넘겨야 활성화된다.
  - 시세/차트(OHLCV) 조회는 모의투자 모드에서도 실전 도메인을 사용한다.
    → Phase 1(데이터 수집)에는 실전 자격증명만 있으면 된다.
  - 모의투자 자격증명은 주문(Phase 2+)을 모의로 라우팅할 때만 필요(선택).
"""
from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── 한국투자증권 실전(real) 자격증명 — KIS 사용 시 필수 ──
    kis_hts_id: str = ""        # HTS 로그인 ID (PyKis id=)
    kis_appkey: str = ""        # 실전 appkey (~36자)
    kis_appsecret: str = ""     # 실전 appsecret / secretkey (~180자)
    kis_account: str = ""       # 실전 계좌 '00000000-01' (CANO 8자리-상품코드 2자리)

    # ── 한국투자증권 모의투자(virtual) 자격증명 — 모의 주문(Phase 2+)에만 필요(선택) ──
    kis_virtual_appkey: str = ""
    kis_virtual_appsecret: str = ""
    kis_virtual_account: str = ""   # 예: '50000000-01'
    kis_virtual_hts_id: str = ""    # 보통 실전과 동일

    # ── KIS 동작 옵션 ──
    kis_use_virtual: bool = True    # 주문을 모의투자로 라우팅(개인 도구 안전 기본값)
    kis_keep_token: bool = True     # OAuth2 토큰 디스크 캐시(24h, 과다발급 방지)
    kis_token_dir: str = ".cache"   # 토큰 캐시 디렉터리(compose 의 kis-cache 볼륨에 매핑)
    kis_use_websocket: bool = False  # 실시간 웹소켓(불필요하면 끈다 → REST 폴링만)

    # ── 토스증권 Open API read-only market data ──
    market_data_provider: str = "kis"  # kis | toss | fdr | auto
    tossinvest_client_id: str = ""
    tossinvest_client_secret: str = ""
    tossinvest_base_url: str = "https://openapi.tossinvest.com"
    tossinvest_timeout: float = 10.0

    # ── Redis (KronosKit 과 인스턴스 공유 가능 → 키 prefix 로 네임스페이스 분리) ──
    redis_url: str = "redis://localhost:6379/0"
    redis_password: str = ""
    redis_key_prefix: str = "kronos:stock:"

    # ── Telegram 개인 알림 ──
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # ── Kronos-mini 모델 ──
    kronos_model_repo: str = "NeoQuasar/Kronos-mini"
    kronos_tokenizer_repo: str = "NeoQuasar/Kronos-Tokenizer-2k"  # mini 전용 토크나이저
    kronos_device: str = "cpu"      # Hetzner CX22 = GPU 없음
    kronos_max_context: int = 2048  # mini=2048 (small/base=512)
    kronos_lookback: int = 400      # 예측에 사용할 과거 봉 개수 (<= max_context)
    kronos_pred_len: int = 5        # 예측할 미래 일봉 개수(기본 horizon)

    # ── 예측(Forecast) 파이프라인 ──
    forecast_lookback_days: int = 450  # OHLCV 수집 거래일. kronos_lookback(400) 이상이어야 컨텍스트 충분
    forecast_n_paths: int = 20         # Monte Carlo 경로 수(분위수 밴드·상승확률 계산용)

    # ── 워치리스트 (config/.env 수정으로 즉시 반영) ──
    watchlist: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["005930", "000660", "035420", "051910", "006400"]
    )

    # ── 스케줄 (KST) ──
    timezone: str = "Asia/Seoul"
    schedule_premarket: str = "08:50"  # 장 시작 전 예측
    schedule_open: str = "09:30"       # 시초가 반영
    schedule_midday: str = "12:00"     # 장중 체크
    schedule_close: str = "15:20"      # 마감 후 기록 + 익일 예측

    log_level: str = "INFO"

    @field_validator("watchlist", mode="before")
    @classmethod
    def _split_watchlist(cls, v):
        # .env 에서 콤마 구분 문자열 허용: WATCHLIST=005930,000660,035420
        # (NoDecode 로 JSON 파싱을 끄고 직접 분해한다)
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v

    # ── 헬퍼 ──
    @property
    def kis_configured(self) -> bool:
        """실전 자격증명이 모두 채워졌는지(데이터 수집 최소 요건)."""
        return all((self.kis_hts_id, self.kis_appkey, self.kis_appsecret, self.kis_account))

    @property
    def kis_virtual_configured(self) -> bool:
        """모의투자 자격증명이 모두 채워졌는지(모의 주문 가능 여부)."""
        return all((self.kis_virtual_appkey, self.kis_virtual_appsecret, self.kis_virtual_account))

    @property
    def tossinvest_configured(self) -> bool:
        """토스증권 Open API client credentials 설정 여부."""
        return bool(self.tossinvest_client_id and self.tossinvest_client_secret)

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """프로세스 전역 싱글턴. 환경 변경 후에는 get_settings.cache_clear()."""
    return Settings()


# 편의용 모듈 레벨 싱글턴
settings = get_settings()
