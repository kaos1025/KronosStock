# DRAM·NAND·HBM 데이터 가용성·비용 검증

작성 기준: 2026-07-31 22:02 KST
대상 프로젝트: KronosStock / K-Semiconductor Flow Desk
상태: P1 PM 결정안
주의: 이 문서는 제품/데이터 설계 판단용이며 법률 자문이 아니다. 유료 데이터 구매, 재배포, 고객-facing 표시, 투자상품/예측시장/외부 서비스화 전에는 공급자 약관과 별도 계약을 확인해야 한다.

## 1. 결론

초기 MVP에서는 TrendForce/DRAMeXchange 데이터와 그 파생값을 scoring, decision, AI prompt에 사용하지 않는다. 투자·prediction-use 라이선스가 긍정적으로 확인되기 전 모든 관련 source/feature는 `license_review_required=true`, `BLOCKED_REVIEW`이며 아래 항목은 승인 입력이 아니라 가용성 조사 기록이다.

1. DRAM spot snapshot 방향성
   - 무료 공개 페이지에 노출되는 대표 DRAM chip spot의 항목 구조를 조사했다. `session_average`를 포함한 raw price는 승인 전 저장하지 않는다.
   - `dram_spot_change_direction`, `dram_spot_as_of`, `dram_spot_staleness` 같은 파생 필드도 승인 전 생성·저장·표시하지 않는다.

2. NAND wafer/flash spot snapshot 방향성
   - DRAMeXchange 공개 homepage에 노출되는 512Gb/256Gb/128Gb TLC wafer spot 또는 flash spot의 구조를 조사했으나 수집 후보로 승인하지 않는다.
   - 원본 가격 전체/히스토리 백필은 MVP에서 제외한다.

3. HBM 공개 리서치/IR 기반 상태 플래그도 TrendForce/DRAMeXchange를 출처로 하면 사용하지 않는다.
   - `hbm_supply_tightness_note`, `hbm_generation_note`, `hbm_source_as_of` 같은 qualitative feature도 해당 출처의 라이선스 승인 전 차단한다.
   - HBM 절대 가격, 고객별 가격, ASP 시계열은 공개 spot market이 없고 대부분 유료/계약/추정치라 MVP 필수 지표에서 제외한다.

4. Stanford DAM Memory Prices 같은 공개·다운로드 가능한 장기 시계열은 백테스트/시각화 참고 후보로만 둔다.
   - DRAM/NAND는 consumer retail $/GB 기준이라 반도체 현물/계약 가격과 다르다.
   - HBM은 industry-analyst estimate라 실거래가가 아니다.

## 2. 소스별 판정표

| 소스 | 데이터 구분 | 가용 지표 | 업데이트 주기 | 비용 | 백필 | 저장·표시 제한 / 리스크 | MVP 판정 |
|---|---|---|---|---:|---|---|---|
| TrendForce DRAM Price Trends | 무료 공개 snapshot | DRAM spot table: DDR5/DDR4/DDR3 chip별 daily/session high/low/average/change, related contract report headline | 공개 페이지 기준 daily 수준. 확인 시점에는 2026-07-30 18:10 GMT+8 | 무료 공개 | 공개 화면은 latest 중심. 과거 history/export는 멤버십 영역 | DRAMeXchange/TrendForce 약관상 무단 reproduce/display/publish/distribute 금지. 투자/예측/투기 목적 사용 제한 문구 존재. 원본 표 재게시 금지로 취급 | `BLOCKED_REVIEW`: 원본·파생 scoring/AI 사용 금지 |
| DRAMeXchange homepage | 무료 공개 snapshot | DRAM spot, module spot, flash spot, wafer spot, 일부 contract category listing, DXI timestamp | DRAM/eMMC는 daily timestamp, module/flash/wafer는 weekly 또는 latest snapshot | 무료 공개 | history는 제한적, download/history는 멤버십 | 동일. 공개 table이라도 원본 bulk 저장/재배포/화면 복제는 위험 | `BLOCKED_REVIEW`: 원본·파생 scoring/AI 사용 금지 |
| DRAMeXchange Silver | 유료 원본 데이터 | DRAM/NAND spot·contract download, 5년 Excel, DXI 1년 Excel, chart/history, daily express | spot daily, contract monthly, chart weekly/monthly/quarterly/yearly 가능 | USD 5,000 | DRAM/NAND 5년, DXI 1년 | 멤버십 계약: 내부 reference/research license, 계정 공유/재배포 금지, AI model training/fine-tune/RL/testing 금지 조항. 외부 표시·API 제공은 별도 허가 필요 | MVP 제외. 구매 전 법무/계약 검토 필요 |
| DRAMeXchange Gold | 유료 원본 데이터 | Silver + DRAM/NAND contract/spot download, monthly contract early notice | monthly early notice PDF | USD 11,000 | Silver와 유사 | 동일 | MVP 제외 |
| DRAM Platinum / NAND Platinum | 유료 리서치 | DRAM 또는 NAND monthly datasheet, quarterly report, optional weekly market bulletin | monthly/quarterly, bulletin weekly add-on | 각 USD 18,000 | 리포트 구독 기간 범위 | 동일. 리포트 내용을 AI prompt/raw input으로 넣거나 dashboard에 재게시하지 않도록 별도 계약 필요 | MVP 제외 |
| Memory Platinum / Diamond | 유료 리서치·데이터 | DRAM/NAND monthly datasheet, quarterly reports, company-level datasheet 등 | monthly/quarterly | USD 30,000 / 55,000 | 계약 범위 | 동일 | MVP 이후 검토 |
| HBM Package / HBM Market Bulletin / HBM Industry Analysis | 유료 HBM 리서치 | HBM bulletin, quarterly report/datasheet, price update, supplier share, HBM generation, S/D, ASP/revenue share | bulletin monthly, quarterly report/datasheet quarterly | USD 30,000 | 계약 범위 | HBM 세부 정량은 유료 리서치. raw 저장/재표시/AI학습/재배포 위험 큼 | MVP 제외; 공개 landing headline 파생도 `BLOCKED_REVIEW` |
| Stanford DAM Memory Prices | 무료 공개·다운로드 CSV | DRAM/NAND/HBM $/GB 장기 시계열, inflation-adjusted toggle, HBM generation estimates | page 설명상 DRAM/NAND live refresh monthly 계열 | 무료 | CSV 다운로드 제공 | Methodology상 consumer retail cheapest listed price. HBM은 analyst estimate. 라이선스는 별도 확인 필요 | 보조 참고 후보. scoring 핵심 입력 아님 |
| DRAMWatch | 무료 공개 aggregator | DRAM/HBM/NAND pricing summaries, supplier share, supply/demand, source library | page update Jun 29 2026, 일부 news Jul 28 2026 | 무료 | 사이트 제공 범위 | 2차 aggregator이고 자체 caveat: source disagreement/revisions, directional ranges. 원본 출처 확인 필요 | 보조 monitoring 후보. 원본 출처 우선 |
| Statista | 유료 통계 | DRAM/NAND spot/contract statistics | 통계별 | 유료 | 구독 범위 | paywall·재배포 제한 | MVP 제외 |

## 3. TrendForce/DRAMeXchange 사용 범위 판단

### 3.1 무료 공개 데이터

공개 페이지에서 확인된 내용:

- TrendForce DRAM Price Trends는 DRAM spot table을 공개하고, 확인 시점 기준 `Last Update 2026-07-30 18:10 (GMT+8)`를 표시한다.
- DRAMeXchange homepage도 DRAM spot, module spot, flash spot, wafer spot을 latest snapshot 형태로 노출한다.
- DRAMeXchange homepage의 wafer spot 예시는 512Gb/256Gb/128Gb TLC session average/change를 포함한다.

MVP 허용 범위:

- 라이선스 승인 전 자동 수집·내부 feature화하지 않는다. 사람이 검토한 URL/약관/검토시각 같은 license-review metadata만 기록할 수 있다.
- 사용자-facing Telegram/dashboard/선택적 Notion report에는 원본 또는 방향성 요약을 표시하지 않는다. TrendForce/DRAMeXchange 표 재게시, bulk CSV, API 원본 가격, 원본 chart와 파생 방향 모두 승인 전 금지한다.

### 3.2 유료 멤버십 데이터

TrendForce membership page 기준 비용과 범위:

- Silver USD 5,000: DRAM/NAND spot 또는 monthly contract price의 5년 Excel, DXI 1년 Excel, chart/history, market view.
- Gold USD 11,000: Silver + contract/spot download와 monthly contract early notice.
- DRAM Platinum / NAND Platinum USD 18,000: monthly datasheet, quarterly report, consulting/visiting, weekly bulletin add-on.
- Memory Platinum USD 30,000 / Memory Diamond USD 55,000: DRAM/NAND 통합 monthly/quarterly 리서치와 company-level datasheet 확대.
- HBM Package 또는 HBM Industry Analysis 계열 USD 30,000: monthly HBM bulletin, quarterly HBM report/datasheet, price update, supplier share, generation, ASP/revenue share 등.

MVP 판정:

- 예산 대비 초기 개인용 MVP에는 과하다.
- 특히 계약 약관상 내부 reference/research 범위를 넘어 저장·재표시·재배포·AI 모델 학습/테스트에 쓰는 것은 위험하다.
- 구매하더라도 raw data table을 dashboard/Telegram/API에 노출하지 말고, 계약서에 허용된 내부 분석·파생지표 범위만 사용해야 한다.

## 4. HBM 지표 판단

HBM은 DRAM/NAND와 달리 공개 spot market이 없다. Stanford DAM도 HBM 가격을 “confidential contracts / sparse industry-analyst estimates”로 설명한다. TrendForce HBM reports의 landing page에서 확인 가능한 범위는 다음 정도다.

- HBM Market Bulletin: monthly PDF, USD 30,000, HBM 가격·DDR5 profitability·CSP LTA floor price clause 등 headline과 목차 일부 공개.
- HBM Industry Analysis: quarterly PDF, USD 30,000, supplier share, HBM generation, TSV/backend capacity, S/D ratio, blended ASP, revenue share 등이 리포트 내 포함되는 것으로 안내.

MVP에서는 HBM을 숫자 가격으로 취급하지 않는다. 다음 public qualitative flags만 허용한다.

| 필드 | 값 예시 | source | 사용처 |
|---|---|---|---|
| `hbm_public_market_available` | `false` | Stanford DAM methodology, TrendForce report landing | HBM raw price 미사용 이유 기록 |
| `hbm_supply_tightness` | `tight` / `normalizing` / `unknown` | TrendForce public report headline, company IR/earnings public quote, DRAMWatch with original source check | 종목별 보조 설명 |
| `hbm_generation_phase` | `HBM3e mainstream`, `HBM4 ramping`, `unknown` | TrendForce public landing, supplier IR | qualitative note |
| `hbm_source_as_of` | date/url | each source | stale 판단 |

주의: HBM qualitative flag도 삼성전자와 SK하이닉스 비교·순위화에 쓰지 않는다. 각 종목 카드에서 “HBM 관련 업황 보조 코멘트”로만 독립 주입한다.

## 5. 백필 정책

### 5.1 MVP 백필 범위

- 필수 백필 없음.
- 최초 실행일부터 latest snapshot만 저장한다.
- 과거 데이터가 없으면 score에 임의 momentum을 만들지 않고 `PARTIAL_DATA` 또는 해당 feature `missing_history`로 표시한다.

### 5.2 이후 확장 후보

1. DRAMeXchange Silver 구매
   - 장점: DRAM/NAND 5년 Excel과 DXI 1년 Excel로 백테스트 가능.
   - 단점: USD 5,000부터 시작, 라이선스/AI/재표시 제한 검토 필요.

2. Stanford DAM CSV
   - 장점: 무료, 장기 시계열, raw CSV 제공.
   - 단점: consumer retail $/GB와 추정치라 반도체 spot/contract 직접 proxy로 약함.

3. 자체 스냅샷 축적
   - 장점: 비용 0, 사용 범위 통제 가능.
   - 단점: MVP 시작 이후 데이터만 존재. 사이트 약관상 원본 가격 축적·복제 리스크는 계속 관리해야 함.

## 6. 저장·표시 제한

| 행위 | MVP 판정 | 이유 |
|---|---|---|
| 공개 page latest snapshot을 사람이 확인하고 source URL·약관 검토 기록 | 허용 | license review용 read-only reference이며 scoring/AI 입력 아님 |
| crawler로 latest snapshot 일부를 내부 DB에 저장 | 금지(승인 전) | 투자·prediction-use 및 파생 이용 허용이 확인되지 않음 |
| 원본 price table 전체를 DB에 매일 저장 | 금지에 가깝게 취급 | history/download는 유료 멤버십 가치이고 약관상 reproduce/distribute 리스크 |
| Telegram/dashboard에 원본 표 표시 | 금지 | display/publish/distribute 제한 리스크 |
| 파생 direction/staleness를 scoring/AI에 사용 또는 표시 | 금지(승인 전) | 파생값도 licensing clearance 대상 |
| 유료 report PDF/Excel을 AI prompt에 raw로 투입 | 금지 | 멤버십 약관의 AI model training/fine-tune/RL/testing 금지 및 재배포 제한과 충돌 가능 |
| 유료 data를 외부 API/SaaS/고객 화면으로 제공 | 금지 | 별도 redistribution license 필요 |
| TrendForce data로 예측시장/베팅/파생상품 settlement 지표 구성 | 금지 | 약관상 prediction market/speculative future event use 제한 |
| 종목별 독립 리서치 카드에 source_as_of와 방향성 기록 | 금지(승인 전) | 투자·prediction-use clearance 필요 |

## 7. 라이선스 검토 최소 metadata 계약

초기 구현 task는 가격 snapshot이 아닌 다음 license-review metadata schema만 허용한다.

```json
{
  "source": "trendforce_or_dramexchange_public",
  "source_url": "https://www.trendforce.com/price/dram/dram_spot",
  "source_as_of": "2026-07-30T18:10:00+08:00",
  "as_of_kst": "2026-07-31T22:02:00+09:00",
  "ingested_at_kst": "2026-07-31T22:02:00+09:00",
  "available_data_cutoff": "2026-07-31T22:02:00+09:00",
  "metric_group": "memory_spot_public_snapshot",
  "items": [],
  "raw_price_stored": false,
  "license_review_required": true,
  "source_status": "BLOCKED_REVIEW",
  "feature_status": "LICENSE_BLOCKED",
  "license_scope": "unverified_for_investment_prediction_use",
  "display_policy": "do_not_display_or_score"
}
```

필수 구현 규칙:

- `raw_price_stored=false`가 승인 전 불변값이다. `session_average`를 포함한 원시 가격은 저장하지 않는다.
- 가격 숫자 저장은 `license_review_required=true` 표시만으로 허용되지 않으며, 긍정적 라이선스 확인과 별도 승인 이후 계약을 갱신해야 한다.
- 모든 review record에는 `as_of_kst`, `source_as_of`, `ingested_at_kst`, `available_data_cutoff`가 있어야 한다.
- 이 source에는 scoring feature를 만들지 않으므로 stale feature 판정도 하지 않고 `LICENSE_BLOCKED`를 유지한다.
- memory spot 입력은 MVP 필수가 아니므로 차단 상태가 허가된 국내 데이터 기반 run 전체를 막지 않는다.

## 8. Scoring 반영 결정

MVP scoring에서 TrendForce/DRAMeXchange 메모리 가격 지표와 모든 파생값은 제외한다.

- feature group: `global_memory_price_context`
- group cap: 0%
- 해당 source는 `license_review_required=true`, `BLOCKED_REVIEW`; 전체 run은 허가된 국내 가격/OHLCV·수급만으로 진행 가능
- 라이선스 승인 전 사용 금지 파생값:
  - `dram_spot_direction`: up/down/flat/unknown
  - `dram_spot_change_bucket`: strong_up/up/flat/down/strong_down/unknown
  - `nand_wafer_direction`: up/down/flat/unknown
  - `hbm_supply_tightness`: tight/normalizing/unknown
  - `memory_data_staleness_days`
- 금지:
  - TrendForce 원본 가격표를 feature contribution에 그대로 노출
  - 유료 계약/ASP/HBM 절대가를 무단 저장
  - 삼성전자와 SK하이닉스를 같은 메모리 지표로 상대 순위화

## 9. Gate B 승인 조건

Read-only 데이터 연결 구현 전 체크리스트:

- [ ] source별 약관 URL과 캡처일을 원장에 기록한다.
- [ ] 수집기는 GET/read-only만 사용하며 로그인/구매/우회 접근을 하지 않는다.
- [ ] robots/rate limit를 존중하고 실패 시 retry 폭주를 막는다.
- [ ] raw external response 전체를 로그/Git에 남기지 않는다.
- [ ] Telegram/dashboard/선택적 Notion report에는 source direction·가격·파생값을 표시하지 않는다.
- [ ] 유료 멤버십 또는 로그인 데이터 사용은 별도 승인 task로 분리한다.
- [ ] 실주문/broker API/deploy/systemd 변경은 수행하지 않는다.

## 10. 근거 URL

- TrendForce DRAM Price Trends: https://www.trendforce.com/price/dram/dram_spot
- DRAMeXchange homepage: https://www.dramexchange.com/
- TrendForce / DRAMeXchange Membership Plans: https://www.trendforce.com/membership/DRAMeXchange
- DRAMeXchange Terms of Use: https://www.dramexchange.com/About/TermsOfUse
- TrendForce Membership Service and Subscription Agreement: https://www.trendforce.com/terms/content
- TrendForce Substack Terms / redistribution restriction reference: https://insights.trendforce.com/tos
- TrendForce HBM Market Bulletin landing: https://www.trendforce.com/research/download/RP260421CP3
- TrendForce HBM Industry Analysis landing: https://www.trendforce.com/research/download/RP260204DA3
- Stanford DAM Memory Prices: https://dam.stanford.edu/memory-prices.html
- DRAMWatch public aggregator: https://dramwatch.com/

## 11. PM Review 결과

판정: MVP는 TrendForce/DRAMeXchange 무료·유료 snapshot 및 그 파생 지표를 scoring/decision/AI에 사용하지 않는다.

라이선스 승인 전 차단 지표:

1. DRAM latest public spot direction/as_of
2. NAND wafer 또는 flash latest public spot direction/as_of
3. HBM public qualitative tightness/generation note/as_of
4. source staleness와 missing 상태

명시적 제외:

- 유료 DRAM/NAND 5년 Excel 백필
- DXI 1년 Excel 백필
- HBM 유료 ASP/share/S/D report 원본
- raw price table 사용자-facing 표시
- 원본 데이터 재배포 API

판정: Gate B 데이터 연결은 투자·prediction-use 및 파생 이용이 긍정적으로 확인되기 전 `BLOCKED_REVIEW`다. 허가 전에는 license-review metadata 외 수집·저장·표시·scoring을 진행하지 않는다.
