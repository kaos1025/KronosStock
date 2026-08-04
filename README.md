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

## 예측 품질 평가 + 최적화 루프
예측이 "맞나"가 아니라 **"베이스라인(랜덤워크)보다 나은가"** 와 **"확률 밴드가 정직한가"** 를
walk-forward 로 측정한다. 예측 품질을 바꾸는 변경은 반드시 이 루프로만 진행한다("좋아 보인다"로 머지 금지).
프로토콜 전문은 [`CLAUDE.md`](./CLAUDE.md) 의 "🔁 최적화 루프 프로토콜" 참고.

북극성 지표(`strategy/evaluator.py`):
- **edge** = 모델 방향 적중률 − 베이스라인(추세지속) — ↑ 좋음, 음수면 동전던지기 이하
- **skill_score** = 1 − MAE_model / MAE_randomwalk — ↑ 좋음, ≤0 이면 랜덤워크 이하
- **coverage_gap** = |밴드 커버리지 − 목표(0.80)| — ↓ 좋음, 0 이면 밴드가 정직

```bash
make loop-baseline CODES="005930 000660" NE=40 NP=50   # 변경 전 baseline 고정 (eval_runs/baseline.json)
# ... 가설을 1개만 변경(하이퍼파라미터 1개 또는 코드 1곳) ...
make loop-try CONFIRM=20      # 재측정 + 홀드아웃 확인 + 자동 ACCEPT/REJECT 판정
make loop-promote             # ACCEPT 일 때만 baseline 갱신
make loop-show                # 현재 baseline/candidate 지표
make eval                     # 단발 측정(리포트만, 영속화 X)
```
- 루프 1회전 = 커밋 1개(메시지에 Δedge / Δskill / Δcoverage 기록).
- 금지: n_eval·horizon·codes 를 바꾸며 baseline 과 비교(잣대 불일치 → 무효) / 노이즈(±2%p) 채택 / 동시 다중 변경.
- `eval_runs/`(baseline·candidate JSON)는 `.gitignore` 처리(재생성 가능, 로컬 전용).
- ⚠️ CPU 비용: 1 origin = n_paths 회 autoregressive 추론. 2 vCPU 기준 종목당 수~수십 분 → **오프라인 1회성 잡**으로 돌릴 것(`make` 없으면 `.venv` python 으로 `python -m strategy.loop ...` 직접 호출).

### 현재 예측 품질 (정직한 상태)
일봉 대형주는 랜덤워크에 가깝다(martingale) — **방향 edge ≈ 0(코인플립), 점예측 skill ≤ 0**.
이건 모델 결함이 아니라 일봉 대형주의 본질이다. 따라서 이 도구의 가치는 *수익 예측*이 아니라
**거짓 확신 없는 정직한 불확실성 표현**에 둔다:
- **밴드 캘리브레이션(L1)**: `predict_probabilistic` 의 `band_scale`(밴드폭만 median 중심 ×1.7, median/up_prob 불변)로
  밴드 커버리지를 55.5%→~80% 로 보정(`coverage_gap` −24.5%p → −0.8%p). 점예측 정확도는 추적만 하고 목표로 삼지 않는다.
- 강한 추세 레짐에서는 단일 라이브 예측의 밴드가 넓게 나올 수 있다(레짐 대응은 후속 루프 항목).

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
             evaluator.py · loop.py (walk-forward 평가 + 최적화 루프, 구현됨)
bot/         alert_bot.py · scheduler.py (dry-run runner, 구현됨)
dashboard/   app.py  ← 헬스/상태 + forecast/signal/paper 조회 (구현됨)
common/      config.py, redis_client.py  (구현됨)
tests/       test_forecast_runner.py · test_toss_data_fetcher.py
             test_analyzer_paper_trader.py · test_backtester.py
             test_dashboard_and_alerts.py · test_scheduler_dry_run.py (구현됨)
notebooks/   backtest.ipynb (예정)
Makefile     loop-baseline / loop-try / loop-promote / loop-show / eval 타깃
eval_runs/   baseline·candidate 지표 JSON (gitignore, 로컬 전용)
```

## 의존성 메모
- `pykrx`가 `pandas<3.0`을 요구 → **pandas는 2.3.3에 고정**(3.0.x 불가). Kronos 의 `==2.2.2` 대신 호환 범위 내 통일.
- torch는 **CPU 휠**(`torch==2.5.1+cpu`, PyTorch CPU 인덱스). GPU 불필요.
- Kronos 는 `huggingface_hub.PyTorchModelHubMixin` 사용 → **`transformers` 불필요**(추가 금지).
- `exchange-calendars`(XKRX 휴장일)는 pandas/numpy 상한이 없어 현재 핀과 호환.
- 테스트 전용 의존성은 `requirements-dev.txt`(pytest, fakeredis) — 런타임 이미지 미포함.
- 로컬이 Python 3.13이면 핀이 어긋날 수 있다 — 권위 있는 검증은 `docker build`(3.11).

## KSF 대시보드 프라이빗 브라우저 미리보기 (SSH 터널)
대시보드 서비스(`deploy/systemd/kronostock-dashboard.service`)는 VPS 루프백
`127.0.0.1:8000` 에만 바인딩된다 — 공개 바인딩/`--reload` 없음. 클라이언트에서 SSH 터널로 접속:

```bash
ssh -N -L 8000:127.0.0.1:8000 deploy@<VPS_HOST>
```

대시보드 자격증명은 레포 전체 `.env` 가 아니라 **대시보드 전용 최소권한 env 파일**
`/srv/kronostock/config/ksf-dashboard.env`(모드 0600, 소유자 `deploy`)에서만 읽는다.
이 파일에는 읽기 자격증명 3개만 넣는다 — 다른 비밀(KIS/텔레그램)은 절대 넣지 않는다.
개념적 생성 예시(플레이스홀더만, 실제 값은 커밋/기록 금지):

```bash
# VPS 에서 1회 (root 또는 sudo):
install -d -m 0755 /srv/kronostock/config
umask 077
cat > /srv/kronostock/config/ksf-dashboard.env <<'ENV'
KSF_READ_USERNAME=<원하는_사용자명>
KSF_READ_PASSWORD=<강한_임의_비밀번호>
KSF_READ_TOKEN=<강한_임의_토큰>
ENV
chown deploy:deploy /srv/kronostock/config/ksf-dashboard.env
chmod 0600 /srv/kronostock/config/ksf-dashboard.env
```

브라우저에서 열면 HTTP Basic 로그인 창이 뜬다(`KSF_READ_USERNAME` / `KSF_READ_PASSWORD`,
둘 다 비어 있지 않게 설정돼야 하며 미설정/부분설정이면 Basic 은 전면 거부):
- http://127.0.0.1:8000/ksf — 카드 목록
- http://127.0.0.1:8000/ksf/005930 — 삼성전자 상세
- http://127.0.0.1:8000/ksf/000660 — SK하이닉스 상세

HTTP 트래픽은 VPS 루프백을 벗어나지 않고, 클라이언트↔VPS 구간은 SSH 로 암호화된다.
JSON/API 클라이언트는 기존 `Authorization: Bearer <token>` 헤더(`KSF_READ_TOKEN` 값)가 그대로 유효하다.
쿼리스트링 자격증명은 받지 않는다(항상 401). 유닛은 uvicorn 을 `--no-access-log` 로 실행하므로
요청 라인(URL 쿼리스트링 포함)이 액세스 로그로 journald 에 기록되지 않는다 — 테스트로 검증된
범위는 여기까지이며, 애플리케이션 에러 로그 전반의 자격증명 마스킹을 보장하는 것은 아니다.
DB 경로(`KSF_LEDGER_DB_PATH`)는 비밀이 아니므로 유닛의 `Environment=` 지시어로 주입한다.

### 원장 스냅샷 (WAL + 완전 읽기 전용 서비스의 공존)
프로덕션 원장 `/srv/kronostock/data/ksf_ledger.sqlite3` 은 **의도적으로 WAL 저널
모드**다(수집기 동시성). WAL DB 자체는 읽기 전용으로 읽을 수 있지만 — 읽을 수 있는
`-wal`/`-shm` 사이드카가 있을 때 한정이다. 마지막 writer 가 최종 체크포인트 후
사이드카를 지운 상태에서는, 완전 읽기 전용 트리에서 `mode=ro` 열기가
`attempt to write a readonly database` 로 실패한다(SQLite 가 사이드카를 재생성하려
하기 때문). 그래서 대시보드 유닛은 시작 시점에 스냅샷을 뜬다:

1. `ExecStartPre` 가 `scripts/deploy/prepare_ksf_dashboard_snapshot.py` 를 실행해
   원장을 SQLite backup API 로 `/run/kronostock-dashboard/ksf-ledger.sqlite3`
   (RuntimeDirectory, 0700)에 원자적으로 복사한다 — 임시 파일에 백업 →
   `integrity_check` → DELETE 저널로 변환 → `os.replace`.
2. 원본은 `mode=ro` + `query_only` 로만 연다. 사이드카가 **둘 다 없을 때만**
   `immutable=1` 로 폴백한다. **주의(과거 문서의 정정)**: 사이드카 부재 관찰만으로는
   안전이 보장되지 않는다 — 관찰 직후 writer 가 시작해 커밋하면 immutable 백업이
   그 커밋을 조용히 무시할 수 있다(TOCTOU). 폴백이 안전한 것은 아래의 **writer-배제
   잠금을 관찰~백업~발행 전 구간 동안 쥐고 있기 때문**이다. 사이드카가 하나만
   있거나 열기 실패 시에는 스냅샷을 거부하고 서비스 시작을 막는다
   (fail-closed — 커밋된 WAL 프레임 무시 금지).
3. 대시보드(`KSF_LEDGER_DB_PATH`)는 런타임 스냅샷만 읽는다. 프로덕션 데이터
   디렉터리는 서비스에 읽기 전용(`ReadOnlyPaths`)이고, 쓰기 가능한 경로는
   `/run/kronostock-dashboard` 하나뿐이다.

즉 대시보드가 보여주는 데이터는 **서비스 시작 시점의 point-in-time 스냅샷**이다.
수집기가 원장을 갱신한 뒤 대시보드에 반영하려면 `systemctl restart
kronostock-dashboard` 로 재시작해야 한다.

### 원장 사이드카 잠금 (writer-배제, `ksf/ledger_lock.py`)
원장의 모든 접근은 DB 경로에서 유도되는 단일 잠금 파일
`/srv/kronostock/data/ksf_ledger.sqlite3.lock` (= `Path(f"{db}.lock")`) 위의
배타 `flock` 으로 직렬화된다. 잠금 경로는 호출자가 지정할 수 없어, runner 를
직접 실행해도 다른 잠금을 몰래 쓸 수 없다.

- **writer (`ksf.production_runner.run_once`)**: DB 를 열거나 만들기 **이전에**
  잠금을 배타 획득하고, 마이그레이션·수집·커밋·연결 close 까지 run 전체 동안
  유지한다. 잠금 파일이 없으면 안전하게 생성한다(0600, O_NOFOLLOW, 정규 파일·
  소유자·권한 검증). timer wrapper 의 외부 `flock` 은 제거되었다 — 모든
  프로덕션 writer 는 내부에서 잠금을 잡으므로 외부 잠금을 다시 씌우면 이중
  잠금/자기교착 위험만 생긴다.
- **스냅샷 헬퍼**: 이미 존재하는 잠금만 O_CREAT 없이 읽기 전용으로 열어
  (읽기 전용 fd 위의 배타 flock — Linux 에서 유효) 사이드카 관찰 → mode=ro
  백업 → immutable 폴백 → 무결성/저널 변환 → 발행까지 전 구간 동안 쥔다.
- **대기/fail-closed**: 두 모드 모두 monotonic 논블로킹 flock 루프로 최대
  60초 대기한다. 잠금 파일이 없거나(스냅샷 모드), 심볼릭 링크/비정규 파일,
  다른 소유자, group/world 권한, 대기 초과 — 전부 즉시 실패한다. runner CLI 는
  `{"status":"failed","failed_stage":"lock"}` 만 출력한다(경로/비밀 비노출).
- **배포 선행조건**: 대시보드 서비스 시작 전에 잠금 파일이 존재해야 한다
  (`install -o deploy -g deploy -m 0600 /dev/null /srv/kronostock/data/ksf_ledger.sqlite3.lock`,
  또는 첫 writer 실행이 생성한 것을 사용). 잠금 파일이 없거나 unsafe
  (심볼릭 링크·비정규 파일·타 소유자·group/world 권한)면 `ExecStartPre` 가
  실패해 서비스가 시작되지 않는다.

## 보안
- `.env`는 절대 커밋 금지(`.gitignore`로 차단). 비밀값은 환경변수로만 주입.
- **로그에 비밀값 금지**: `httpx` 가 요청 URL 을 INFO 로 찍으면 Telegram `sendMessage` URL(봇 토큰 포함)이 평문으로 로그(journald)에 남는다. `bot/alert_bot.py` 가 `httpx` 로거를 WARNING 으로 낮춰 차단한다 — **텔레그램/httpx 경로에 INFO 로깅 재도입 금지.**
- 자동매매는 충분한 **모의투자 검증 후** 실전 전환(Phase 2+).
