# KronosStock Phase 1 → Phase 3 실행 계획

## 안전 경계
- 기본 경로는 네트워크 없는 단위 테스트, mock/historical data, Redis/fakeredis, 모의투자 객체까지만 사용한다.
- 한국투자증권 주문 endpoint 호출은 사용자가 별도로 명시 승인하기 전까지 구현/테스트 경로에서 금지한다.
- `KIS_USE_VIRTUAL=true`가 기본이며, 실전 전환 코드는 명시적 opt-in guard 없이는 동작하지 않게 한다.

## Phase 1 — 데이터 파이프라인 + 예측 안정화
1. 이미 완료: KIS/FDR OHLCV 수집, Redis 버퍼링, Kronos forecaster 싱글톤, watchlist forecast, dashboard health/status.
2. 남은 보강:
   - Forecast payload 읽기 유틸과 dashboard forecast endpoint.
   - Telegram message formatter와 dry-run sender.
   - scheduler job 함수는 실제 네트워크 호출을 주입 가능하게 분리.

## Phase 2 — 백테스트 + 시그널
1. 이미 완료: threshold backtester + edge-case tests.
2. 다음 구현:
   - `strategy/analyzer.py`: ForecastResult/Redis payload → BUY/HOLD/SELL signal.
   - 예측 정확도 기록용 순수 계산 유틸.
   - signal 결과를 dashboard/Telegram에 노출.

## Phase 3 — 자동화 + 고도화
1. 안전한 시작점:
   - `strategy/paper_trader.py`: broker API 없는 in-memory/JSON paper portfolio.
   - signal → paper order intent 생성만 수행, real order client와 분리.
   - 분봉/멀티종목 최적화는 인터페이스 먼저 만들고 실제 broker order는 보류.
2. 실전 전환 조건:
   - 모의투자 최소 기간/로그/손익 검증.
   - 사용자 명시 승인.
   - real-trading guard와 별도 환경변수 필요.

## 이번 구현 단위
- Signal analyzer와 paper-trading simulator를 먼저 추가한다.
- 네트워크/API/Redis/주문 호출 없는 테스트만 추가한다.
- 테스트 통과 후 diff 보고, 커밋/푸시는 사용자 승인 후 진행한다.
