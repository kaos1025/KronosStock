# KIS/KRX 국내 투자자 수급 API 실응답 검증

기준 시각: 2026-07-31 21:59 KST
대상 종목: 삼성전자(005930), SK하이닉스(000660)
안전 범위: read-only 시세/통계 조회만 검토. KIS 주문/계좌/실주문 API는 호출하지 않음.
상위 계약: `docs/k-semiconductor-mvp-operating-scope.md`의 Gate B, canonical status/timestamp, 영구 원장 및 no-look-ahead 규칙을 따른다.

## 결론

1. 장중 외국인·기관 방향성은 KIS `종목별 외인기관 추정가집계` 또는 `국내기관_외국인 매매종목가집계`로 받을 수 있다. 단, 공식 확정 데이터가 아니라 증권사 직원 입력/단순 누계 기반 가집계다.
2. 장마감 후 확정/공식성은 KRX `투자자별 거래실적(개별종목)`이 우선 fallback이다. 이 화면은 기관 세부 주체(금융투자/보험/투신/사모/은행/기타금융/연기금 등)까지 분해된다.
3. 이 VPS의 `.env`에는 KIS 실전/모의 AppKey/AppSecret/HTS/계좌 값이 비어 있어 KIS 실호출은 토큰 발급 단계까지 가지 못했다. 따라서 이번 산출물의 KIS 응답 필드는 공식 `koreainvestment/open-trading-api` 샘플 코드와 체크 스크립트 기준으로 검증했다.
4. KRX 웹 JSON 직접 호출은 현재 세션/권한 요구로 `400 LOGOUT`을 반환했다. 브라우저 화면/공식 페이지 스키마와 pykrx wrapper의 KRX bld/필드 매핑은 확인했지만, 이 환경에서 실데이터 rows는 확보하지 못했다.
5. MVP 기본 정책은 `KIS 장중 가집계 → KRX 장마감 확정 → 수동 CSV/증권사 HTS export` 순서가 안전하다. 자동 매매/주문 판단에는 장중 가집계만 단독 사용하지 않는다.

## 검증 환경에서 실제 확인한 실행 결과

### KIS 자격증명 상태

명령:

```bash
python3 - <<'PY'
from pathlib import Path
for line in Path('.env').read_text(errors='ignore').splitlines():
    s=line.strip()
    if not s or s.startswith('#') or '=' not in s: continue
    k,v=s.split('=',1)
    k=k.replace('export ','').strip()
    if k.startswith('KIS'):
        val=v.strip().strip('"').strip("'")
        print(k, 'SET' if val else 'EMPTY_OR_PLACEHOLDER', 'len', len(val))
PY
```

실제 출력 요약:

```text
KIS_HTS_ID EMPTY_OR_PLACEHOLDER len 0
KIS_APPKEY EMPTY_OR_PLACEHOLDER len 0
KIS_APPSECRET EMPTY_OR_PLACEHOLDER len 0
KIS_ACCOUNT EMPTY_OR_PLACEHOLDER len 0
KIS_VIRTUAL_APPKEY EMPTY_OR_PLACEHOLDER len 0
KIS_VIRTUAL_APPSECRET EMPTY_OR_PLACEHOLDER len 0
KIS_VIRTUAL_ACCOUNT EMPTY_OR_PLACEHOLDER len 0
KIS_VIRTUAL_HTS_ID EMPTY_OR_PLACEHOLDER len 0
KIS_USE_VIRTUAL SET len 4
KIS_KEEP_TOKEN SET len 4
KIS_TOKEN_DIR SET len 6
KIS_USE_WEBSOCKET SET len 5
```

직접 KIS 토큰 스모크도 `credentials_present False False`로 중단됐다. 토큰 또는 secret 값은 출력/저장하지 않았다.

### KRX 직접 JSON 호출 상태

KRX `MDCSTAT02301`에 대해 삼성전자 ISIN(`KR7005930003`)으로 직접 POST를 시도했다.

요청 bld/파라미터:

```text
bld=dbms/MDC/STAT/standard/MDCSTAT02301
strtDd=20260724
endDd=20260731
isuCd=KR7005930003
inqTpCd=1
trdVolVal=1
askBid=1
```

실제 출력:

```text
400 text/html; charset=utf-8 LOGOUT
```

판정: KRX data.krx.co.kr의 웹 JSON endpoint는 이 환경에서 세션/인증 또는 봇 방어를 요구한다. 운영 파이프라인에는 KRX 공식 Open API 인증키 또는 허용된 다운로드/API 경로를 확보해야 한다.

## KIS 후보 API 검증

### 1) 주식현재가 투자자

공식 샘플:

- 출처: `koreainvestment/open-trading-api/examples_llm/domestic_stock/inquire_investor/inquire_investor.py`
- API: `[국내주식] 기본시세 > 주식현재가 투자자[v1_국내주식-012]`
- Method: GET
- Path: `/uapi/domestic-stock/v1/quotations/inquire-investor`
- TR ID: `FHKST01010900`
- 파라미터:
  - `FID_COND_MRKT_DIV_CODE`: `J`(KRX), `NX`(NXT)
  - `FID_INPUT_ISCD`: 6자리 종목코드. 예: `005930`, `000660`
- 유의사항: 공식 샘플 주석상 당일 데이터는 장 종료 후 제공. 외국인은 외국인+기타외국인 합산 의미.

응답 필드(체크 스크립트의 컬럼 매핑 기준):

| raw field | 의미 |
|---|---|
| `stck_bsop_date` | 주식 영업 일자 |
| `stck_clpr` | 주식 종가 |
| `prdy_vrss` | 전일 대비 |
| `prdy_vrss_sign` | 전일 대비 부호 |
| `prsn_ntby_qty` | 개인 순매수 수량 |
| `frgn_ntby_qty` | 외국인 순매수 수량 |
| `orgn_ntby_qty` | 기관계 순매수 수량 |
| `prsn_ntby_tr_pbmn` | 개인 순매수 거래대금 |
| `frgn_ntby_tr_pbmn` | 외국인 순매수 거래대금 |
| `orgn_ntby_tr_pbmn` | 기관계 순매수 거래대금 |
| `prsn_shnu_vol` | 개인 매수 거래량 |
| `frgn_shnu_vol` | 외국인 매수 거래량 |
| `orgn_shnu_vol` | 기관계 매수 거래량 |
| `prsn_shnu_tr_pbmn` | 개인 매수 거래대금 |
| `frgn_shnu_tr_pbmn` | 외국인 매수 거래대금 |
| `orgn_shnu_tr_pbmn` | 기관계 매수 거래대금 |
| `prsn_seln_vol` | 개인 매도 거래량 |
| `frgn_seln_vol` | 외국인 매도 거래량 |
| `orgn_seln_vol` | 기관계 매도 거래량 |
| `prsn_seln_tr_pbmn` | 개인 매도 거래대금 |
| `frgn_seln_tr_pbmn` | 외국인 매도 거래대금 |
| `orgn_seln_tr_pbmn` | 기관계 매도 거래대금 |

MVP 적합성:

- 외국인/기관 합계의 매수·매도·순매수 수량/대금 확인 가능.
- 연기금 세부분류 없음.
- 장중 가집계용이 아니라 장마감 후 성격으로 보는 게 안전.

### 2) 종목별 외인기관 추정가집계

공식 샘플:

- 출처: `examples_llm/domestic_stock/investor_trend_estimate/investor_trend_estimate.py`
- API: `[국내주식] 시세분석 > 종목별 외인기관 추정가집계[v1_국내주식-046]`
- Method: GET
- Path: `/uapi/domestic-stock/v1/quotations/investor-trend-estimate`
- TR ID: `HHPTJ04160200`
- 파라미터: `MKSC_SHRN_ISCD` = 6자리 종목코드

응답 필드:

| raw field | 의미 |
|---|---|
| `bsop_hour_gb` | 입력구분/입력시각 구분 |
| `frgn_fake_ntby_qty` | 외국인 수량(가집계) |
| `orgn_fake_ntby_qty` | 기관 수량(가집계) |
| `sum_fake_ntby_qty` | 합산 수량(가집계) |

장중/확정 구분:

- 공식 샘플 주석: 한국투자 MTS 현재가 > 투자자 > 투자자동향 탭의 `추정(주)` 화면과 대응.
- 증권사 직원이 장중 집계/입력한 자료를 단순 누계한 수치.
- 입력시간: 외국인 09:30, 11:20, 13:20, 14:30 / 기관종합 10:00, 11:20, 13:20, 14:30. ±10분 또는 장운영 사정에 따른 변동 가능.

MVP 적합성:

- 장중 외국인·기관 방향성 신호로 적합.
- 연기금 세부분류 없음.
- 금액이 아니라 수량 중심 가집계이므로 확정 데이터와 분리 저장 필요.

### 3) 국내기관_외국인 매매종목가집계

공식 샘플:

- 출처: `examples_llm/domestic_stock/foreign_institution_total/foreign_institution_total.py`
- API: `[국내주식] 시세분석 > 국내기관_외국인 매매종목가집계[국내주식-037]`
- Method: GET
- Path: `/uapi/domestic-stock/v1/quotations/foreign-institution-total`
- TR ID: `FHPTJ04400000`
- 파라미터 예시:
  - `FID_COND_MRKT_DIV_CODE=V`
  - `FID_COND_SCR_DIV_CODE=16449`
  - `FID_INPUT_ISCD=0000`(전체), `0001`(코스피), `1001`(코스닥) 등
  - `FID_DIV_CLS_CODE=0`(수량정렬), `1`(금액정렬)
  - `FID_RANK_SORT_CLS_CODE=0`(순매수상위), `1`(순매도상위)
  - `FID_ETC_CLS_CODE=0`(전체), `1`(외국인), `2`(기관계), `3`(기타)

응답 필드:

| raw field | 의미 |
|---|---|
| `hts_kor_isnm` | HTS 한글 종목명 |
| `mksc_shrn_iscd` | 유가증권 단축 종목코드 |
| `ntby_qty` | 순매수 수량 |
| `stck_prpr` | 현재가 |
| `prdy_vrss_sign` | 전일 대비 부호 |
| `prdy_vrss` | 전일 대비 |
| `prdy_ctrt` | 전일 대비율 |
| `acml_vol` | 누적 거래량 |
| `frgn_ntby_qty` | 외국인 순매수 수량 |
| `orgn_ntby_qty` | 기관계 순매수 수량 |
| `ivtr_ntby_qty` | 투자신탁 순매수 수량 |
| `bank_ntby_qty` | 은행 순매수 수량 |
| `insu_ntby_qty` | 보험 순매수 수량 |
| `mrbn_ntby_qty` | 종금 순매수 수량 |
| `fund_ntby_qty` | 기금 순매수 수량 |
| `etc_orgt_ntby_vol` | 기타단체 순매수 거래량 |
| `etc_corp_ntby_vol` | 기타법인 순매수 거래량 |
| `frgn_ntby_tr_pbmn` | 외국인 순매수 거래대금 |
| `orgn_ntby_tr_pbmn` | 기관계 순매수 거래대금 |
| `ivtr_ntby_tr_pbmn` | 투자신탁 순매수 거래대금 |
| `bank_ntby_tr_pbmn` | 은행 순매수 거래대금 |
| `insu_ntby_tr_pbmn` | 보험 순매수 거래대금 |
| `mrbn_ntby_tr_pbmn` | 종금 순매수 거래대금 |
| `fund_ntby_tr_pbmn` | 기금 순매수 거래대금 |
| `etc_orgt_ntby_tr_pbmn` | 기타단체 순매수 거래대금 |
| `etc_corp_ntby_tr_pbmn` | 기타법인 순매수 거래대금 |

장중/확정 구분:

- HTS eFriend Plus [0440] 외국인/기관 매매종목 가집계 화면 대응.
- 증권사 직원이 장중 집계/입력한 자료의 단순 누계.
- 입력시간: 외국인 09:30, 11:20, 13:20, 14:30 / 기관종합 10:00, 11:20, 13:20, 14:30. ±10분 또는 변동 가능.

MVP 적합성:

- 삼성전자/SK하이닉스가 상위 리스트에 포함되는 경우 장중 수급 랭킹/방향성 확인 가능.
- `fund_*`가 `기금`으로 표기되어 연기금과 정확히 같은지 확인 필요. KRX의 `연기금 등`과 명칭/범위가 다를 수 있으므로 MVP에서는 `fund`를 연기금 확정 대체로 쓰지 않는다.

### 4) 종목별 투자자매매동향(일별)

공식 샘플:

- 출처: `examples_llm/domestic_stock/investor_trade_by_stock_daily/investor_trade_by_stock_daily.py`
- API: `[국내주식] 시세분석 > 종목별 투자자매매동향(일별)`
- Method: GET
- Path: `/uapi/domestic-stock/v1/quotations/investor-trade-by-stock-daily`
- TR ID: `FHPTJ04160001`
- 파라미터:
  - `FID_COND_MRKT_DIV_CODE`: `J`(KRX), `NX`(NXT), `UN`(통합)
  - `FID_INPUT_ISCD`: 6자리 종목코드
  - `FID_INPUT_DATE_1`: 조회 기준일 예 `20250812`
  - `FID_ORG_ADJ_PRC`: 공란
  - `FID_ETC_CLS_CODE`: 공란
- 응답: `output1`, `output2` 두 DataFrame. 공식 샘플은 pagination/continuation(`tr_cont`) 처리 포함.

MVP 적합성:

- 과거 일별 범위/페이지네이션 검증 후보.
- 이번 환경에서는 KIS credential 부재로 실제 rows와 최대 과거범위는 확인하지 못했다.

## KRX 후보 API 검증

### 투자자별 거래실적(개별종목)

공식 화면:

- `https://data.krx.co.kr/contents/MDC/MDI/outerLoader/index.cmd?screenId=MDCSTAT023&locale=ko_KR&kosdaqGlobalYn=1`
- 메뉴: 통계 > 기본 통계 > 주식 > 거래실적 > 투자자별 거래실적(개별종목)

화면/pykrx 매핑 기준 bld:

| 용도 | bld | 주요 파라미터 |
|---|---|---|
| 기간합계 | `dbms/MDC/STAT/standard/MDCSTAT02301` | `strtDd`, `endDd`, `isuCd`, `inqTpCd=1`, `trdVolVal=1`, `askBid=1` |
| 일별추이 일반 | `dbms/MDC/STAT/standard/MDCSTAT02302` | `strtDd`, `endDd`, `isuCd`, `inqTpCd=2`, `trdVolVal=1/2`, `askBid=1/2/3` |
| 일별추이 상세 | `dbms/MDC/STAT/standard/MDCSTAT02303` | 위와 동일 + `detailView=1` |

KRX/pykrx wrapper에서 확인한 출력 컬럼:

기간합계 raw:

| raw field | 의미 |
|---|---|
| `INVST_TP_NM` | 투자자구분 |
| `ASK_TRDVOL` | 매도 거래량 |
| `BID_TRDVOL` | 매수 거래량 |
| `NETBID_TRDVOL` | 순매수 거래량 |
| `ASK_TRDVAL` | 매도 거래대금 |
| `BID_TRDVAL` | 매수 거래대금 |
| `NETBID_TRDVAL` | 순매수 거래대금 |

일별추이 일반 컬럼:

```text
날짜, 기관합계, 기타법인, 개인, 외국인합계, 전체
```

일별추이 상세 컬럼:

```text
날짜, 금융투자, 보험, 투신, 사모, 은행, 기타금융, 연기금, 기타법인, 개인, 외국인, 기타외국인, 전체
```

연기금 세부분류:

- KRX 상세 일별추이에서 `연기금` 또는 화면상 `연기금 등`이 제공된다.
- 세부 기관분류까지 필요한 MVP 확정 수급은 KRX 상세 일별추이를 primary로 삼는다.

과거 조회 범위:

- pykrx wrapper는 `strtDd/endDd` 장기 요청 시 730일 단위로 분할 호출한다.
- 이는 KRX endpoint가 긴 기간을 지원하더라도 안정성을 위해 2년 단위 chunking이 필요하다는 구현상 근거다.
- 실제 KRX Open API/웹 경로의 정책상 최대 과거범위·호출 제한은 인증키/세션 확보 후 재검증해야 한다.

## 샘플 저장 스키마 제안

### 장중 가집계 테이블: `investor_flow_intraday_estimates`

```json
{
  "symbol": "005930",
  "market": "KRX",
  "source": "KIS",
  "endpoint": "investor-trend-estimate",
  "source_status": "INTRADAY_ESTIMATE",
  "as_of_kst": "2026-07-31T14:35:00+09:00",
  "source_as_of": "2026-07-31T14:30:00+09:00",
  "ingested_at_kst": "2026-07-31T14:35:00+09:00",
  "available_data_cutoff": "2026-07-31T14:35:00+09:00",
  "observed_at_kst": "2026-07-31T14:35:00+09:00",
  "input_slot": "14:30",
  "foreign_net_buy_qty_est": 0,
  "institution_net_buy_qty_est": 0,
  "foreign_institution_net_buy_qty_est": 0,
  "raw_field_map_version": "kis-investor-trend-estimate-20250601"
}
```

### 장마감 확정 테이블: `investor_flow_daily_confirmed`

```json
{
  "symbol": "005930",
  "market": "KRX",
  "source": "KRX",
  "endpoint": "MDCSTAT02303",
  "source_status": "CLOSE_CONFIRMED",
  "as_of_kst": "2026-07-31T16:00:00+09:00",
  "source_as_of": "2026-07-31T15:30:00+09:00",
  "ingested_at_kst": "2026-07-31T15:55:00+09:00",
  "available_data_cutoff": "2026-07-31T16:00:00+09:00",
  "trade_date": "2026-07-31",
  "metric": "net_buy_value",
  "foreign": 0,
  "institution_total": 0,
  "pension_fund": 0,
  "financial_investment": 0,
  "insurance": 0,
  "investment_trust": 0,
  "private_fund": 0,
  "bank": 0,
  "other_finance": 0,
  "retail": 0,
  "other_corporation": 0,
  "other_foreign": 0,
  "currency": "KRW"
}
```

상태값(`source_status`; canonical 실행 상태인 `run_status`와 구분):

- `INTRADAY_ESTIMATE`: KIS 장중 가집계. 입력 시각/추정치임을 UI에 표시.
- `CLOSE_CONFIRMED`: KRX 장마감 후 확정/공식 데이터.
- `PARTIAL_DATA`: KIS는 성공했으나 KRX 확정 미수신.
- `MISSING_REQUIRED_DATA`: 외국인/기관/연기금 중 MVP 필수값 결손.
- `FALLBACK_MANUAL_CSV`: KRX 자동 호출 실패 후 수동 CSV/HTS export 사용.

## 호출 제한·라이선스 메모

- KIS Open API는 KIS Developers 서비스 신청 및 AppKey/AppSecret 필요. 토큰은 24시간 유효이며 토큰 발급 자체도 제한이 있으므로 토큰 캐시가 필요하다.
- KIS 포털 공지에 신규 고객 초당 호출 제한 안내가 존재한다. 운영 전 계정별 제한을 포털에서 확인해야 한다.
- KIS 샘플 코드는 참고용이며, 샘플 활용으로 인한 손해를 한국투자증권이 책임지지 않는다는 고지 포함.
- KRX data.krx.co.kr 웹 JSON은 이번 환경에서 `LOGOUT`을 반환했다. 비공식 scraping에 의존하지 말고 KRX Open API 인증키 또는 허용된 다운로드/CSV 절차를 fallback으로 둔다.
- 외부 데이터는 투자 판단 보조용이며, 장중 가집계와 장마감 확정 데이터는 원장/상태를 분리 저장한다.

## 운영 fallback 순서

1. 장중: KIS `investor-trend-estimate`로 09:30/11:20/13:20/14:30 슬롯 기준 추정 수량 저장.
2. 장중 랭킹/교차확인: KIS `foreign-institution-total`에서 대상 종목이 포함되는 경우 수량/금액 및 기관 세부 추정 필드 저장.
3. 장마감: KRX `MDCSTAT02303` 상세 일별추이로 외국인·기관합계·연기금 확정값 적재.
4. KRX 자동 실패: KRX Open API 인증키 재시도 또는 수동 CSV export를 `FALLBACK_MANUAL_CSV`로 적재.
5. 리포트: 같은 날짜에 `CLOSE_CONFIRMED`가 있으면 장중 추정치를 대체하지 말고 별도 섹션에서 추정 대비 확정 차이로 표시.

## 남은 블로커

- KIS 실 AppKey/AppSecret/HTS ID가 필요하다. 현재 `.env`는 키 이름만 있고 값이 비어 있다. 이는 상위 문서 Gate B의 credential/live-response 검증 blocker이며 확보 전 해당 source는 `BLOCKED_REVIEW`, run은 상황에 따라 `PARTIAL_DATA` 또는 `MISSING_REQUIRED_DATA`다.
- KRX 공식 Open API 인증키 또는 data.krx.co.kr 세션/다운로드 허용 경로가 필요하다.
- 위 두 자격이 확보되면 삼성전자/SK하이닉스에 대해 실제 rows, pagination 범위, 계정별 rate limit을 다시 측정해야 한다.
- 실제 rows가 확보되면 `source_as_of <= available_data_cutoff` 및 `ingested_at_kst <= available_data_cutoff`를 강제하고, cutoff 이후 확정·정정 수급이 과거 run에 섞이지 않는 no-look-ahead test를 Gate B 증적에 포함한다.
