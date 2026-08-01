# K-Semiconductor Flow Desk MVP 운영 범위·승인 게이트

작성 기준: 2026-07-31 KST
대상 프로젝트: KronosStock / K-Semiconductor Flow Desk
상태: P0 PM 확정안

## 1. 목적

Notion 제안서의 큰 방향을 즉시 개발 가능한 MVP 범위로 고정한다. MVP는 삼성전자와 SK하이닉스를 서로 비교·순위화하지 않고, 각 종목을 독립된 분석 단위로 다루는 개인용 AI 리서치/알림 도구다.

이 문서는 이후 구현·리뷰·QA task의 기준 계약이다. 코드가 이 문서와 충돌하면 이 문서를 우선하고, 범위 변경은 별도 PM 승인 후 문서 갱신으로만 반영한다.

## 2. 핵심 원칙

### 2.1 독립 분석 원칙

- 삼성전자(005930)와 SK하이닉스(000660)는 절대 상호 비교하거나 순위화하지 않는다.
- 각 종목은 별도 입력 원장, 별도 점수 원장, 별도 AI 요청 원장, 별도 성과 원장을 가진다.
- UI/알림/리포트에서 두 종목을 한 화면에 함께 보여줄 수는 있으나, `A가 B보다 낫다`, `1위/2위`, `상대 우위`, `pair trade` 표현은 MVP 범위 밖이다.
- 공통 글로벌 지표는 각 종목 분석에 독립적으로 주입하고, 두 종목 간 상대 점수로 재가공하지 않는다.

### 2.2 AI 역할 분리

- 결정론적 정량 계산이 1차 판정을 만든다.
- AI는 원장화된 입력과 점수 결과를 설명·요약·리스크 해석하는 보조 역할만 한다.
- AI 응답은 원본 데이터나 점수를 덮어쓰지 않는다.
- AI가 매수/매도/주문 지시를 직접 생성하거나 broker API를 호출하는 경로는 금지한다.

### 2.3 안전 원칙

- 실주문 및 broker 주문 API 호출 금지.
- 외부 데이터는 read-only로만 사용한다.
- production deploy, systemd timer 변경/중지, 운영 credential 변경은 별도 사용자 승인 전 금지한다.
- 비밀값·원시 토큰·Authorization header·원본 외부 응답 전체를 로그/Git/산출물에 남기지 않는다.

## 3. MVP In Scope

### 3.1 대상 종목

- 삼성전자: `005930`
- SK하이닉스: `000660`

MVP에서는 이 두 종목만 정식 지원한다. 다른 종목 확장 구조를 과도하게 설계하지 않는다.

### 3.2 데이터 입력

각 종목별로 다음 입력을 독립 원장에 저장한다.

- 국내 가격/OHLCV
- 국내 외국인 수급
- 국내 기관 수급
- 국내 연기금 수급
- 관련 글로벌 반도체 지표
  - Micron
  - NVIDIA
  - TSMC
  - SOXX 또는 동등한 반도체 섹터 proxy
  - 메모리 가격 지표가 확보되는 경우 read-only 보조 입력
- 기존 Kronos 예측 결과·밴드·불확실성 지표는 신규 입력에서 제외한다. 기존 값은 과거 archive/evaluation에서만 읽을 수 있으며 신규 decision/scoring/AI prompt에는 전달하지 않는다.

데이터 source가 일시적으로 없으면 해당 입력을 `missing`으로 기록하고 전체 분석을 임의 값으로 채우지 않는다.

### 3.3 데이터 기준시각

MVP의 기본 기준시각은 KST 장 마감 이후 최신 확정 데이터다.

- 기본 daily cut: KST 15:30 이후 확정 가능한 국내 장 데이터
- 리포트 생성 기본 시간: KST 16:00 이후
- 장중 실행이 필요한 경우: `intraday_partial` 상태로 표시하고 daily 확정 리포트와 분리
- 해외 지표는 한국 리포트 생성 시점에서 확인 가능한 최신 close 또는 latest snapshot을 사용하되, 기준시각을 source별로 기록

모든 분석 결과와 원장은 다음 canonical timestamp를 사용한다.

- `as_of_kst`: 해당 종목 판단이 생성된 KST 시각
- `source_as_of`: source가 그 값을 대표한다고 명시한 원천 기준시각
- `ingested_at_kst`: 시스템이 값을 수신·정규화한 KST 시각
- `available_data_cutoff`: 해당 run이 사용할 수 있었던 정보의 최종 cutoff. `source_as_of`와 `ingested_at_kst`가 모두 cutoff 이하인 데이터만 허용한다.

### 3.4 분석 horizon

MVP의 horizon은 다음으로 고정한다.

- 판단 horizon: T+1, T+5, T+20 거래일
- 알림/리포트 표현: `next_1d`, `next_5d`, `next_20d`
- 장기 투자 의견, 월간/분기 전망, 목표주가 산출은 MVP 범위 밖

각 판단은 T+1/T+5/T+20 모두를 의무 정산한다. 거래일 캘린더 기준 종가와 동일 기준 벤치마크를 사용하며, 미도래 horizon은 `PENDING_SETTLEMENT`, 휴장·가격 결손은 사유와 함께 미정산으로 남긴다. 기존 Kronos horizon은 신규 계약의 기준이 아니다.

### 3.5 산출물

각 종목별로 다음 산출물을 만든다.

- 입력 원장: 어떤 source의 어떤 기준시각 데이터를 사용했는지
- 점수 원장: 결정론적 scoring 결과와 feature별 기여
- AI 요청 원장: AI에 전달한 redacted prompt/input hash/요청 목적
- AI 응답 원장: AI 요약, 리스크 설명, confidence 메타데이터
- 성과 원장: 이후 실제 가격/수급과의 사후 비교 결과
- Telegram 또는 dashboard용 요약: 종목별 독립 카드
- Notion: 선택적 report channel. 필수 운영 의존성이나 원장은 아니다.

## 4. MVP Out of Scope

다음은 MVP에서 하지 않는다.

- 삼성전자 vs SK하이닉스 상대 순위, 추천 순위, pair trade 제안
- 실전 주문, broker 주문 API 호출, 자동매매
- 사용자 자금/포지션 기반 리밸런싱
- 목표주가 산출 또는 수익 보장형 문구
- multi-user 서비스화, 과금, 공개 SaaS
- 임의의 신규 종목 자동 확장
- AI가 원장 값을 생성·수정하는 구조
- 누락 데이터를 추정치로 조용히 대체
- production deploy/systemd timer 변경을 자동으로 수행

## 5. 상태 Taxonomy

`run_status`만 실행의 canonical 단일 상태다. `source_status`는 원천 row의 확정성·수집 상태, `feature_status`는 개별 feature의 품질·가용성 상태이며 실행 상태처럼 사용하지 않는다.

| canonical `run_status` | 의미 | 후속 처리 |
|---|---|---|
| `READY` | 필수 입력이 있고 분석 실행 가능 | scoring/AI 요약 실행 가능 |
| `PARTIAL_DATA` | 일부 보조 source 누락, 핵심 국내 데이터는 존재 | 제한 리포트 생성 가능, 누락 source 명시 |
| `STALE_DATA` | 기준시각이 허용 범위보다 오래됨 | 새 데이터 수집 전 AI 요약 금지 |
| `MISSING_REQUIRED_DATA` | 필수 국내 가격/OHLCV 또는 핵심 수급 데이터 없음 | 분석 중단, 원인 기록 |
| `SCORING_DONE` | 결정론적 점수 산출 완료 | AI 설명 요청 가능 |
| `AI_SUMMARY_DONE` | AI 설명 응답 저장 완료 | 알림/대시보드 노출 가능 |
| `BLOCKED_REVIEW` | PM/리뷰어 확인 필요 | 사용자 또는 reviewer unblock 필요 |
| `FAILED` | 실행 오류 발생 | 오류 코드/단계 기록, 자동 주문 금지 |
| `ARCHIVED` | 과거 실행 원장 보존 상태 | 변경 금지, 읽기 전용 |

상태는 종목별·실행별로 독립적이다. 한 종목 실패가 다른 종목 실행을 막지 않는다.

`source_status` 예시는 `INTRADAY_ESTIMATE`, `CLOSE_CONFIRMED`, `MISSING`, `STALE`, `BLOCKED_REVIEW`이고, `feature_status` 예시는 `READY`, `MISSING_OPTIONAL`, `MISSING_REQUIRED`, `STALE`, `LICENSE_BLOCKED`다. source/feature 상태를 `SCORING_DONE` 같은 run 진행 상태와 혼합하지 않는다.

## 6. Scoring 계약

MVP scoring은 종목별 독립 점수만 만든다.

- score 범위: `-100` ~ `+100`
- score label:
  - `positive_watch`
  - `neutral_watch`
  - `risk_watch`
  - `insufficient_data`
- 사용자-facing opinion taxonomy는 `POSITIVE`, `ACCUMULATION`, `WATCH`, `NEUTRAL`, `CAUTION`, `DISTRIBUTION`, `INSUFFICIENT_DATA`로 고정한다.
- 내부 score label의 결정론적 매핑은 `positive_watch → WATCH`, `neutral_watch → NEUTRAL`, `risk_watch → CAUTION`, `insufficient_data → INSUFFICIENT_DATA`다. `POSITIVE`, `ACCUMULATION`, `DISTRIBUTION`은 별도 문서화·버전된 결정론적 rule이 승인되기 전에는 출력하지 않으며 AI가 임의 승격·강등하지 않는다.
- score는 매수/매도 주문 지시가 아니다.
- score 산출식과 feature contribution은 원장에 저장한다.
- 글로벌 공통 신호는 중복 과대반영을 피하기 위해 feature group별 cap을 둔다.
- Kronos 예측·밴드·불확실성과 TrendForce/DRAMeXchange 원본 및 그 파생값은 신규 scoring에서 기여도 0이며 AI 입력에서도 제외한다. TrendForce/DRAMeXchange는 투자·예측 목적 라이선스가 명시적으로 확인될 때까지 `license_review_required=true`, source/feature `BLOCKED_REVIEW`로 기록한다.

## 6.1 판단 원장·no-look-ahead 계약

- 입력·점수·AI·판단·정산 원장은 TTL 없는 영구 append-only 원장이다. cache TTL을 원장 보존정책으로 사용하지 않는다.
- 각 run은 위 네 canonical timestamp와 source/feature snapshot, rule/prompt/model version을 저장한다.
- 자동 테스트는 `source_as_of <= available_data_cutoff`, `ingested_at_kst <= available_data_cutoff`를 강제하고 cutoff 이후 정정값·미래 종가·미래 수급이 feature에 포함되면 실패해야 한다.
- 재실행/백필은 당시 cutoff snapshot만 사용하고 현재 알려진 값을 과거 run에 덮어쓰지 않는다. 정정은 새 ledger event로 남긴다.
- T+1/T+5/T+20 정산기는 판단 생성과 분리하고, 각 horizon 도래 전 결과 접근 금지 및 거래일 정렬 회귀 테스트를 포함한다.

## 7. AI 요청 계약

AI 요청은 종목별로 분리한다.

필수 포함:

- symbol/code/name
- `as_of_kst`
- 사용 source 목록과 source별 기준시각
- 결정론적 score 및 feature contribution
- 누락 데이터 목록
- 금지 지시: 주문/실행/상대 비교/순위화 금지

금지 포함:

- raw credential/token
- 외부 API 원본 응답 전체
- broker 주문 가능 payload
- 두 종목을 상대 비교하도록 유도하는 prompt

AI 응답은 다음 구조로 저장한다.

- `summary`
- `drivers`
- `risks`
- `data_gaps`
- `confidence_note`
- `not_investment_advice` 고지

## 8. 운영 승인 게이트

### Gate A: 문서/범위 승인

- 본 문서가 PM 검토를 통과해야 구현 task가 진행된다.
- 변경 시 문서 diff를 남기고 관련 child task의 기준을 갱신한다.

### Gate B: Read-only 데이터 연결 승인

- 외부 source별 read-only endpoint와 rate limit, credential 취급 방식을 리뷰한다.
- Alpha Vantage `TIME_SERIES_DAILY_ADJUSTED`의 무료 entitlement는 미검증이다. 실제 키로 endpoint entitlement, 응답 schema, adjusted 필드, 계정별 rate limit/error payload를 read-only live probe하여 기록하기 전에는 source/feature를 `BLOCKED_REVIEW`로 둔다.
- TrendForce/DRAMeXchange와 모든 파생값은 투자·prediction use 허용이 긍정적으로 서면 확인되기 전 `license_review_required=true` 및 `BLOCKED_REVIEW`다.
- 쓰기 endpoint, broker 주문 endpoint, 계좌 mutation endpoint는 연결하지 않는다.

### Gate C: Scoring 구현 승인

- 종목별 독립 scoring이 확인되어야 한다.
- 두 종목 비교·순위화 필드가 없어야 한다.
- 누락 데이터 처리와 상태 taxonomy 테스트가 있어야 한다.

### Gate D: AI 요청/응답 승인

- AI prompt가 redacted 원장 입력만 사용해야 한다.
- AI가 주문/실행/상대비교를 제안하지 못하도록 시스템·사용자 prompt에 명시한다.
- AI 요청/응답 원장에는 secret이 없어야 한다.

### Gate E: UI/알림 승인

- Telegram/dashboard는 종목별 독립 카드로 표시한다.
- `삼성전자 > SK하이닉스` 같은 비교 문구가 없어야 한다.
- 투자 조언/수익 보장 오해를 줄이는 고지를 포함한다.

### Gate F: 운영 변경 승인

- commit/push는 사용자 포괄 승인 범위에 따라 가능하다.
- deploy, production 변경, systemd timer 변경/중지, 실주문은 별도 승인 전 금지한다.

## 9. PM Review 결과

PM 기준으로 본 MVP 범위는 실행 가능하다.

승인 조건:

- 구현 task는 이 문서의 in/out scope와 독립 분석 원칙을 acceptance criteria로 포함한다.
- 각 child task는 실주문/broker API 금지를 반복 명시한다.
- 테스트에는 최소한 다음 회귀가 포함되어야 한다.
  - 삼성전자와 SK하이닉스 원장이 분리된다.
  - scoring 결과에 cross-rank 또는 comparison field가 없다.
  - source 기준시각이 산출물에 남는다.
  - canonical timestamp 네 필드와 cutoff 경계 no-look-ahead 테스트가 통과한다.
  - 모든 판단이 T+1/T+5/T+20 정산 대상으로 영구 원장에 남는다.
  - 누락 데이터는 `PARTIAL_DATA` 또는 `MISSING_REQUIRED_DATA`로 표시된다.
  - AI prompt에 secret/order payload가 포함되지 않는다.

판정: PM 승인 가능. 구현 단계로 진행해도 된다.
