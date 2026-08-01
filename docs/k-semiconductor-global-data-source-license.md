# K-Semiconductor 글로벌 반도체·환율 데이터 소스/라이선스 검증

작성 기준: 2026-07-31 22:00 KST
대상 프로젝트: KronosStock / K-Semiconductor Flow Desk
상태: P1 데이터 소스 검증안
관련 상위 문서: `docs/k-semiconductor-mvp-operating-scope.md`

## 1. 목적과 범위

MVP에서 삼성전자(005930)와 SK하이닉스(000660)의 독립 분석에 주입할 글로벌 반도체·환율 데이터 공급자를 검증한다. 이 문서는 다음 항목을 고정한다.

- MU, NVDA, TSM, SOXX 또는 SOX, USD/KRW의 가격·거래량·상대수익률 산출 소스
- TWSE 종목별 기관 수급, SEC 13F, Nasdaq short interest, JPX 주간 투자자별 매매 데이터의 빈도와 한계
- 공식 URL/API, 시차, 비용, 저장·재배포 조건, fallback, 데이터 품질 등급

안전 조건:

- 모든 외부 데이터는 read-only로만 사용한다.
- 실주문, broker 주문 API, 계좌 mutation endpoint는 연결하지 않는다.
- 외부 API 원본 응답 전체와 credential/token은 로그/Git/AI prompt/산출물에 남기지 않는다.
- 이 문서는 데이터 계약이며 deploy/production/systemd 변경을 수행하지 않는다.

## 2. 품질 등급 정의

| 등급 | 의미 | MVP 사용 방식 |
|---|---|---|
| A | 공식 기관/거래소/규제기관 소스, 무료 또는 명확한 공개 API/파일, 시차·제약이 명확함 | 기본 소스 후보 |
| B | 상용 또는 제3자 API지만 문서화·SLA·비용 체계가 있고 개인/내부 분석 사용 가능 | 가격/편의성 보완 또는 fallback |
| C | 스크래핑/비공식 endpoint/약관상 재배포 불명확/가끔 차단 가능 | 개발 보조 또는 수동 검증용, 자동 운영 기본값 금지 |
| D | 약관 위반 가능성이 높거나 출처/라이선스 불명확 | MVP 제외 |

상대수익률은 별도 공급자 데이터가 아니라 동일 기준의 adjusted close 또는 close에서 내부 계산한다. 공식/상용 원천 데이터의 재배포 제약을 피하기 위해 외부로는 원시 가격 테이블을 재배포하지 않고, 내부 원장에는 source/as_of/hash/요약 feature만 남긴다.

## 3. MVP 권장 소스 매트릭스

| 데이터 | 기본 소스 | 공식 URL/API | 시차/빈도 | 비용 | 저장·재배포 조건 | fallback | 품질 |
|---|---|---|---|---|---|---|---|
| MU/NVDA 일별 가격·거래량 | Alpha Vantage `TIME_SERIES_DAILY_ADJUSTED` 후보 | `https://www.alphavantage.co/documentation/`, 예: `https://www.alphavantage.co/query?function=TIME_SERIES_DAILY_ADJUSTED&symbol=NVDA&outputsize=compact&apikey=$ALPHAVANTAGE_API_KEY` | 일별 후보. 무료 entitlement/schema/rate limit은 미검증 | Free entitlement 미확인 + 유료 tier | Gate B live probe 전 `BLOCKED_REVIEW`; 내부 분석 캐시만, 원시 OHLCV 재배포 금지 | Nasdaq Data Link/Polygon/IEX Cloud 같은 유료 feed, 또는 수동 검증용 Stooq | B 후보 |
| TSM 가격·거래량 | TWSE STOCK_DAY, 필요 시 Alpha Vantage ADR `TSM` 보조 | `https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY?date=YYYYMMDD&stockNo=2330&response=json` | 월 단위 일별 OHLCV, 대만 장 마감 후. TWSE 2330은 TSMC 보통주, Alpha `TSM`은 NYSE ADR라 환율/ADR 괴리가 있음 | TWSE 공개 무료 | TWSE 공개 웹/API. 내부 분석·source metadata 저장. 원시 대량 재배포는 거래소 이용조건 확인 전 금지 | Alpha Vantage `TSM` ADR, Stooq `2330.TW` 수동 검증 | A for TWSE, B/C fallback |
| SOXX ETF 가격·거래량 | Alpha Vantage `TIME_SERIES_DAILY_ADJUSTED` 후보 | `https://www.alphavantage.co/query?function=TIME_SERIES_DAILY_ADJUSTED&symbol=SOXX&outputsize=compact&apikey=$ALPHAVANTAGE_API_KEY` | 일별 후보. 무료 entitlement/schema/rate limit은 미검증 | Free entitlement 미확인 + 유료 tier | Gate B live probe 전 `BLOCKED_REVIEW`; 원시 재배포 금지 | iShares fund page 수동 확인, Nasdaq Data Link/Polygon/IEX 유료 feed | B 후보 |
| SOX 지수 | Nasdaq 지수 라이선스 feed 또는 Nasdaq Data Link 계열 유료/라이선스 상품 | `https://www.nasdaq.com/solutions/data/nasdaq-data-link/api`, `https://data.nasdaq.com/tools/api` | 보통 EOD/지연/실시간은 라이선스별 상이 | 대체로 유료/라이선스 필요 | 지수 데이터는 재배포 제한이 강함. 라이선스 확정 전 원시 저장·재배포 금지 | MVP 기본 proxy는 SOXX로 대체. SOX는 사람이 확인하는 참고 지표 | B if licensed, C otherwise |
| USD/KRW | 한국은행 ECOS Open API `731Y001/D/0000001` | `https://ecos.bok.or.kr/api/`, 예: `https://ecos.bok.or.kr/api/StatisticSearch/$BOK_ECOS_KEY/json/kr/1/100/731Y001/D/20260701/20260731/0000001` | 일별. 한국은행 통계 확정 후 사용. 장중 거래 환율 아님 | 무료 인증키 | ECOS Open API는 개발 활용 가능. 출처 표기와 내부 캐시 중심. 외부 원시 재배포는 한국은행 이용조건 확인 전 금지 | FRED `DEXKOUS` daily noon buying rate, 서울외국환중개/은행 고시 수동 확인 | A |
| TWSE 종목별 기관 수급 | TWSE T86 | `https://www.twse.com.tw/rwd/zh/fund/T86?date=YYYYMMDD&selectType=ALLBUT0999&response=json` | 일별, 대만 장 마감 후. 종목별 외국인/투신/딜러 순매수 주식 수 | 무료 공개 | 내부 feature 저장. 원시 대량 재배포 금지. TSMC 2330 행만 추출 권장 | 제3자 TW market data API, TWSE CSV 수동 다운로드 | A |
| SEC 13F | SEC EDGAR filing index + 13F XML information table | `https://www.sec.gov/edgar/search/`, `https://data.sec.gov/submissions/CIK##########.json`, archive filing XML | 분기별. quarter-end 후 45일 이내 제출. confidential treatment와 CUSIP/ticker mapping 한계 | 무료 | SEC EDGAR는 무료 접근 가능. User-Agent 선언 및 10 req/s 이하 fair access 준수. 내부 분석 cache OK, 원본 bulk mirror는 지양 | sec-api.io/WhaleWisdom/Nasdaq institutional holdings 같은 상용 정규화 feed | A for raw filings, B for normalized vendors |
| Nasdaq short interest | Nasdaq Trader Short Interest | `https://nasdaqtrader.com/trader.aspx?id=ShortInterest`, publication schedule `https://nasdaqtrader.com/Trader.aspx?id=ShortIntPubSch` | 월 2회. mid-month/end-month settlement 기준, dissemination date 16:00 ET 이후 공개. rolling 12 months | 공개 웹. 상세/대량/히스토리 확장은 라이선스 가능성 | issue별 short interest는 공식 공개. 자동 수집은 사이트 정책 준수. 원시 재배포 금지 | FINRA short volume은 다른 개념이라 fallback이 아니라 보조 feature. 상용 short interest feed | A for published page, B for licensed bulk |
| JPX 주간 투자자별 매매 | JPX Trading by Type of Investors weekly Excel | `https://www.jpx.co.jp/english/markets/statistics-equities/investor-type/`; 현재 파일 예: `/english/markets/statistics-equities/investor-type/.../stock_val_1_YYMMWW.xls` | 주간. JPX Data Portal 설명상 대략 매주 4영업일 15:00경 업데이트. 종목별 아님, 시장/투자자 유형 aggregate | 무료 공개 | 내부 feature 저장. 원본 Excel 재배포 금지. 출처/as_of 기록 | JPxData Portal 또는 상용 일본 시장 데이터 vendor | A for market aggregate |

## 4. 소스별 세부 검증

### 4.1 MU, NVDA, SOXX 가격·거래량

Alpha Vantage daily adjusted API는 Gate B 검증 후보이며 아직 권장 기본값으로 확정하지 않는다. 특히 `TIME_SERIES_DAILY_ADJUSTED`의 무료 계정 entitlement는 문서만으로 확인된 것으로 간주하지 않는다.

- endpoint: `TIME_SERIES_DAILY_ADJUSTED`
- 필수 파라미터: `symbol`, `apikey`
- 권장 파라미터: `outputsize=compact`, `datatype=json`
- 저장 필드: `open/high/low/close/adjusted_close/volume/dividend/split_coefficient/source_as_of`
- 상대수익률: `adjusted_close.pct_change()`로 내부 계산

한계:

- Alpha Vantage는 거래소 공식 원천이 아니라 제3자 API다.
- 무료 plan이 이 endpoint를 제공하는지와 실제 호출 한도는 미검증이므로 tickers 3~5개 batch에 충분하다고 가정하지 않는다.
- 데이터 재배포 권리는 별도 라이선스 없이 보장하지 않는다. 외부 UI에는 원시 가격표 대신 score contribution과 출처만 노출한다.

Fallback 판단:

- 운영 신뢰성이 더 필요하면 Polygon/IEX/Nasdaq Data Link 등 유료 feed를 Gate B에서 다시 검토한다.
- Stooq/Yahoo 계열은 편리하지만 약관/차단/비공식성 때문에 자동 운영 기본값으로 쓰지 않는다.

### 4.2 TSM 가격·거래량

TSMC의 본주 기준 가격·거래량은 TWSE `2330`을 우선한다.

검증된 호출 예:

```text
GET https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY?date=20260701&stockNo=2330&response=json
```

실제 read-only 확인 결과 `stat=OK`, fields는 `日期, 成交股數, 成交金額, 開盤價, 最高價, 最低價, 收盤價, 漲跌價差, 成交筆數, 註記` 형식으로 반환된다.

한계:

- TSM ADR(`TSM`)와 TWSE 본주(`2330`)는 통화, 거래시간, ADR ratio, 환율 영향이 다르다.
- 한국 반도체 분석의 글로벌 peer signal로는 TWSE 본주를 기본으로 두고, 미국 장 sentiment가 필요할 때만 ADR을 별도 보조 feature로 둔다.

### 4.3 USD/KRW

권장 기본값은 한국은행 ECOS다.

- 통계 코드: `731Y001` 주요국 통화의 대원화환율
- 주기: `D`
- USD item code: `0000001`
- endpoint template: `https://ecos.bok.or.kr/api/StatisticSearch/$BOK_ECOS_KEY/json/kr/1/100/731Y001/D/{start}/{end}/0000001`

한계:

- ECOS 환율은 통계/분석용 확정 데이터다. 실시간 환전/송금/주문 기준 환율이 아니다.
- API key가 필요하므로 secret manager/env var에 저장하고 로그에 출력하지 않는다.

Fallback:

- FRED `DEXKOUS`는 무료·안정적 fallback이나 미국 영업일/시각 기준이고 한국 장 마감 직후 기준과 다를 수 있다.
- 장중 민감도를 계산하지 않는 MVP에서는 ECOS daily 확정값을 우선한다.

### 4.4 TWSE 종목별 기관 수급

TSMC 2330의 기관별 수급은 TWSE T86을 사용한다.

검증된 호출 예:

```text
GET https://www.twse.com.tw/rwd/zh/fund/T86?date=20260730&selectType=ALLBUT0999&response=json
```

실제 read-only 확인 결과 `stat=OK`, fields는 외국인, 외자자영, 투신, 자영업자/헤지, 삼대법인 순매수 주식 수를 포함한다.

MVP 사용 방식:

- 전체 T86 응답을 저장하지 말고 `證券代號 == 2330` 행만 추출한다.
- feature는 외국인 순매수, 투신 순매수, dealer 순매수, 삼대법인 합계를 별도 column으로 저장한다.
- 삼성전자/SK하이닉스 종목별 원장에는 같은 TSMC 수급 feature를 공통 글로벌 지표로 주입하되, 두 국내 종목 간 상대 비교 feature로 재가공하지 않는다.

### 4.5 SEC 13F

SEC 13F는 실시간 수급이 아니라 미국 기관 보유 현황의 지연된 분기 snapshot이다.

공식 조건:

- $100M 이상 13(f) securities 운용 기관은 Form 13F를 제출한다.
- 제출 기한은 각 분기 종료 후 45일 이내다.
- SEC EDGAR 접근은 무료이며, SEC fair access 정책상 현재 max request rate는 10 requests/second이고 User-Agent를 명시해야 한다.

MVP 한계:

- NVDA/MU/TSM ADR/SOXX의 기관 보유 변화는 quarter-end 후 최대 45일 늦게 관측된다.
- 13F는 short, non-US ordinary shares 일부, derivatives 일부, confidential treatment를 완전하게 보여주지 않는다.
- ticker가 아니라 CUSIP 중심이므로 mapping table과 corporate action 검증이 필요하다.

사용 판단:

- daily scoring의 핵심 signal로 쓰지 않는다.
- 장기/분기 institutional positioning 보조 feature로만 사용한다.
- `source_lag_days`를 45~60일로 명시하고 confidence를 낮게 둔다.

### 4.6 Nasdaq short interest

Nasdaq Trader의 short interest는 issue별 rolling 12 months가 공개되고 월 2회 업데이트된다.

공식 페이지 문구 검증:

- issue별 short interest 제공
- rolling 12 months
- mid-month/end-month settlement date 기준
- dissemination date 16:00 ET 이후 공개

MVP 한계:

- 일별 short pressure가 아니다.
- publication schedule에 따른 시차가 크고, short volume과 개념이 다르다.
- MU/NVDA/SOXX에는 유용하지만 TSM ADR/TSM TWSE 본주와 직접 대응하지 않을 수 있다.

사용 판단:

- `short_interest_ratio`, `days_to_cover`가 안정적으로 확보될 때만 보조 feature로 사용한다.
- daily score에서 과대 가중하지 않고 feature group cap을 둔다.
- 결측 시 `missing_optional`로 처리한다.

### 4.7 JPX 주간 투자자별 매매

JPX weekly investor type data는 일본 시장 전체 또는 세그먼트별 투자자 유형 aggregate다.

검증된 공식 페이지:

- `https://www.jpx.co.jp/english/markets/statistics-equities/investor-type/`
- 페이지에 현재 주차별 `stock_vol_1_*.xls`, `stock_val_1_*.xls` 파일 링크가 노출된다.
- JPX Data Portal 설명상 weekly 데이터는 대략 매주 4영업일 15:00경 업데이트된다.

MVP 한계:

- 종목별/반도체별 수급이 아니다.
- 일본 시장 risk-on/risk-off proxy로만 사용 가능하다.
- 한국 반도체 개별 종목 신호에는 낮은 가중치만 허용한다.

사용 판단:

- `jpx_foreign_net_value_weekly`, `jpx_individual_net_value_weekly` 같은 market sentiment feature로 사용한다.
- 국내 종목별 원장에는 동일한 macro/global proxy로 주입하되 cross-rank 산출에 사용하지 않는다.

## 5. 저장 계약

외부 source별 raw cache와 normalized ledger를 분리한다.

권장 구조:

```text
data/raw/{source}/{YYYYMMDD}/{request_hash}.json|csv|xls     # gitignore, 원본 응답 전체
ledgers/market_inputs/{symbol}/{as_of_kst}.jsonl             # normalized, secret-free
```

normalized row 필수 필드:

```json
{
  "ledger_symbol": "005930",
  "source": "alpha_vantage|twse|bok_ecos|sec_edgar|nasdaq_trader|jpx",
  "source_url": "redacted template or public URL",
  "source_as_of": "2026-07-30",
  "ingested_at_kst": "2026-07-31T16:05:00+09:00",
  "available_data_cutoff": "2026-07-31T16:00:00+09:00",
  "lag_days": 1,
  "quality_grade": "A|B|C|D",
  "redistribution": "internal_only_no_raw_redistribution",
  "fields": {"feature_name": "value"},
  "missing_reason": null
}
```

금지:

- API key 포함 URL 저장
- Authorization header 저장
- 외부 원본 응답 전체를 Git 또는 AI prompt에 첨부
- 결측 데이터를 0으로 조용히 대체

## 6. Fallback 우선순위

| primary 장애 | fallback | fallback 사용 시 상태 |
|---|---|---|
| Alpha Vantage rate limit/API 장애 | 유료 feed가 있으면 유료 feed, 없으면 Stooq/Yahoo 수동 검증 | 자동 운영은 `PARTIAL_DATA`, 수동 검증은 `BLOCKED_REVIEW` 가능 |
| TWSE STOCK_DAY/T86 장애 | 다음 수집 tick 재시도, TWSE CSV 수동 다운로드 | TSM 가격/수급은 `missing_optional` 또는 TSM feature 제외 |
| ECOS 장애/API key 없음 | FRED `DEXKOUS` | `PARTIAL_DATA`, source_lag/기준 차이 명시 |
| SEC EDGAR rate limit | backoff 후 재시도, index file batch 처리 | 13F feature 제외, daily score 영향 없음 |
| Nasdaq short interest 페이지 장애 | 다음 dissemination 후 재시도, 상용 feed 검토 | optional missing |
| JPX Excel 링크 변경 | 공식 page 재파싱, JPxData Portal 수동 확인 | optional missing |

## 7. MVP 결론

1. 일별 글로벌 가격·거래량은 Alpha Vantage를 Gate B 후보로 두고, live entitlement/schema/rate-limit probe 통과 후에만 기본 source로 승격한다. SOX 대신 SOXX를 proxy 후보로 둔다.
2. TSMC 본주 가격·거래량과 기관 수급은 TWSE `STOCK_DAY`, `T86`이 가장 적합하다.
3. USD/KRW는 한국은행 ECOS `731Y001/D/0000001`을 기본으로 사용하고, FRED는 fallback으로만 둔다.
4. SEC 13F, Nasdaq short interest, JPX weekly investor type은 모두 시차가 큰 보조 feature다. daily 핵심 필수 입력으로 두면 안 된다.
5. 라이선스 리스크를 낮추기 위해 원시 데이터 재배포를 하지 않고, 내부 normalized feature/score contribution/source metadata만 저장한다.

## 8. Gate B acceptance criteria

구현 task는 다음 조건을 만족해야 Gate B를 통과한다.

- source별 endpoint template, rate limit/backoff, User-Agent 정책이 코드에 명시된다.
- secret/API key는 env 또는 secret store에서만 읽고 로그/원장/AI prompt에 남기지 않는다.
- 원본 응답은 gitignored raw cache에만 저장하거나 저장하지 않는다.
- normalized ledger에는 `source`, `as_of_kst`, `source_as_of`, `ingested_at_kst`, `available_data_cutoff`, `quality_grade`, `lag_days`, `missing_reason`이 포함된다.
- Alpha Vantage 실제 credential로 `TIME_SERIES_DAILY_ADJUSTED` entitlement, 정상/오류 response schema, adjusted 필드, account rate limit을 read-only probe해 secret 없이 증적한다. 통과 전 `license_review_required=true` 및 `BLOCKED_REVIEW`다.
- `source_status`/`feature_status`는 원천·feature 품질만 나타내고 실행의 canonical 상태는 상위 문서의 `run_status`를 따른다.
- cutoff 이후 데이터가 과거 feature에 포함되지 않는 no-look-ahead fixture가 통과한다.
- SEC EDGAR 호출은 10 req/s 이하와 declared User-Agent를 지킨다.
- TWSE T86은 2330 행만 추출하며 전체 원본 대량 재배포를 하지 않는다.
- Nasdaq short interest, SEC 13F, JPX weekly는 optional feature이며 결측 시 분석이 중단되지 않는다.
- 두 국내 종목 간 상대 비교·순위화 feature를 만들지 않는다.
