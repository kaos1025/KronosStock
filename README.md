# KronosStock

Kronos-mini(AAAI 2026) 기반 **개인용** KOSPI/KOSDAQ 가격 예측 도구. 비공개·비수익.
한국투자증권 Open API로 데이터를 모으고, Kronos-mini로 예측해, 텔레그램으로 알림을 보낸다.

> 상세 로드맵·원칙은 [`CLAUDE.md`](./CLAUDE.md) 참고.

## 스택
- Python **3.11**, FastAPI, Redis, APScheduler
- 데이터: `python-kis`(한국투자증권) → `FinanceDataReader`/`pykrx`(백업)
- 모델: Kronos-mini (PyTorch CPU)
- 알림: python-telegram-bot

## 빠른 시작 (로컬)
```bash
# 1) Python 3.11 가상환경 (로컬 기본 파이썬이 3.13이면 3.11을 따로 설치)
py -3.11 -m venv .venv && .venv\Scripts\activate    # Windows
# python3.11 -m venv .venv && source .venv/bin/activate   # macOS/Linux

# 2) 의존성
pip install -r requirements.txt

# 3) 환경설정
copy .env.example .env        # Windows  (cp .env.example .env)
#   → .env 에 한국투자증권 실전 키를 채운다 (아래 'KIS 인증' 참고)

# 4) 대시보드 헬스체크
uvicorn dashboard.app:app --reload
#   http://localhost:8000/health  ,  http://localhost:8000/status
```

## 빠른 시작 (Docker)
```bash
copy .env.example .env   # 키 입력 후
docker compose up --build
#   app → http://localhost:8000  /  redis 동봉 (공유 시 redis 서비스 제거)
```

## KIS 인증 (중요)
`python-kis` v2.x는 **모의투자 단독 모드를 지원하지 않는다.**
- `PyKis(...)`는 **실전(real) 자격증명을 1차 인증으로 반드시 요구**한다.
- 모의투자(virtual)는 `KIS_VIRTUAL_*` 키를 **추가로** 채워야 활성화된다.
- **시세/차트(OHLCV) 조회는 모의 모드에서도 실전 도메인을 사용** → Phase 1(데이터 수집)에는 실전 키만 있으면 된다. 모의 키는 모의 **주문**(Phase 2+)에만 필요.
- 토큰은 24h 유효. `KIS_KEEP_TOKEN=true`로 디스크 캐시(과다발급 시 KIS 사용제한).
- 레이트리밋: 실전 20 req/s, 모의 5 req/s.

## Toss Open API 시세 provider
토스증권 Open API를 read-only market data provider로 사용할 수 있다. 주문 API는 연결하지 않는다.
```env
MARKET_DATA_PROVIDER=toss
TOSSINVEST_CLIENT_ID=...
TOSSINVEST_CLIENT_SECRET=...
```
현재 사용 endpoint:
- `POST /oauth2/token` — OAuth2 client credentials access token 발급/캐시
- `GET /api/v1/candles?symbol=005930&interval=1d` — 일봉 OHLCV 조회
- `GET /api/v1/prices?symbols=005930,000660` — 현재가 조회(optional)

토스 캔들 응답에는 거래대금(`amount`) 필드가 없어 Redis OHLCV 표준 스키마에는 `amount=0.0`으로 저장한다.

## 예측 파이프라인 (Kronos)

### 1) 모델 vendoring (추론 전 1회)
Kronos는 pip 패키지가 아니다. `model/` 패키지를 repo 루트에 vendor 한다(Docker 빌드에서 자동 수행):
```bash
bash inference/vendor_kronos.sh          # shiyu-coder/Kronos 의 model/ 를 sparse-checkout
python -c "from model import Kronos, KronosTokenizer, KronosPredictor"   # import 확인
```
> `model/` 은 `.gitignore` 처리(커밋 금지, 빌드 시 재생성). 가중치는 최초 추론 시 HuggingFace
> Hub 에서 자동 다운로드된다(`HF_HOME=/app/.cache/huggingface`, 캐시 볼륨에 영속).

### 2) 모델 스모크 (합성 데이터, 실제 CPU 추론)
```bash
python -m inference.predictor            # 가중치 다운로드 + CPU 추론. Docker(3.11) 권장.
```

### 3) 예측 실행 (Redis OHLCV → 예측 → Redis)
`kr_data_fetcher` 로 수집·버퍼링된 OHLCV 를 읽어 확률적 예측을 수행한다.
KronosForecaster 는 **프로세스당 1회만 로드(싱글톤)** 되어 모든 종목에 재사용된다(CPU 성능).
```bash
python -m inference.kr_data_fetcher 005930     # (선택) 먼저 OHLCV 수집·버퍼링
python -m inference.forecast_runner 005930     # 예측 → kronos:stock:forecast:daily:005930 저장
```
```python
from inference.forecast_runner import run_watchlist_forecast
results = run_watchlist_forecast(horizon=5, n_paths=20)   # 워치리스트 일괄(부분 실패 내성)
```

### 4) 단위 테스트 (네트워크 없이)
모델은 stub, Redis 는 fakeredis 로 대체 — 가중치 다운로드 없이 글루 로직만 검증한다.
```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests/ -q
```
> **uv 사용 시:** torch CPU 휠 등 서로 다른 인덱스가 섞여 설치가 실패할 수 있다.
> 이 경우 `uv pip install --index-strategy unsafe-best-match -r requirements.txt -r requirements-dev.txt` 로 설치한다.

**모델 ↔ 토크나이저 매핑(깨지 말 것):** Kronos-mini ↔ Tokenizer-2k(ctx 2048), small/base ↔ Tokenizer-base(ctx 512).
predictor 가 모델명으로 자동 매핑한다(`config.py` 의 `kronos_*` 값은 이 표와 일관 유지).

**한국 장 휴장일:** `exchange_calendars` 의 `XKRX` 캘린더로 미래 타임스탬프에서 휴장일을 제거한다
(음력·대체공휴일·임시휴장 자동 반영). 미설치/실패 시 평일(월~금) 기준으로 폴백(경고 로깅).

## 대시보드 엔드포인트
`uvicorn dashboard.app:app --reload` 로 띄운다. 모든 응답은 **비밀값/토큰 비노출**.
| Method · Path | 설명 |
|---|---|
| `GET /health` | 헬스체크 |
| `GET /status` | 구성 상태(설정 여부 불리언) + watchlist/model/device |
| `GET /forecast/{code}` | Redis `kronos:stock:forecast:daily:<code>` payload (없으면 404) |
| `GET /signal/{code}` | 저장된 forecast → BUY/HOLD/SELL 시그널 변환 |
| `GET /paper/portfolio` | scheduler dry-run 이 저장한 paper portfolio snapshot (없으면 404) |

`/paper/portfolio` 는 scheduler 와 동일 키(`kronos:stock:paper:portfolio`)의 현금·보유수량·체결기록만 반환한다.

## 자동화 dry-run 러너 (scheduler)
`bot/scheduler.py` 는 **forecast → signal → paper order → digest** 를 1회 수행하는 안전한 자동화다.
**실제 KIS/broker 주문 API 는 호출하지 않으며**, CLI 기본 실행은 Telegram 전송을 `--send-alert` opt-in 으로 둔다.
VPS 정기 timer wrapper 는 사용자가 시그널을 받을 수 있도록 Telegram digest 전송을 켜지만, 여전히 paper portfolio 만 갱신한다.
```bash
python -m bot.scheduler              # 4개 cron job(08:50/09:30/12:00/15:20 KST) dry-run 스케줄러 기동
python -m bot.scheduler --once       # dry-run 사이클 1회 실행 후 종료(터미널 digest 출력)
python -m bot.scheduler --send-alert # Telegram 전송 opt-in
```
paper portfolio snapshot 은 Redis `kronos:stock:paper:portfolio` 에 저장되어 `/paper/portfolio` 로 조회한다.

## VPS 배포 자동화
`main` 브랜치 push 시 GitHub Actions `.github/workflows/deploy-vps.yml` 이 Hostinger VPS의
`/srv/agent-workspaces/KronosStock` 를 해당 커밋으로 `reset --hard` 하고 테스트/서비스 갱신을 수행한다.
서버 로컬 파일 `.env`, `.venv/`, `model/` 은 보존한다.

필요한 GitHub repository secrets:
- `VPS_HOST`: VPS 주소
- `VPS_USER`: SSH 사용자(현재 deploy)
- `VPS_PORT`: SSH 포트(기본 22)
- `VPS_SSH_KEY`: deploy 사용자로 접속 가능한 private key

배포 중 수행:
1. `uv pip install --index-strategy unsafe-best-match -r requirements.txt -r requirements-dev.txt`
2. Kronos vendoring/import smoke
3. `python -m pytest tests/ -q`
4. `.env` mode 600, Redis `PONG` 확인
5. `/srv/kronostock/dry-run-once.sh` 및 `kronostock-dry-run.timer` 재설치/활성화

정기 timer wrapper 는 `send_alert=True` 로 Telegram digest 를 발송하지만, scheduler 경로는 계속 paper order/Redis snapshot 만 갱신하며 실제 KIS/broker 주문 API 를 호출하지 않는다.

## 구조
```
inference/   predictor.py · kr_data_fetcher.py · forecast_runner.py (구현됨)
             toss_data_fetcher.py (Toss read-only 시세 provider, 구현됨)
             vendor_kronos.sh (Kronos model/ vendoring 스크립트)
strategy/    analyzer.py · backtester.py · paper_trader.py (구현됨)
bot/         alert_bot.py · scheduler.py (dry-run runner, 구현됨)
dashboard/   app.py  ← 헬스/상태 + forecast/signal/paper 조회 (구현됨)
common/      config.py, redis_client.py  (구현됨)
tests/       test_forecast_runner.py · test_toss_data_fetcher.py
             test_analyzer_paper_trader.py · test_backtester.py
             test_dashboard_and_alerts.py · test_scheduler_dry_run.py (구현됨)
notebooks/   backtest.ipynb (예정)
```

## 의존성 메모
- `pykrx`가 `pandas<3.0`을 요구 → **pandas는 2.3.3에 고정**(3.0.x 불가). Kronos 의 `==2.2.2` 대신 호환 범위 내 통일.
- torch는 **CPU 휠**(`torch==2.5.1+cpu`, PyTorch CPU 인덱스). GPU 불필요.
- Kronos 는 `huggingface_hub.PyTorchModelHubMixin` 사용 → **`transformers` 불필요**(추가 금지).
- `exchange-calendars`(XKRX 휴장일)는 pandas/numpy 상한이 없어 현재 핀과 호환.
- 테스트 전용 의존성은 `requirements-dev.txt`(pytest, fakeredis) — 런타임 이미지 미포함.
- 로컬이 Python 3.13이면 핀이 어긋날 수 있다 — 권위 있는 검증은 `docker build`(3.11).

## 보안
- `.env`는 절대 커밋 금지(`.gitignore`로 차단). 비밀값은 환경변수로만 주입.
- 자동매매는 충분한 **모의투자 검증 후** 실전 전환(Phase 2+).
