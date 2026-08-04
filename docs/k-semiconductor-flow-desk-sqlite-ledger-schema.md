# K-Semiconductor Flow Desk SQLite 영구 원장 스키마 설계

작성 기준: 2026-08-04 KST
대상 migrations:
- `migrations/001_k_semiconductor_flow_desk_permanent_ledgers.sql`
- `migrations/002_k_semiconductor_flow_desk_update_guard_upgrade.sql`
상태: initial schema 및 기존 영속 원장 UPDATE guard 호환 migration 구현 / 독립 리뷰 승인

전제: fresh DB에는 `001`을 적용한 뒤 `002`를 적용한다. 이미 `001`로 생성된 영속 DB에도 `002`를 적용하여 구형 동명 UPDATE trigger를 transaction 안에서 strict 정의로 교체한다. production runner는 두 migration을 순서대로 적용하고 schema version 1과 2를 모두 검증한다.

## 1. 목적

K-Semiconductor Flow Desk의 판단·출처·성과 원장을 Redis cache가 아닌 SQLite 영구 원장으로 보존한다. 이 원장은 삼성전자(`005930`)와 SK하이닉스(`000660`)를 독립 분석 단위로 저장하며, 수집 원본 metadata, source snapshot, 정규화 feature, 결정론적 판단, AI 요청/응답, T+1/T+5/T+20 성과 정산을 TTL 없이 append-first로 남긴다.

이 문서는 구현자가 migration을 검토하고, reviewer가 PK/인덱스/중복 방지/no-look-ahead/백업 정책을 확인할 수 있도록 schema 의도와 예시 query를 함께 고정한다.

## 2. 핵심 불변조건

1. 종목 독립성
   - 모든 run, feature, decision, AI I/O, settlement는 `symbol`을 가진다.
   - 글로벌 공통 source snapshot은 `symbol IS NULL`을 허용하지만 feature로 주입될 때는 반드시 특정 `run_id + symbol`에 귀속된다.
   - 삼성전자와 SK하이닉스 간 상대 순위, pair score, 비교 feature는 schema에 두지 않는다.

2. canonical timestamp 4종 / 기준시각 계약
   - `as_of_kst`: 해당 판단 또는 AI 요청이 생성된 KST 시각.
   - `source_as_of`: source가 값의 기준으로 명시한 시각.
   - `ingested_at_kst`: 시스템이 값을 수신·정규화한 시각.
   - `available_data_cutoff`: 해당 run이 볼 수 있었던 정보의 최종 cutoff.

3. no-look-ahead
   - run/decision/AI request는 `julianday(available_data_cutoff) <= julianday(as_of_kst)`를 강제해 서로 다른 UTC offset도 절대시각으로 비교한다.
   - source snapshot과 feature도 `julianday`로 source/ingest/cutoff 순서를 비교하고, parse 불가능한 값은 거부한다.
   - `*_kst`, `available_data_cutoff`, `as_of_kst`는 parse 가능한 `+09:00` timestamp여야 한다. `source_as_of`는 parse 가능하면 미국/Taipei 등 외부 source offset 또는 날짜 표현을 유지할 수 있다.
   - feature/decision/AI request는 trigger로 run의 `symbol`과 `available_data_cutoff`가 일치해야 insert된다.
   - settlement는 판단 이후 별도 테이블에 저장한다. `ksf_decisions`는 미래 가격을 가지지 않고, `ksf_performance_settlements`가 horizon 도래 후 결과를 붙인다.

4. TTL 없음
   - 모든 테이블은 영구 보존을 전제로 한다.
   - cache 만료 정책은 원장 보존정책으로 사용하지 않는다.
   - 삭제 대신 `run_status='ARCHIVED'`와 archive 사유를 사용한다.

5. append-first
   - 재실행·백필·정정은 기존 row를 덮어쓰기보다 새 `run_id`, 새 `snapshot_id`, 새 `feature_id`로 남긴다.
   - append-only 성질은 DB trigger로 직접 강제한다. 의미·계보(provenance) 컬럼은 insert 이후 UPDATE가 abort 되고, 상태 전이 lifecycle 컬럼만 update가 허용된다.
     - `ksf_runs`: `run_id/symbol/trading_date/as_of_kst/available_data_cutoff/scoring_ruleset_version/prompt_template_version/model_policy_version/created_at_kst` 불변. `run_status`와 `ARCHIVED` 전환(`archived_at_kst`/`archived_reason`)은 허용. archived run은 전체 불변.
     - `ksf_source_snapshots`: `snapshot_id/run_id/symbol`(NULL 분리·parent 교체 포함 금지), `source_metadata_id/source_name/source_kind`, `source_as_of/ingested_at_kst/available_data_cutoff`, `raw_ref_uri/raw_ref_sha256/normalized_payload_sha256/snapshot_metadata_json`, `created_at_kst` 불변. `source_status/quality_grade/lag_days/missing_reason` lifecycle은 허용.
     - `ksf_normalized_features`: `feature_id/run_id/symbol/source_snapshot_id`(NULL 분리 포함 금지), `feature_group/feature_name/feature_version`, `value_num/value_text/value_json/unit`, `source_as_of/ingested_at_kst/available_data_cutoff/contribution_cap_bps/created_at_kst` 불변. `feature_status/missing_reason` lifecycle은 허용.
     - `ksf_decisions`·`ksf_ai_requests`·`ksf_ai_responses`: 행 전체 append-only(UPDATE 전면 abort). 정정은 새 run/decision/request/response로 남기며 hash와 내용의 divergence를 허용하지 않는다.
     - `ksf_performance_settlements`: `settlement_id/decision_id/run_id/symbol/horizon_days/base_trade_date/due_after_kst/created_at_kst` 불변. `PENDING_SETTLEMENT` → 결과 lifecycle(`target_trade_date/settlement_status/base_close/target_close/return_bps/benchmark·kospi·industry 지표/direction hit/turnover·cost/net_return_bps/settlement_cutoff_kst/settlement_source_snapshot_id/settled_at_kst/missing_reason`)은 허용.
   - delete는 trigger로 막지 않지만 FK `ON DELETE RESTRICT`가 참조된 row 삭제를 거부하며, 운영 규칙상 delete 대신 archive를 사용한다.

## 3. 테이블 개요

### 3.1 `ksf_schema_versions`

마이그레이션 적용 이력이다. SQLite 자체에는 migration manager가 없으므로 최소 version ledger를 둔다.

주요 키:
- PK: `version`
- UNIQUE: `name`

적용 순서:
- 마이그레이션은 번호 순서대로 적용한다: `001_k_semiconductor_flow_desk_permanent_ledgers.sql` → `002_k_semiconductor_flow_desk_update_guard_upgrade.sql`.
- 001은 `CREATE TRIGGER IF NOT EXISTS`를 사용하므로, 예전(약한) 같은 이름 trigger 정의를 가진 기존 영구 DB에 001을 재실행해도 정의가 교체되지 않는다.
- 002가 이 gap을 닫는다: 모든 UPDATE/불변성 guard trigger를 `DROP TRIGGER IF EXISTS` 후 현재 strict 정의로 재생성하고, version 2를 idempotent하게 기록한다. fresh DB와 기존 DB 모두 001+002 적용 후 동일한 trigger 집합을 가진다.
- `ksf.production_runner`는 매 실행마다 001→002를 순서대로 적용하고 version 1과 2가 각각 정확히 1회 기록됐는지 검증한다(불일치 시 fail-closed).

### 3.2 `ksf_symbols`

MVP 지원 종목 registry다.

기본 seed:
- `005930` 삼성전자 / KOSPI
- `000660` SK하이닉스 / KOSPI

CHECK:
- `symbol`은 6자리 숫자 glob.
- `market`은 `KOSPI` 또는 `KOSDAQ`.

### 3.3 `ksf_runs`

종목별 판단 실행 원장이다. 하나의 run은 한 종목, 한 거래일, 한 cutoff, 한 scoring/prompt version 조합을 대표한다.

주요 컬럼:
- `run_id`: 애플리케이션이 생성하는 UUID/ULID 문자열.
- `symbol`: 독립 분석 대상.
- `trading_date`: 판단 기준 KRX 거래일 `YYYY-MM-DD`.
- `run_status`: 운영 문서의 canonical 상태 taxonomy.
- `as_of_kst`, `available_data_cutoff`: no-look-ahead 기준.
- `scoring_ruleset_version`, `prompt_template_version`, `model_policy_version`: 버전 재현성.

중복 방지:
- `symbol + trading_date + available_data_cutoff + scoring_ruleset_version + prompt_template_version` unique index.

주의:
- 같은 거래일이라도 cutoff나 version이 다르면 별도 run으로 저장한다.
- `available_data_cutoff <= as_of_kst`를 강제한다. 즉 run 생성 시점보다 늦은 cutoff를 허용하지 않으며, 16:05 판단이 16:00 cutoff를 사용하는 정상 케이스는 허용한다.

### 3.4 `ksf_collected_source_metadata`

원본 수집 metadata와 라이선스/정책 ledger다. 외부 API 원본 응답 전체가 아니라 endpoint template, 약관 URL, rate limit, User-Agent, 저장 정책 같은 검토 가능한 metadata만 저장한다.

주요 컬럼:
- `source_name`, `endpoint_template`, `method`, `source_url`, `terms_url`
- `license_scope`, `rate_limit_policy`, `user_agent_policy`
- `quality_grade`: `A/B/C/D/BLOCKED`
- `raw_storage_policy`: `NONE`, `GITIGNORED_INTERNAL_ONLY`, `METADATA_ONLY`

중복 방지:
- `source_name + capture_date + source_url + terms_url` unique.

### 3.5 `ksf_source_snapshots`

특정 run이 사용할 수 있었던 source별 snapshot ledger다. 국내 가격/OHLCV, 국내 수급, 글로벌 가격, FX, memory license review 같은 입력 단위를 기록한다.

주요 컬럼:
- `run_id`: 해당 run에 직접 귀속된 snapshot. 전역 검토 snapshot은 NULL 가능.
- `symbol`: 종목별 source면 필수, 글로벌 공통 source면 NULL 가능.
- `source_metadata_id`: `ksf_collected_source_metadata.metadata_id`를 가리키는 NOT NULL FK. trigger로 metadata의 `source_name`과 일치해야 한다.
- `source_name`, `source_kind`, `source_status`
- `source_as_of`, `ingested_at_kst`, `available_data_cutoff`
- `quality_grade`, `lag_days`, `missing_reason`
- `raw_ref_uri`, `raw_ref_sha256`: gitignored 내부 cache reference만. tokenized URL 저장 금지.
- `normalized_payload_sha256`: feature 생성에 사용한 정규화 payload hash.

중복 방지:
- `run_id/symbol/source/source_kind/source_as_of/cutoff/payload_hash/raw_hash` unique.

no-look-ahead:
- CHECK로 parse 가능성과 `julianday(source_as_of) <= julianday(cutoff)`, `julianday(ingested_at_kst) <= julianday(cutoff)`를 검증한다.
- trigger로 `run_id`가 있으면 run의 cutoff 및 symbol과 일치해야 한다.
- trigger로 metadata와 snapshot의 `source_name`이 같아야 한다. FK만 맞고 source가 다른 metadata 연결은 거부한다.

### 3.6 `ksf_normalized_features`

정규화 feature ledger다. source snapshot을 특정 종목 run에 주입한 결과를 저장한다.

주요 컬럼:
- `run_id`, `symbol`, `source_snapshot_id`
- `feature_group`, `feature_name`, `feature_version`
- `feature_status`: `READY`, `MISSING_OPTIONAL`, `MISSING_REQUIRED`, `STALE`, `LICENSE_BLOCKED`
- `value_num`, `value_text`, `value_json`
- `source_as_of`, `ingested_at_kst`, `available_data_cutoff`
- `contribution_cap_bps`: feature group cap 표현. memory 가격 지표처럼 score 제외인 경우 0으로 둔다.

중복 방지:
- `run_id + symbol + feature_group + feature_name + feature_version` unique.

no-look-ahead:
- feature 자체 timestamp CHECK.
- trigger로 run cutoff/symbol 일치.
- trigger로 연결된 source snapshot이 feature cutoff보다 미래가 아님을 확인.
- 연결 snapshot의 `symbol`이 NULL(글로벌 공통)이거나 feature symbol과 같아야 하고, snapshot `run_id`가 NULL이 아니면 feature run과 같아야 한다.

### 3.7 `ksf_decisions`

결정론적 scoring 판단 원장이다. AI 결과가 아니라 deterministic score/rule 산출물이다.

주요 컬럼:
- `run_id`, `symbol`
- `horizon_days`: 1, 5, 20만 허용.
- `deterministic_score`: -100~100.
- `score_label`: `positive_watch`, `neutral_watch`, `risk_watch`, `insufficient_data`.
- `user_opinion`: 사용자-facing taxonomy. 승인 전에는 기본 매핑인 `WATCH/NEUTRAL/CAUTION/INSUFFICIENT_DATA` 중심으로 사용한다.
- `scoring_ruleset_version`
- `feature_snapshot_sha256`: 해당 판단에 사용된 feature 집합 hash.
- `feature_contributions_json`, `rationale_json`

중복 방지:
- `run_id + horizon_days` unique.

주의:
- T+1/T+5/T+20 모두 row를 만든다.
- 미래 성과는 여기에 저장하지 않는다.

### 3.8 `ksf_ai_requests`

AI 요청 ledger다. prompt 원문 전체 대신 redacted preview와 hash를 저장한다.

주요 컬럼:
- `purpose`: `explain_decision`, `risk_summary`, `data_gap_summary`
- `prompt_template_version`, `redaction_policy_version`
- `input_ledger_hash_sha256`: AI에 전달한 원장 입력의 결정적 hash.
- `prompt_hash_sha256`: redacted prompt 전체 hash.
- `prompt_redacted_preview`: secret-free 짧은 preview만.
- `model_provider`, `model_name`

중복 방지:
- `run_id + purpose + prompt_template_version + input_hash + prompt_hash + model_provider + model_name` unique.

금지:
- credential/token, Authorization header, broker 주문 가능 payload, 두 종목 상대 비교 prompt 저장 금지.

### 3.9 `ksf_ai_responses`

AI 응답 ledger다. AI는 원장 값을 덮어쓰지 않으며 summary/risk/data gap 설명만 저장한다.

주요 컬럼:
- `response_status`: `OK`, `REFUSED`, `FAILED`, `BLOCKED_REVIEW`
- `response_hash_sha256`
- `summary`, `drivers_json`, `risks_json`, `data_gaps_json`, `confidence_note`
- `not_investment_advice`

중복 방지:
- `ai_request_id + response_hash_sha256` unique.

### 3.10 `ksf_performance_settlements`

T+1/T+5/T+20 성과 정산 ledger다. 판단 생성과 분리되어 horizon 도래 후 채워진다.

주요 컬럼:
- `decision_id`, `run_id`, `symbol`, `horizon_days`
- `base_trade_date`, `target_trade_date`
- `settlement_status`: `PENDING_SETTLEMENT`, `SETTLED`, `MISSING_PRICE`, `CALENDAR_BLOCKED`, `FAILED`
- `base_close`, `target_close`, `return_bps`: 종목의 비용 차감 전 절대 수익률.
- `kospi_return_bps`, `kospi_excess_return_bps`, `industry_return_bps`, `industry_excess_return_bps`: KOSPI 및 반도체 업종 benchmark와 초과 수익률.
- `direction_hit`, `status_direction_hit`, `opinion_direction_hit`: score, 상태 label, 사용자 opinion 방향 적중 여부.
- `max_drawdown_bps`: base부터 target까지 보유 구간 최대 낙폭.
- `turnover_assumption`, `transaction_cost_bps`, `net_return_bps`: 회전율/거래비용 가정과 비용 차감 수익률.
- `settlement_cutoff_kst`: 정산 실행 시 적용한 no-look-ahead cutoff.
- `settlement_source_snapshot_id`: 정산 가격 snapshot 출처.
- `due_after_kst`, `settled_at_kst`, `missing_reason`

중복 방지:
- `decision_id + horizon_days` unique.

정산 규칙:
- insert 시점에는 기본적으로 `PENDING_SETTLEMENT` row를 먼저 만든다.
- target 거래일 도래 후 별도 정산기가 `SETTLED` row 상태와 성과 값을 채운다.
- horizon 미도래 상태에서는 `target_close`, `return_bps`, `settled_at_kst`가 NULL이어야 한다.
- target은 calendar day가 아닌 제공된 KRX 거래 세션에서 horizon만큼 이동해 정한다. 캘린더가 부족하면 `target_trade_date`를 NULL로 두고 `CALENDAR_BLOCKED` 및 사유를 기록한다.
- target 거래 세션이 정해지면 `due_after_kst`는 해당 세션의 종가 확정 시각(`target_trade_date` 16:00 KST)을 기록한다. `CALENDAR_BLOCKED`는 target을 알 수 없으므로 현재 정산 실행 시각을 기록한다.
- 가격의 `source_as_of`, `ingested_at_kst`, `available_data_cutoff`가 모두 정산 cutoff 이하여야 하며 `CLOSE_CONFIRMED` 가격만 사용한다.
- 동일 decision/horizon의 `SETTLED` row는 재실행해도 변경하지 않는다. `MISSING_PRICE`/`PENDING_SETTLEMENT` row는 이후 적격 가격이 나타나면 정산할 수 있다.

## 4. 인덱스 설계

주요 조회 패턴을 기준으로 인덱스를 둔다.

- 최신 종목 run: `ksf_runs(symbol, as_of_kst DESC)`
- 상태별 운영 queue: `ksf_runs(run_status, trading_date DESC)`
- run별 source/feature 조회: `ksf_source_snapshots(run_id, source_name)`, `ksf_normalized_features(run_id, feature_group)`
- 종목별 feature history: `ksf_normalized_features(symbol, feature_name, source_as_of DESC)`
- 종목별 판단 history: `ksf_decisions(symbol, as_of_kst DESC, horizon_days)`
- 정산 due queue: `ksf_performance_settlements(settlement_status, due_after_kst)`

## 5. Look-ahead 방지 설계

SQLite migration이 직접 강제하는 것:

```sql
CHECK (julianday(source_as_of) IS NOT NULL)
CHECK (julianday(source_as_of) <= julianday(available_data_cutoff))
CHECK (julianday(ingested_at_kst) <= julianday(available_data_cutoff))
```

그리고 trigger가 다음을 확인한다.

- source snapshot이 run에 귀속되면 run cutoff와 snapshot cutoff가 같아야 한다.
- feature는 run symbol/cutoff와 일치해야 한다.
- feature가 참조하는 source snapshot은 feature cutoff보다 미래 데이터일 수 없고, global(`symbol IS NULL`)이 아닌 snapshot은 동일 symbol이어야 하며 non-NULL run은 동일 run이어야 한다.
- decision과 AI request는 run symbol/cutoff와 일치해야 한다.
- AI response는 연결된 request의 run/symbol/model과 일치해야 한다.
- settlement는 decision의 run/symbol/horizon과 일치해야 한다.
- 위 관계는 insert뿐 아니라 UPDATE에서도 같은 trigger 조건으로 재검증되고, 여기에 더해 2.5절의 불변 컬럼 규칙(의미·계보 컬럼 UPDATE abort, lifecycle 컬럼만 허용)이 함께 적용된다.

애플리케이션 계층에서 추가로 해야 할 것:

1. 거래일 캘린더로 T+1/T+5/T+20 `target_trade_date`를 계산한다.
2. 정산기는 `due_after_kst <= now_kst`인 `PENDING_SETTLEMENT`만 처리한다.
3. historical backfill은 현재 최신 정정값을 과거 run에 update하지 않고, 당시 cutoff snapshot을 새 event로 남긴다.
4. 테스트 fixture에서 cutoff 이후 `source_as_of` 또는 `ingested_at_kst` row를 insert할 때 실패하는지 확인한다.

## 6. 백업·복구 정책

SQLite 파일은 영구 원장이므로 Redis snapshot과 다르게 운영 백업 대상이다.

권장 위치:
- 운영 DB: `data/k_semiconductor_flow_desk.sqlite3` 또는 production 전용 절대 경로.
- raw cache: `data/raw/` 또는 별도 gitignored 내부 object store.
- `.gitignore`의 `data/` 정책에 따라 DB와 raw cache는 Git에 커밋하지 않는다.

백업 원칙:

1. WAL 사용
   - migration은 `PRAGMA journal_mode=WAL`을 설정한다.
   - 실행 중 복사는 `.backup` API 또는 `VACUUM INTO`를 사용한다.

2. 일 단위 immutable backup
   - 예: `backups/k_semiconductor_flow_desk/YYYYMMDD.sqlite3`
   - backup artifact는 repo 밖 또는 gitignored 경로에 둔다.
   - 최소 최근 30일 + 월말본 12개월 보존을 권장한다.

3. 무결성 검사
   - 백업 전후 `PRAGMA integrity_check;`가 `ok`인지 확인한다.
   - `PRAGMA foreign_key_check;` 결과가 비어야 한다.

4. 복구 drill
   - staging/temp DB에 backup 파일을 열고 migration version, row counts, latest run view를 확인한다.
   - production 경로 교체는 별도 사용자 승인 후에만 한다.

자동화 테스트는 `sqlite3.Connection.backup()`으로 임시 파일에 round-trip한 뒤 integrity, FK, schema version, seed symbol을 다시 검증한다.

예시 백업 command:

```bash
python - <<'PY'
import sqlite3
from pathlib import Path
src = Path('data/k_semiconductor_flow_desk.sqlite3')
dst = Path('backups/k_semiconductor_flow_desk/20260801.sqlite3')
dst.parent.mkdir(parents=True, exist_ok=True)
with sqlite3.connect(src) as con:
    assert con.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    with sqlite3.connect(dst) as out:
        con.backup(out)
with sqlite3.connect(dst) as con:
    assert con.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
PY
```

## 7. 예시 query

### 7.1 최신 종목별 판단 카드

```sql
SELECT
  symbol,
  name_ko,
  trading_date,
  as_of_kst,
  available_data_cutoff,
  run_status,
  horizon_days,
  deterministic_score,
  score_label,
  user_opinion,
  settlement_status,
  target_trade_date,
  return_bps,
  direction_hit
FROM v_ksf_decision_cards
WHERE symbol = '005930'
ORDER BY as_of_kst DESC, horizon_days;
```

### 7.2 특정 run의 source 출처 점검

```sql
SELECT
  source_name,
  source_kind,
  source_status,
  source_as_of,
  ingested_at_kst,
  available_data_cutoff,
  quality_grade,
  lag_days,
  missing_reason,
  normalized_payload_sha256
FROM ksf_source_snapshots
WHERE run_id = :run_id
ORDER BY source_kind, source_name;
```

### 7.3 cutoff 위반 후보 탐지

정상 DB에서는 CHECK/trigger 때문에 결과가 없어야 한다.

```sql
SELECT 'source_snapshot' AS table_name, snapshot_id AS row_id
FROM ksf_source_snapshots
WHERE julianday(source_as_of) > julianday(available_data_cutoff)
   OR julianday(ingested_at_kst) > julianday(available_data_cutoff)
UNION ALL
SELECT 'feature', feature_id
FROM ksf_normalized_features
WHERE julianday(source_as_of) > julianday(available_data_cutoff)
   OR julianday(ingested_at_kst) > julianday(available_data_cutoff)
UNION ALL
SELECT 'decision', decision_id
FROM ksf_decisions
WHERE julianday(available_data_cutoff) > julianday(as_of_kst)
UNION ALL
SELECT 'ai_request', ai_request_id
FROM ksf_ai_requests
WHERE julianday(available_data_cutoff) > julianday(as_of_kst);
```

### 7.4 정산 due queue

```sql
SELECT
  settlement_id,
  decision_id,
  symbol,
  horizon_days,
  base_trade_date,
  target_trade_date,
  due_after_kst
FROM ksf_performance_settlements
WHERE settlement_status = 'PENDING_SETTLEMENT'
  AND due_after_kst <= :now_kst
ORDER BY due_after_kst, symbol, horizon_days;
```

### 7.5 feature contribution audit

```sql
SELECT
  f.symbol,
  f.feature_group,
  f.feature_name,
  f.feature_version,
  f.feature_status,
  f.value_num,
  f.unit,
  f.source_as_of,
  s.source_name,
  s.source_status,
  s.quality_grade
FROM ksf_normalized_features f
LEFT JOIN ksf_source_snapshots s ON s.snapshot_id = f.source_snapshot_id
WHERE f.run_id = :run_id
ORDER BY f.feature_group, f.feature_name;
```

### 7.6 AI 입력/응답 재현성 확인

```sql
SELECT
  req.ai_request_id,
  req.purpose,
  req.prompt_template_version,
  req.redaction_policy_version,
  req.input_ledger_hash_sha256,
  req.prompt_hash_sha256,
  res.response_status,
  res.response_hash_sha256,
  res.model_provider,
  res.model_name
FROM ksf_ai_requests req
LEFT JOIN ksf_ai_responses res ON res.ai_request_id = req.ai_request_id
WHERE req.run_id = :run_id
ORDER BY req.created_at_kst;
```

## 8. Migration 검증 방법

네트워크와 secret 없이 로컬에서 검증한다.

회귀 테스트(재적용, backup round-trip, metadata FK/source 일치, 절대시각, cross-symbol/cross-run 포함):

```bash
.venv/bin/pytest -q tests/test_k_semiconductor_ledger_migration.py
```

```bash
python - <<'PY'
import sqlite3
from pathlib import Path
sql = Path('migrations/001_k_semiconductor_flow_desk_permanent_ledgers.sql').read_text()
con = sqlite3.connect(':memory:')
con.executescript(sql)
print(con.execute('PRAGMA integrity_check').fetchone()[0])
print(con.execute('PRAGMA foreign_key_check').fetchall())
print(con.execute("SELECT COUNT(*) FROM ksf_symbols").fetchone()[0])
PY
```

no-look-ahead guard smoke:

```bash
python - <<'PY'
import sqlite3
from pathlib import Path
con = sqlite3.connect(':memory:')
con.executescript(Path('migrations/001_k_semiconductor_flow_desk_permanent_ledgers.sql').read_text())

# snapshot provenance row (실제 collector는 source/capture date로 deterministic ID를 생성한다).
con.execute("""
  INSERT INTO ksf_collected_source_metadata(
    metadata_id, source_name, capture_date, quality_grade, raw_storage_policy
  ) VALUES('meta_krx_price', 'krx_price', '2026-08-01', 'A', 'METADATA_ONLY')
""")

# 정상: 판단 생성 시각(as_of)이 cutoff 직후인 run은 허용된다.
con.execute("""
  INSERT INTO ksf_runs(run_id, symbol, trading_date, run_status, as_of_kst, available_data_cutoff)
  VALUES('run_ok', '005930', '2026-08-01', 'READY', '2026-08-01T16:05:00+09:00', '2026-08-01T16:00:00+09:00')
""")

# 차단: 판단 생성 시각보다 미래 cutoff를 선언할 수 없다.
try:
    con.execute("""
      INSERT INTO ksf_runs(run_id, symbol, trading_date, run_status, as_of_kst, available_data_cutoff)
      VALUES('run_bad_cutoff', '005930', '2026-08-01', 'READY', '2026-08-01T09:00:00+09:00', '2026-08-01T16:00:00+09:00')
    """)
except sqlite3.IntegrityError as e:
    print('run cutoff blocked:', e)
else:
    raise SystemExit('run cutoff guard failed')

# 차단: source_as_of가 run cutoff보다 미래면 feature 입력으로 저장할 수 없다.
try:
    con.execute("""
      INSERT INTO ksf_source_snapshots(
        snapshot_id, run_id, symbol, source_metadata_id, source_name, source_kind, source_status,
        source_as_of, ingested_at_kst, available_data_cutoff, quality_grade
      ) VALUES(
        'snap_future', 'run_ok', '005930', 'meta_krx_price', 'krx_price', 'domestic_price', 'CLOSE_CONFIRMED',
        '2026-08-01T16:01:00+09:00', '2026-08-01T15:59:00+09:00', '2026-08-01T16:00:00+09:00', 'A'
      )
    """)
except sqlite3.IntegrityError as e:
    print('source future blocked:', e)
else:
    raise SystemExit('source no-look-ahead guard failed')
PY
```

## 9. 구현자 체크리스트

- [ ] 모든 insert는 애플리케이션에서 UUID/ULID 기반 id를 생성한다.
- [ ] `PRAGMA foreign_keys=ON`을 연결마다 설정한다.
- [ ] raw external response는 원장에 직접 저장하지 않고, 필요 시 gitignored 내부 path와 hash만 저장한다.
- [ ] AI prompt 원문 전체 저장이 필요하면 별도 redaction 검증 후 내부 암호화 저장소를 사용한다. 기본 원장은 hash + preview만 저장한다.
- [ ] score 산출 전에 `ksf_normalized_features`의 cutoff 위반 query가 빈 결과인지 테스트한다.
- [ ] 정산기는 `PENDING_SETTLEMENT`만 처리하고 horizon 미도래 성과를 읽지 않는다.
- [ ] production DB 생성/교체/복구는 별도 사용자 승인 후 수행한다.

## 10. 안전 메모

이 migration과 문서는 설계 산출물이다. broker 주문 API, 계좌 mutation, systemd timer, deploy, production DB 변경은 수행하지 않는다. SQLite 원장은 리서치/알림 재현성을 위한 내부 기록이며 투자 조언이나 주문 지시가 아니다.
