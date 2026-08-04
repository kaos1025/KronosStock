-- K-Semiconductor Flow Desk permanent ledger schema
-- Task: t_b54ce593
-- Target: SQLite 3.x
-- Safety: append-only ledgers with no TTL; no broker/order payload storage.

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 5000;

BEGIN;

CREATE TABLE IF NOT EXISTS ksf_schema_versions (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    applied_at_kst TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+09:00','now','+9 hours')),
    checksum_sha256 TEXT,
    notes TEXT,
    CHECK (julianday(applied_at_kst) IS NOT NULL AND substr(applied_at_kst, -6) = '+09:00')
);

INSERT OR IGNORE INTO ksf_schema_versions (version, name, notes)
VALUES (1, '001_k_semiconductor_flow_desk_permanent_ledgers', 'Initial permanent ledger schema for source snapshots, features, decisions, AI I/O, and T+1/T+5/T+20 settlements.');

CREATE TABLE IF NOT EXISTS ksf_symbols (
    symbol TEXT PRIMARY KEY,
    name_ko TEXT NOT NULL,
    market TEXT NOT NULL CHECK (market IN ('KOSPI','KOSDAQ')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    created_at_kst TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+09:00','now','+9 hours')),
    CHECK (symbol GLOB '[0-9][0-9][0-9][0-9][0-9][0-9]'),
    CHECK (julianday(created_at_kst) IS NOT NULL AND substr(created_at_kst, -6) = '+09:00')
);

INSERT OR IGNORE INTO ksf_symbols (symbol, name_ko, market) VALUES
('005930', '삼성전자', 'KOSPI'),
('000660', 'SK하이닉스', 'KOSPI');

CREATE TABLE IF NOT EXISTS ksf_runs (
    run_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL REFERENCES ksf_symbols(symbol),
    trading_date TEXT NOT NULL,                 -- KRX session date represented by this run, YYYY-MM-DD
    run_status TEXT NOT NULL CHECK (run_status IN (
        'READY','PARTIAL_DATA','STALE_DATA','MISSING_REQUIRED_DATA',
        'SCORING_DONE','AI_SUMMARY_DONE','BLOCKED_REVIEW','FAILED','ARCHIVED'
    )),
    as_of_kst TEXT NOT NULL,                    -- judgement creation timestamp
    available_data_cutoff TEXT NOT NULL,        -- maximum timestamp allowed for all inputs
    scoring_ruleset_version TEXT,
    prompt_template_version TEXT,
    model_policy_version TEXT,
    created_at_kst TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+09:00','now','+9 hours')),
    archived_at_kst TEXT,
    archived_reason TEXT,
    CHECK (julianday(as_of_kst) IS NOT NULL AND substr(as_of_kst, -6) = '+09:00'),
    CHECK (julianday(available_data_cutoff) IS NOT NULL AND substr(available_data_cutoff, -6) = '+09:00'),
    CHECK (julianday(available_data_cutoff) <= julianday(as_of_kst)),
    CHECK (julianday(created_at_kst) IS NOT NULL AND substr(created_at_kst, -6) = '+09:00'),
    CHECK (archived_at_kst IS NULL OR (julianday(archived_at_kst) IS NOT NULL AND substr(archived_at_kst, -6) = '+09:00')),
    CHECK ((run_status = 'ARCHIVED' AND archived_at_kst IS NOT NULL) OR (run_status != 'ARCHIVED' AND archived_at_kst IS NULL))
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_ksf_runs_symbol_trading_cutoff
ON ksf_runs(symbol, trading_date, available_data_cutoff, COALESCE(scoring_ruleset_version,''), COALESCE(prompt_template_version,''));

CREATE INDEX IF NOT EXISTS ix_ksf_runs_symbol_asof ON ksf_runs(symbol, as_of_kst DESC);
CREATE INDEX IF NOT EXISTS ix_ksf_runs_status ON ksf_runs(run_status, trading_date DESC);

CREATE TABLE IF NOT EXISTS ksf_collected_source_metadata (
    metadata_id TEXT PRIMARY KEY,
    source_name TEXT NOT NULL,
    endpoint_template TEXT,
    method TEXT NOT NULL DEFAULT 'GET' CHECK (method IN ('GET','POST_READONLY','FILE_READONLY','MANUAL_READONLY')),
    source_url TEXT,
    terms_url TEXT,
    license_scope TEXT NOT NULL DEFAULT 'unverified',
    rate_limit_policy TEXT,
    user_agent_policy TEXT,
    capture_date TEXT NOT NULL,
    quality_grade TEXT NOT NULL CHECK (quality_grade IN ('A','B','C','D','BLOCKED')),
    raw_storage_policy TEXT NOT NULL CHECK (raw_storage_policy IN ('NONE','GITIGNORED_INTERNAL_ONLY','METADATA_ONLY')),
    redistribution_policy TEXT NOT NULL DEFAULT 'no_raw_redistribution_without_license',
    notes TEXT,
    created_at_kst TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+09:00','now','+9 hours')),
    CHECK (julianday(created_at_kst) IS NOT NULL AND substr(created_at_kst, -6) = '+09:00')
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_ksf_source_metadata_dedupe
ON ksf_collected_source_metadata(source_name, capture_date, COALESCE(source_url,''), COALESCE(terms_url,''));

CREATE INDEX IF NOT EXISTS ix_ksf_source_metadata_source ON ksf_collected_source_metadata(source_name, capture_date DESC);

CREATE TABLE IF NOT EXISTS ksf_source_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    run_id TEXT REFERENCES ksf_runs(run_id) ON DELETE RESTRICT,
    symbol TEXT REFERENCES ksf_symbols(symbol),              -- NULL allowed for global/common sources
    source_metadata_id TEXT NOT NULL REFERENCES ksf_collected_source_metadata(metadata_id) ON DELETE RESTRICT,
    source_name TEXT NOT NULL,
    source_kind TEXT NOT NULL CHECK (source_kind IN (
        'domestic_price','domestic_investor_flow','global_price','global_investor_flow',
        'fx','memory_license_review','ai_input_context','manual_review'
    )),
    source_status TEXT NOT NULL CHECK (source_status IN ('INTRADAY_ESTIMATE','CLOSE_CONFIRMED','MISSING','STALE','BLOCKED_REVIEW')),
    source_as_of TEXT NOT NULL,
    ingested_at_kst TEXT NOT NULL,
    available_data_cutoff TEXT NOT NULL,
    quality_grade TEXT NOT NULL CHECK (quality_grade IN ('A','B','C','D','BLOCKED')),
    lag_days INTEGER NOT NULL DEFAULT 0 CHECK (lag_days >= 0),
    missing_reason TEXT,
    raw_ref_uri TEXT,                         -- gitignored local path/object URI only; never tokenized URL
    raw_ref_sha256 TEXT,                      -- hash of raw object if stored internally
    normalized_payload_sha256 TEXT,           -- deterministic hash of normalized rows used by features
    snapshot_metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at_kst TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+09:00','now','+9 hours')),
    CHECK (julianday(source_as_of) IS NOT NULL),
    CHECK (julianday(ingested_at_kst) IS NOT NULL AND substr(ingested_at_kst, -6) = '+09:00'),
    CHECK (julianday(available_data_cutoff) IS NOT NULL AND substr(available_data_cutoff, -6) = '+09:00'),
    CHECK (julianday(created_at_kst) IS NOT NULL AND substr(created_at_kst, -6) = '+09:00'),
    CHECK (julianday(source_as_of) <= julianday(available_data_cutoff)),
    CHECK (julianday(ingested_at_kst) <= julianday(available_data_cutoff)),
    CHECK ((source_status != 'MISSING') OR missing_reason IS NOT NULL),
    CHECK ((source_status != 'BLOCKED_REVIEW') OR quality_grade = 'BLOCKED')
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_ksf_source_snapshots_dedupe
ON ksf_source_snapshots(
    COALESCE(run_id,''), COALESCE(symbol,''), source_name, source_kind,
    source_as_of, available_data_cutoff, COALESCE(normalized_payload_sha256,''), COALESCE(raw_ref_sha256,'')
);

CREATE INDEX IF NOT EXISTS ix_ksf_source_snapshots_run ON ksf_source_snapshots(run_id, source_name);
CREATE INDEX IF NOT EXISTS ix_ksf_source_snapshots_symbol_source ON ksf_source_snapshots(symbol, source_name, source_as_of DESC);
CREATE INDEX IF NOT EXISTS ix_ksf_source_snapshots_cutoff ON ksf_source_snapshots(available_data_cutoff, ingested_at_kst);

CREATE TABLE IF NOT EXISTS ksf_normalized_features (
    feature_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES ksf_runs(run_id) ON DELETE RESTRICT,
    symbol TEXT NOT NULL REFERENCES ksf_symbols(symbol),
    source_snapshot_id TEXT REFERENCES ksf_source_snapshots(snapshot_id) ON DELETE RESTRICT,
    feature_group TEXT NOT NULL,
    feature_name TEXT NOT NULL,
    feature_version TEXT NOT NULL,
    feature_status TEXT NOT NULL CHECK (feature_status IN ('READY','MISSING_OPTIONAL','MISSING_REQUIRED','STALE','LICENSE_BLOCKED')),
    value_num REAL,
    value_text TEXT,
    value_json TEXT,
    unit TEXT,
    source_as_of TEXT NOT NULL,
    ingested_at_kst TEXT NOT NULL,
    available_data_cutoff TEXT NOT NULL,
    contribution_cap_bps INTEGER NOT NULL DEFAULT 10000 CHECK (contribution_cap_bps BETWEEN 0 AND 10000),
    missing_reason TEXT,
    created_at_kst TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+09:00','now','+9 hours')),
    CHECK (julianday(source_as_of) IS NOT NULL),
    CHECK (julianday(ingested_at_kst) IS NOT NULL AND substr(ingested_at_kst, -6) = '+09:00'),
    CHECK (julianday(available_data_cutoff) IS NOT NULL AND substr(available_data_cutoff, -6) = '+09:00'),
    CHECK (julianday(created_at_kst) IS NOT NULL AND substr(created_at_kst, -6) = '+09:00'),
    CHECK (julianday(source_as_of) <= julianday(available_data_cutoff)),
    CHECK (julianday(ingested_at_kst) <= julianday(available_data_cutoff)),
    CHECK ((feature_status NOT IN ('MISSING_OPTIONAL','MISSING_REQUIRED','LICENSE_BLOCKED')) OR missing_reason IS NOT NULL),
    CHECK (value_num IS NOT NULL OR value_text IS NOT NULL OR value_json IS NOT NULL OR feature_status != 'READY')
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_ksf_features_run_symbol_name_version
ON ksf_normalized_features(run_id, symbol, feature_group, feature_name, feature_version);

CREATE INDEX IF NOT EXISTS ix_ksf_features_run_group ON ksf_normalized_features(run_id, feature_group);
CREATE INDEX IF NOT EXISTS ix_ksf_features_symbol_name ON ksf_normalized_features(symbol, feature_name, source_as_of DESC);
CREATE INDEX IF NOT EXISTS ix_ksf_features_snapshot ON ksf_normalized_features(source_snapshot_id);

CREATE TABLE IF NOT EXISTS ksf_decisions (
    decision_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES ksf_runs(run_id) ON DELETE RESTRICT,
    symbol TEXT NOT NULL REFERENCES ksf_symbols(symbol),
    horizon_days INTEGER NOT NULL CHECK (horizon_days IN (1,5,20)),
    as_of_kst TEXT NOT NULL,
    available_data_cutoff TEXT NOT NULL,
    deterministic_score REAL CHECK (deterministic_score BETWEEN -100 AND 100),
    score_label TEXT NOT NULL CHECK (score_label IN ('positive_watch','neutral_watch','risk_watch','insufficient_data')),
    user_opinion TEXT NOT NULL CHECK (user_opinion IN ('POSITIVE','ACCUMULATION','WATCH','NEUTRAL','CAUTION','DISTRIBUTION','INSUFFICIENT_DATA')),
    scoring_ruleset_version TEXT NOT NULL,
    feature_snapshot_sha256 TEXT NOT NULL,
    feature_contributions_json TEXT NOT NULL DEFAULT '[]',
    rationale_json TEXT NOT NULL DEFAULT '{}',
    created_at_kst TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+09:00','now','+9 hours')),
    CHECK (julianday(as_of_kst) IS NOT NULL AND substr(as_of_kst, -6) = '+09:00'),
    CHECK (julianday(available_data_cutoff) IS NOT NULL AND substr(available_data_cutoff, -6) = '+09:00'),
    CHECK (julianday(available_data_cutoff) <= julianday(as_of_kst)),
    CHECK (julianday(created_at_kst) IS NOT NULL AND substr(created_at_kst, -6) = '+09:00')
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_ksf_decisions_run_horizon ON ksf_decisions(run_id, horizon_days);
CREATE INDEX IF NOT EXISTS ix_ksf_decisions_symbol_asof ON ksf_decisions(symbol, as_of_kst DESC, horizon_days);
CREATE INDEX IF NOT EXISTS ix_ksf_decisions_label ON ksf_decisions(score_label, user_opinion);

CREATE TABLE IF NOT EXISTS ksf_ai_requests (
    ai_request_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES ksf_runs(run_id) ON DELETE RESTRICT,
    symbol TEXT NOT NULL REFERENCES ksf_symbols(symbol),
    purpose TEXT NOT NULL CHECK (purpose IN ('explain_decision','risk_summary','data_gap_summary')),
    as_of_kst TEXT NOT NULL,
    available_data_cutoff TEXT NOT NULL,
    prompt_template_version TEXT NOT NULL,
    redaction_policy_version TEXT NOT NULL,
    input_ledger_hash_sha256 TEXT NOT NULL,
    prompt_hash_sha256 TEXT NOT NULL,
    prompt_redacted_preview TEXT,       -- short, secret-free preview only
    model_provider TEXT NOT NULL,
    model_name TEXT NOT NULL,
    request_metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at_kst TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+09:00','now','+9 hours')),
    CHECK (julianday(as_of_kst) IS NOT NULL AND substr(as_of_kst, -6) = '+09:00'),
    CHECK (julianday(available_data_cutoff) IS NOT NULL AND substr(available_data_cutoff, -6) = '+09:00'),
    CHECK (julianday(available_data_cutoff) <= julianday(as_of_kst)),
    CHECK (julianday(created_at_kst) IS NOT NULL AND substr(created_at_kst, -6) = '+09:00')
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_ksf_ai_requests_dedupe
ON ksf_ai_requests(run_id, purpose, prompt_template_version, input_ledger_hash_sha256, prompt_hash_sha256, model_provider, model_name);
CREATE INDEX IF NOT EXISTS ix_ksf_ai_requests_symbol_asof ON ksf_ai_requests(symbol, as_of_kst DESC);

CREATE TABLE IF NOT EXISTS ksf_ai_responses (
    ai_response_id TEXT PRIMARY KEY,
    ai_request_id TEXT NOT NULL REFERENCES ksf_ai_requests(ai_request_id) ON DELETE RESTRICT,
    run_id TEXT NOT NULL REFERENCES ksf_runs(run_id) ON DELETE RESTRICT,
    symbol TEXT NOT NULL REFERENCES ksf_symbols(symbol),
    response_status TEXT NOT NULL CHECK (response_status IN ('OK','REFUSED','FAILED','BLOCKED_REVIEW')),
    response_hash_sha256 TEXT NOT NULL,
    summary TEXT,
    drivers_json TEXT NOT NULL DEFAULT '[]',
    risks_json TEXT NOT NULL DEFAULT '[]',
    data_gaps_json TEXT NOT NULL DEFAULT '[]',
    confidence_note TEXT,
    not_investment_advice TEXT NOT NULL DEFAULT '개인용 리서치 보조 정보이며 투자 조언이나 주문 지시가 아닙니다.',
    model_provider TEXT NOT NULL,
    model_name TEXT NOT NULL,
    response_metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at_kst TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+09:00','now','+9 hours')),
    CHECK (julianday(created_at_kst) IS NOT NULL AND substr(created_at_kst, -6) = '+09:00')
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_ksf_ai_responses_request_hash ON ksf_ai_responses(ai_request_id, response_hash_sha256);
CREATE INDEX IF NOT EXISTS ix_ksf_ai_responses_run ON ksf_ai_responses(run_id, response_status);

CREATE TABLE IF NOT EXISTS ksf_performance_settlements (
    settlement_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL REFERENCES ksf_decisions(decision_id) ON DELETE RESTRICT,
    run_id TEXT NOT NULL REFERENCES ksf_runs(run_id) ON DELETE RESTRICT,
    symbol TEXT NOT NULL REFERENCES ksf_symbols(symbol),
    horizon_days INTEGER NOT NULL CHECK (horizon_days IN (1,5,20)),
    base_trade_date TEXT NOT NULL,
    target_trade_date TEXT,
    settlement_status TEXT NOT NULL CHECK (settlement_status IN ('PENDING_SETTLEMENT','SETTLED','MISSING_PRICE','CALENDAR_BLOCKED','FAILED')),
    base_close REAL,
    target_close REAL,
    return_bps REAL,
    benchmark_return_bps REAL,
    direction_hit INTEGER CHECK (direction_hit IN (0,1)),
    kospi_return_bps REAL,
    kospi_excess_return_bps REAL,
    industry_return_bps REAL,
    industry_excess_return_bps REAL,
    status_direction_hit INTEGER CHECK (status_direction_hit IN (0,1)),
    opinion_direction_hit INTEGER CHECK (opinion_direction_hit IN (0,1)),
    max_drawdown_bps REAL,
    turnover_assumption REAL CHECK (turnover_assumption IS NULL OR turnover_assumption >= 0),
    transaction_cost_bps REAL CHECK (transaction_cost_bps IS NULL OR transaction_cost_bps >= 0),
    net_return_bps REAL,
    settlement_cutoff_kst TEXT,
    settlement_source_snapshot_id TEXT REFERENCES ksf_source_snapshots(snapshot_id) ON DELETE RESTRICT,
    due_after_kst TEXT NOT NULL,
    settled_at_kst TEXT,
    missing_reason TEXT,
    created_at_kst TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+09:00','now','+9 hours')),
    CHECK (target_trade_date IS NULL OR target_trade_date > base_trade_date),
    CHECK (settlement_status != 'CALENDAR_BLOCKED' OR target_trade_date IS NULL),
    CHECK ((settlement_status = 'PENDING_SETTLEMENT' AND target_close IS NULL AND return_bps IS NULL AND settled_at_kst IS NULL)
        OR (settlement_status != 'PENDING_SETTLEMENT')),
    CHECK ((settlement_status = 'SETTLED' AND base_close IS NOT NULL AND target_close IS NOT NULL AND return_bps IS NOT NULL AND settled_at_kst IS NOT NULL)
        OR (settlement_status != 'SETTLED')),
    CHECK ((settlement_status NOT IN ('MISSING_PRICE','CALENDAR_BLOCKED','FAILED')) OR missing_reason IS NOT NULL),
    CHECK (julianday(due_after_kst) IS NOT NULL AND substr(due_after_kst, -6) = '+09:00'),
    CHECK (settled_at_kst IS NULL OR (julianday(settled_at_kst) IS NOT NULL AND substr(settled_at_kst, -6) = '+09:00')),
    CHECK (settlement_cutoff_kst IS NULL OR (julianday(settlement_cutoff_kst) IS NOT NULL AND substr(settlement_cutoff_kst, -6) = '+09:00')),
    CHECK (julianday(created_at_kst) IS NOT NULL AND substr(created_at_kst, -6) = '+09:00')
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_ksf_settlements_decision_horizon ON ksf_performance_settlements(decision_id, horizon_days);
CREATE INDEX IF NOT EXISTS ix_ksf_settlements_due ON ksf_performance_settlements(settlement_status, due_after_kst);
CREATE INDEX IF NOT EXISTS ix_ksf_settlements_symbol_target ON ksf_performance_settlements(symbol, target_trade_date, horizon_days);

CREATE TRIGGER IF NOT EXISTS trg_ksf_runs_no_update
BEFORE UPDATE ON ksf_runs
FOR EACH ROW
WHEN OLD.run_status = 'ARCHIVED'
BEGIN
    SELECT RAISE(ABORT, 'archived run is immutable');
END;

-- Runs are the lineage root: identity columns can never be rewritten (status lifecycle stays open).
CREATE TRIGGER IF NOT EXISTS trg_ksf_runs_identity_immutable
BEFORE UPDATE OF run_id, symbol, trading_date, available_data_cutoff ON ksf_runs
FOR EACH ROW
WHEN OLD.run_id != NEW.run_id
  OR OLD.symbol != NEW.symbol
  OR OLD.trading_date != NEW.trading_date
  OR OLD.available_data_cutoff != NEW.available_data_cutoff
BEGIN
    SELECT RAISE(ABORT, 'run identity/lineage columns are immutable');
END;

-- Reproducibility provenance of a run (judgement time + ruleset/prompt/model versions) is immutable;
-- run_status and the ARCHIVED lifecycle stay open.
CREATE TRIGGER IF NOT EXISTS trg_ksf_runs_provenance_immutable
BEFORE UPDATE OF as_of_kst, scoring_ruleset_version, prompt_template_version, model_policy_version, created_at_kst ON ksf_runs
FOR EACH ROW
WHEN OLD.as_of_kst IS NOT NEW.as_of_kst
  OR OLD.scoring_ruleset_version IS NOT NEW.scoring_ruleset_version
  OR OLD.prompt_template_version IS NOT NEW.prompt_template_version
  OR OLD.model_policy_version IS NOT NEW.model_policy_version
  OR OLD.created_at_kst IS NOT NEW.created_at_kst
BEGIN
    SELECT RAISE(ABORT, 'run provenance columns are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_ksf_source_snapshot_matches_run_cutoff
BEFORE INSERT ON ksf_source_snapshots
FOR EACH ROW
WHEN NEW.run_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM ksf_runs r
    WHERE r.run_id = NEW.run_id
      AND r.available_data_cutoff = NEW.available_data_cutoff
      AND (NEW.symbol IS NULL OR r.symbol = NEW.symbol)
)
BEGIN
    SELECT RAISE(ABORT, 'source snapshot cutoff/symbol must match run');
END;

CREATE TRIGGER IF NOT EXISTS trg_ksf_source_snapshot_matches_run_cutoff_update
BEFORE UPDATE OF run_id, symbol, available_data_cutoff ON ksf_source_snapshots
FOR EACH ROW
WHEN NEW.run_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM ksf_runs r
    WHERE r.run_id = NEW.run_id
      AND r.available_data_cutoff = NEW.available_data_cutoff
      AND (NEW.symbol IS NULL OR r.symbol = NEW.symbol)
)
BEGIN
    SELECT RAISE(ABORT, 'source snapshot cutoff/symbol must match run');
END;

-- Parent-side guard: a snapshot update must not invalidate features already derived from it.
CREATE TRIGGER IF NOT EXISTS trg_ksf_source_snapshot_update_preserves_feature_lineage
BEFORE UPDATE OF run_id, symbol, available_data_cutoff, source_as_of, ingested_at_kst ON ksf_source_snapshots
FOR EACH ROW
WHEN EXISTS (
    SELECT 1 FROM ksf_normalized_features f
    WHERE f.source_snapshot_id = OLD.snapshot_id
      AND NOT (
        (NEW.symbol IS NULL OR NEW.symbol = f.symbol)
        AND (NEW.run_id IS NULL OR NEW.run_id = f.run_id)
        AND julianday(NEW.available_data_cutoff) <= julianday(f.available_data_cutoff)
        AND julianday(NEW.source_as_of) <= julianday(f.available_data_cutoff)
        AND julianday(NEW.ingested_at_kst) <= julianday(f.available_data_cutoff)
      )
)
BEGIN
    SELECT RAISE(ABORT, 'snapshot update would break normalized feature lineage');
END;

-- Snapshot identity, parent linkage (no NULL detach/parent swap), source identity, timestamps/cutoff
-- and payload hashes/metadata are immutable; source_status/quality_grade/lag_days/missing_reason
-- remain a legitimate lifecycle.
CREATE TRIGGER IF NOT EXISTS trg_ksf_source_snapshots_provenance_immutable
BEFORE UPDATE OF snapshot_id, run_id, symbol, source_metadata_id, source_name, source_kind,
                 source_as_of, ingested_at_kst, available_data_cutoff,
                 raw_ref_uri, raw_ref_sha256, normalized_payload_sha256, snapshot_metadata_json,
                 created_at_kst ON ksf_source_snapshots
FOR EACH ROW
WHEN OLD.snapshot_id IS NOT NEW.snapshot_id
  OR OLD.run_id IS NOT NEW.run_id
  OR OLD.symbol IS NOT NEW.symbol
  OR OLD.source_metadata_id IS NOT NEW.source_metadata_id
  OR OLD.source_name IS NOT NEW.source_name
  OR OLD.source_kind IS NOT NEW.source_kind
  OR OLD.source_as_of IS NOT NEW.source_as_of
  OR OLD.ingested_at_kst IS NOT NEW.ingested_at_kst
  OR OLD.available_data_cutoff IS NOT NEW.available_data_cutoff
  OR OLD.raw_ref_uri IS NOT NEW.raw_ref_uri
  OR OLD.raw_ref_sha256 IS NOT NEW.raw_ref_sha256
  OR OLD.normalized_payload_sha256 IS NOT NEW.normalized_payload_sha256
  OR OLD.snapshot_metadata_json IS NOT NEW.snapshot_metadata_json
  OR OLD.created_at_kst IS NOT NEW.created_at_kst
BEGIN
    SELECT RAISE(ABORT, 'snapshot identity/provenance columns are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_ksf_source_snapshot_matches_metadata
BEFORE INSERT ON ksf_source_snapshots
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1 FROM ksf_collected_source_metadata m
    WHERE m.metadata_id = NEW.source_metadata_id
      AND m.source_name = NEW.source_name
)
BEGIN
    SELECT RAISE(ABORT, 'snapshot metadata source_name must match snapshot source_name');
END;

CREATE TRIGGER IF NOT EXISTS trg_ksf_source_snapshot_matches_metadata_update
BEFORE UPDATE OF source_metadata_id, source_name ON ksf_source_snapshots
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1 FROM ksf_collected_source_metadata m
    WHERE m.metadata_id = NEW.source_metadata_id
      AND m.source_name = NEW.source_name
)
BEGIN
    SELECT RAISE(ABORT, 'snapshot metadata source_name must match snapshot source_name');
END;

CREATE TRIGGER IF NOT EXISTS trg_ksf_feature_matches_run_cutoff
BEFORE INSERT ON ksf_normalized_features
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1 FROM ksf_runs r
    WHERE r.run_id = NEW.run_id
      AND r.symbol = NEW.symbol
      AND r.available_data_cutoff = NEW.available_data_cutoff
)
BEGIN
    SELECT RAISE(ABORT, 'feature cutoff/symbol must match run');
END;

CREATE TRIGGER IF NOT EXISTS trg_ksf_feature_matches_run_cutoff_update
BEFORE UPDATE OF run_id, symbol, available_data_cutoff ON ksf_normalized_features
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1 FROM ksf_runs r
    WHERE r.run_id = NEW.run_id
      AND r.symbol = NEW.symbol
      AND r.available_data_cutoff = NEW.available_data_cutoff
)
BEGIN
    SELECT RAISE(ABORT, 'feature cutoff/symbol must match run');
END;

CREATE TRIGGER IF NOT EXISTS trg_ksf_feature_snapshot_cutoff_guard
BEFORE INSERT ON ksf_normalized_features
FOR EACH ROW
WHEN NEW.source_snapshot_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM ksf_source_snapshots s
    WHERE s.snapshot_id = NEW.source_snapshot_id
      AND (s.symbol IS NULL OR s.symbol = NEW.symbol)
      AND (s.run_id IS NULL OR s.run_id = NEW.run_id)
      AND julianday(s.available_data_cutoff) <= julianday(NEW.available_data_cutoff)
      AND julianday(s.source_as_of) <= julianday(NEW.available_data_cutoff)
      AND julianday(s.ingested_at_kst) <= julianday(NEW.available_data_cutoff)
)
BEGIN
    SELECT RAISE(ABORT, 'feature snapshot lineage or available_data_cutoff violation');
END;

CREATE TRIGGER IF NOT EXISTS trg_ksf_feature_snapshot_cutoff_guard_update
BEFORE UPDATE OF source_snapshot_id, run_id, symbol, available_data_cutoff ON ksf_normalized_features
FOR EACH ROW
WHEN NEW.source_snapshot_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM ksf_source_snapshots s
    WHERE s.snapshot_id = NEW.source_snapshot_id
      AND (s.symbol IS NULL OR s.symbol = NEW.symbol)
      AND (s.run_id IS NULL OR s.run_id = NEW.run_id)
      AND julianday(s.available_data_cutoff) <= julianday(NEW.available_data_cutoff)
      AND julianday(s.source_as_of) <= julianday(NEW.available_data_cutoff)
      AND julianday(s.ingested_at_kst) <= julianday(NEW.available_data_cutoff)
)
BEGIN
    SELECT RAISE(ABORT, 'feature snapshot lineage or available_data_cutoff violation');
END;

-- Feature identity, lineage (no NULL detach/parent swap), versioned value payload, timestamps/cutoff
-- and contribution cap are immutable; feature_status/missing_reason remain a legitimate lifecycle.
CREATE TRIGGER IF NOT EXISTS trg_ksf_features_provenance_immutable
BEFORE UPDATE OF feature_id, run_id, symbol, source_snapshot_id, feature_group, feature_name,
                 feature_version, value_num, value_text, value_json, unit,
                 source_as_of, ingested_at_kst, available_data_cutoff, contribution_cap_bps,
                 created_at_kst ON ksf_normalized_features
FOR EACH ROW
WHEN OLD.feature_id IS NOT NEW.feature_id
  OR OLD.run_id IS NOT NEW.run_id
  OR OLD.symbol IS NOT NEW.symbol
  OR OLD.source_snapshot_id IS NOT NEW.source_snapshot_id
  OR OLD.feature_group IS NOT NEW.feature_group
  OR OLD.feature_name IS NOT NEW.feature_name
  OR OLD.feature_version IS NOT NEW.feature_version
  OR OLD.value_num IS NOT NEW.value_num
  OR OLD.value_text IS NOT NEW.value_text
  OR OLD.value_json IS NOT NEW.value_json
  OR OLD.unit IS NOT NEW.unit
  OR OLD.source_as_of IS NOT NEW.source_as_of
  OR OLD.ingested_at_kst IS NOT NEW.ingested_at_kst
  OR OLD.available_data_cutoff IS NOT NEW.available_data_cutoff
  OR OLD.contribution_cap_bps IS NOT NEW.contribution_cap_bps
  OR OLD.created_at_kst IS NOT NEW.created_at_kst
BEGIN
    SELECT RAISE(ABORT, 'feature identity/provenance columns are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_ksf_decision_matches_run_cutoff
BEFORE INSERT ON ksf_decisions
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1 FROM ksf_runs r
    WHERE r.run_id = NEW.run_id
      AND r.symbol = NEW.symbol
      AND r.available_data_cutoff = NEW.available_data_cutoff
)
BEGIN
    SELECT RAISE(ABORT, 'decision cutoff/symbol must match run');
END;

CREATE TRIGGER IF NOT EXISTS trg_ksf_decision_matches_run_cutoff_update
BEFORE UPDATE OF run_id, symbol, available_data_cutoff ON ksf_decisions
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1 FROM ksf_runs r
    WHERE r.run_id = NEW.run_id
      AND r.symbol = NEW.symbol
      AND r.available_data_cutoff = NEW.available_data_cutoff
)
BEGIN
    SELECT RAISE(ABORT, 'decision cutoff/symbol must match run');
END;

-- Parent-side guard: decision lineage keys cannot drift away from settlements already derived from it.
CREATE TRIGGER IF NOT EXISTS trg_ksf_decision_update_preserves_settlement_lineage
BEFORE UPDATE OF decision_id, run_id, symbol, horizon_days ON ksf_decisions
FOR EACH ROW
WHEN EXISTS (
    SELECT 1 FROM ksf_performance_settlements ps
    WHERE ps.decision_id = OLD.decision_id
      AND (ps.run_id != NEW.run_id OR ps.symbol != NEW.symbol OR ps.horizon_days != NEW.horizon_days)
)
BEGIN
    SELECT RAISE(ABORT, 'decision update would break settlement lineage');
END;

-- Decisions are pure deterministic judgements: no lifecycle columns exist, so the whole row is
-- append-only. Corrections must create a new run/decision.
CREATE TRIGGER IF NOT EXISTS trg_ksf_decisions_append_only
BEFORE UPDATE ON ksf_decisions
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'decision rows are append-only; corrections require a new run/decision');
END;

CREATE TRIGGER IF NOT EXISTS trg_ksf_ai_request_matches_run_cutoff
BEFORE INSERT ON ksf_ai_requests
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1 FROM ksf_runs r
    WHERE r.run_id = NEW.run_id
      AND r.symbol = NEW.symbol
      AND r.available_data_cutoff = NEW.available_data_cutoff
)
BEGIN
    SELECT RAISE(ABORT, 'AI request cutoff/symbol must match run');
END;

CREATE TRIGGER IF NOT EXISTS trg_ksf_ai_request_matches_run_cutoff_update
BEFORE UPDATE OF run_id, symbol, available_data_cutoff ON ksf_ai_requests
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1 FROM ksf_runs r
    WHERE r.run_id = NEW.run_id
      AND r.symbol = NEW.symbol
      AND r.available_data_cutoff = NEW.available_data_cutoff
)
BEGIN
    SELECT RAISE(ABORT, 'AI request cutoff/symbol must match run');
END;

-- Parent-side guard: request lineage keys cannot drift away from responses already stored for it.
CREATE TRIGGER IF NOT EXISTS trg_ksf_ai_request_update_preserves_response_lineage
BEFORE UPDATE OF ai_request_id, run_id, symbol, model_provider, model_name ON ksf_ai_requests
FOR EACH ROW
WHEN EXISTS (
    SELECT 1 FROM ksf_ai_responses resp
    WHERE resp.ai_request_id = OLD.ai_request_id
      AND (resp.run_id != NEW.run_id OR resp.symbol != NEW.symbol
           OR resp.model_provider != NEW.model_provider OR resp.model_name != NEW.model_name)
)
BEGIN
    SELECT RAISE(ABORT, 'AI request update would break response lineage');
END;

-- AI requests are hashed prompts/inputs: any in-place edit would diverge from the recorded hashes.
CREATE TRIGGER IF NOT EXISTS trg_ksf_ai_requests_append_only
BEFORE UPDATE ON ksf_ai_requests
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'AI request rows are append-only; corrections require a new request');
END;

CREATE TRIGGER IF NOT EXISTS trg_ksf_ai_response_matches_request
BEFORE INSERT ON ksf_ai_responses
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1 FROM ksf_ai_requests req
    WHERE req.ai_request_id = NEW.ai_request_id
      AND req.run_id = NEW.run_id
      AND req.symbol = NEW.symbol
      AND req.model_provider = NEW.model_provider
      AND req.model_name = NEW.model_name
)
BEGIN
    SELECT RAISE(ABORT, 'AI response must match request run/symbol/model');
END;

CREATE TRIGGER IF NOT EXISTS trg_ksf_ai_response_matches_request_update
BEFORE UPDATE OF ai_request_id, run_id, symbol, model_provider, model_name ON ksf_ai_responses
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1 FROM ksf_ai_requests req
    WHERE req.ai_request_id = NEW.ai_request_id
      AND req.run_id = NEW.run_id
      AND req.symbol = NEW.symbol
      AND req.model_provider = NEW.model_provider
      AND req.model_name = NEW.model_name
)
BEGIN
    SELECT RAISE(ABORT, 'AI response must match request run/symbol/model');
END;

-- AI responses are hashed model output: any in-place edit would diverge from the recorded hashes.
CREATE TRIGGER IF NOT EXISTS trg_ksf_ai_responses_append_only
BEFORE UPDATE ON ksf_ai_responses
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'AI response rows are append-only; corrections require a new response');
END;

CREATE TRIGGER IF NOT EXISTS trg_ksf_settlement_matches_decision
BEFORE INSERT ON ksf_performance_settlements
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1 FROM ksf_decisions d
    WHERE d.decision_id = NEW.decision_id
      AND d.run_id = NEW.run_id
      AND d.symbol = NEW.symbol
      AND d.horizon_days = NEW.horizon_days
)
BEGIN
    SELECT RAISE(ABORT, 'settlement must match decision run/symbol/horizon');
END;

CREATE TRIGGER IF NOT EXISTS trg_ksf_settlement_matches_decision_update
BEFORE UPDATE OF decision_id, run_id, symbol, horizon_days ON ksf_performance_settlements
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1 FROM ksf_decisions d
    WHERE d.decision_id = NEW.decision_id
      AND d.run_id = NEW.run_id
      AND d.symbol = NEW.symbol
      AND d.horizon_days = NEW.horizon_days
)
BEGIN
    SELECT RAISE(ABORT, 'settlement must match decision run/symbol/horizon');
END;

-- Settlement identity, decision linkage (no parent swap), baseline date, due timestamp and creation
-- time are immutable; the PENDING_SETTLEMENT -> result lifecycle (target/status/prices/returns/
-- benchmarks/hits/costs/cutoff/source snapshot/settled timestamp/missing reason) stays open.
CREATE TRIGGER IF NOT EXISTS trg_ksf_settlements_identity_immutable
BEFORE UPDATE OF settlement_id, decision_id, run_id, symbol, horizon_days, base_trade_date,
                 due_after_kst, created_at_kst ON ksf_performance_settlements
FOR EACH ROW
WHEN OLD.settlement_id IS NOT NEW.settlement_id
  OR OLD.decision_id IS NOT NEW.decision_id
  OR OLD.run_id IS NOT NEW.run_id
  OR OLD.symbol IS NOT NEW.symbol
  OR OLD.horizon_days IS NOT NEW.horizon_days
  OR OLD.base_trade_date IS NOT NEW.base_trade_date
  OR OLD.due_after_kst IS NOT NEW.due_after_kst
  OR OLD.created_at_kst IS NOT NEW.created_at_kst
BEGIN
    SELECT RAISE(ABORT, 'settlement identity/lineage columns are immutable');
END;

CREATE VIEW IF NOT EXISTS v_ksf_latest_symbol_runs AS
SELECT r.*
FROM ksf_runs r
JOIN (
    SELECT symbol, MAX(as_of_kst) AS max_as_of_kst
    FROM ksf_runs
    WHERE run_status != 'ARCHIVED'
    GROUP BY symbol
) latest
  ON latest.symbol = r.symbol AND latest.max_as_of_kst = r.as_of_kst;

CREATE VIEW IF NOT EXISTS v_ksf_decision_cards AS
SELECT
    r.symbol,
    sym.name_ko,
    r.trading_date,
    r.as_of_kst,
    r.available_data_cutoff,
    r.run_status,
    d.horizon_days,
    d.deterministic_score,
    d.score_label,
    d.user_opinion,
    d.scoring_ruleset_version,
    ps.settlement_status,
    ps.target_trade_date,
    ps.return_bps,
    ps.direction_hit
FROM ksf_runs r
JOIN ksf_symbols sym ON sym.symbol = r.symbol
LEFT JOIN ksf_decisions d ON d.run_id = r.run_id
LEFT JOIN ksf_performance_settlements ps ON ps.decision_id = d.decision_id AND ps.horizon_days = d.horizon_days;

COMMIT;
