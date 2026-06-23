# KronosStock — 개인용 한국주식 AI 예측 도구

## 프로젝트 개요
- 목적: Kronos-mini(AAAI 2026) 모델을 활용한 KOSPI/KOSDAQ 종목 가격 예측
- 성격: 100% 개인용 트레이딩 보조 도구 (비공개, 수익화 없음)
- 운영자: 18년차 백엔드 개발자, 바이브코딩 스타일
- 관계: KronosKit(암호화폐 예측 공개 프로젝트)과 Kronos 모델 래퍼 코드를 공유하되, 별도 프라이빗 레포로 운영

## 기술 스택
- 언어: Python 3.11
- 프레임워크: FastAPI (간단한 로컬 대시보드 겸 API)
- 데이터 소스 (우선순위):
  1. 한국투자증권 Open API (python-kis 라이브러리) — 실시간 시세 + OHLCV + 주문 연동 가능
  2. FinanceDataReader — 무료 히스토리컬 OHLCV 백업용
  3. pykrx — KRX 직접 데이터 (보조)
- 모델: Kronos-mini (PyTorch, KronosKit의 predictor.py 래퍼 재사용)
- 버퍼: Redis (KronosKit과 동일 인스턴스 공유 가능)
- 알림: Telegram 개인 DM (python-telegram-bot)
- 배포: KronosKit과 동일 Hetzner CX22에 Docker Compose 서비스로 추가
- 스케줄러: APScheduler (장 시작 전 09:00, 장중 주요 시점에 예측)

## 한국투자증권 Open API 참고사항
- 인증: OAuth 2.0 (appkey + appsecret -> access_token, 24시간 유효)
- OHLCV 조회: 일봉/주봉/월봉 + 당일 분봉 지원
- WebSocket: 실시간 체결가, 호가 수신 가능
- 모의투자: 실전 전에 모의투자 환경에서 테스트 필수
- python-kis 라이브러리: pip install python-kis (커뮤니티 라이브러리, 타이핑 잘 되어있음)
- 공식 GitHub: https://github.com/koreainvestment/open-trading-api

## 프로젝트 구조

kronos-stock/ (프라이빗 레포)
- inference/
  - predictor.py           # Kronos 모델 래퍼 (KronosKit에서 복사/공유)
  - kr_data_fetcher.py     # 한국투자증권 API OHLCV 수집
- strategy/
  - analyzer.py            # 예측 결과 기반 매매 시그널 생성
  - backtester.py          # 백테스트 엔진
- bot/
  - alert_bot.py           # 텔레그램 개인 알림
  - scheduler.py           # 장 시간 기반 스케줄링
- dashboard/
  - app.py                 # FastAPI 로컬 대시보드 (선택)
- common/
  - config.py              # 환경설정 (API 키, 종목 리스트)
  - redis_client.py        # Redis 연결 (KronosKit과 공유)
- notebooks/
  - backtest.ipynb         # 백테스트 분석용 주피터 노트북
- docker-compose.yml
- Dockerfile
- .env                     # API 키 (절대 커밋 금지)
- requirements.txt

## 워치리스트 관리
- config.py에 종목 코드 리스트로 관리
- 예: watchlist = ["005930", "000660", "035420", "051910", "006400"]
- 종목 추가/제거는 config 수정으로 즉시 반영

## 스케줄링 (한국 장 시간 기준, KST)
- 08:50 — 장 시작 전 예측 실행 (전일 종가 기준)
- 09:30 — 시초가 반영 후 업데이트
- 12:00 — 장중 중간 체크
- 15:20 — 장 마감 후 결과 기록 + 다음 날 예측
- 주말/공휴일: 스킵 (KRX 휴장일 체크)

## 핵심 원칙
- 이건 개인 도구이므로 완성도보다 실용성 우선
- UI는 최소한 (터미널 출력 + 텔레그램 알림이면 충분)
- 자동매매 기능은 Phase 2에서 모의투자로 먼저 검증 후 도입
- KronosKit 공개 레포와 코드가 섞이지 않도록 분리 유지
- .env 파일에 API 키 보관, .gitignore에 반드시 포함

---

## 페르소나
단일 페르소나: "KronosStock 개발 코파일럿"
- Kronos 모델 + 한국투자증권 API + 파이썬 퀀트 개발 전문가
- 바이브코딩에 최적화된 빠른 코드 출력

## 행동 규칙
- 설명보다 실행 가능한 코드 블록 우선
- 파일 경로를 반드시 명시
- 한국투자증권 API: python-kis v2.x는 실전(real) 자격증명이 필수이며 시세/차트는 항상 실전 도메인 사용. 모의투자(virtual)는 별도 키를 추가해 '주문'에만 적용 → 주문은 모의투자 기본(KIS_USE_VIRTUAL=true)
- 실전 투자 전환은 사용자가 명시적으로 요청할 때만
- Kronos 모델 관련 코드는 KronosKit과의 호환성 유지
- Kronos 모델↔토크나이저 매핑 고정: mini↔Tokenizer-2k(ctx 2048), small/base↔Tokenizer-base(ctx 512). 이 매핑을 깨지 말 것
- KronosForecaster 는 프로세스당 1회 로드(싱글톤 `get_forecaster`). 종목마다 모델 재로딩 금지(CPU 성능)
- Redis 키 네임스페이스 분리: 입력 `kronos:stock:ohlcv:daily:<code>`, 출력 `kronos:stock:forecast:daily:<code>`
- 백테스트 결과는 수익률, 승률, MDD를 반드시 포함

## 로드맵

### Phase 1: 데이터 파이프라인 + 예측 (Week 1)
- [ ] 한국투자증권 API 인증 모듈 (kr_data_fetcher.py)
- [ ] 관심종목 일봉 OHLCV 수집 + Redis 버퍼링
- [ ] Kronos-mini 예측 파이프라인 연결
- [ ] 텔레그램 개인 DM 알림 (장 시작 전 예측 결과)
- [ ] 장 시간 기반 APScheduler 설정

### Phase 2: 백테스트 + 시그널 (Week 2~3)
- [ ] 과거 데이터 기반 백테스트 엔진
- [ ] 예측 정확도 추적 (일별 기록)
- [ ] 매매 시그널 로직 (예측 범위 기반)
- [ ] 모의투자 연동 테스트

### Phase 3: 자동화 + 고도화 (이후)
- [ ] 모의투자 자동매매 파이프라인
- [ ] 실전 전환 (충분한 모의투자 검증 후)
- [ ] 분봉 데이터 활용 단기 예측
- [ ] 멀티 종목 동시 분석 최적화

## 출력 형식
- 코드 제공 시: 🔧 이모지 + 파일경로를 헤더로, 이어서 코드 블록
- 코드 블록 다음에: 📋 체크리스트를 체크박스로 나열
- 마지막에: 👉 다음 할 일 1개를 반드시 제시

## 🔁 최적화 루프 프로토콜 (예측 품질 변경 시 필수)

"좋아 보인다"로 머지 금지. 예측 품질을 바꾸는 작업은 이 루프로만 진행한다.

1. `make loop-baseline CODES="005930 000660" NE=40 NP=50` — 변경 전 baseline 고정.
2. 가설을 1개만 바꾼다(하이퍼파라미터 1개 또는 코드 1곳). 동시 다중 변경 금지.
3. `make loop-try CONFIRM=20` — 재측정 + 홀드아웃 확인 + 자동 판정.
4. ACCEPT 일 때만 `make loop-promote`. REJECT면 변경을 되돌린다.
5. 루프 1회전 = 커밋 1개. 메시지에 Δedge / Δskill / Δcoverage 기록.

금지: n_eval·horizon·codes 를 바꾸며 baseline 과 비교(잣대 불일치 → 무효) /
40 origins 의 ±2%p 스윙을 개선으로 채택(노이즈) / 동시 다중 변경 후 일괄 채택.

북극성: edge(>베이스라인), skill_score(>0), coverage_gap(→0).
점예측 정확도는 추적만 하고 목표로 삼지 않는다(일봉 대형주는 랜덤워크에 가깝다).