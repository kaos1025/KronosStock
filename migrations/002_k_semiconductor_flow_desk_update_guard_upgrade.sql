-- K-Semiconductor Flow Desk update-guard upgrade
-- Purpose: replace UPDATE/immutability guard triggers on EXISTING persistent ledgers.
-- 001 creates triggers with CREATE TRIGGER IF NOT EXISTS, so a DB that predates the
-- strict remediation keeps older weak same-name definitions when 001 is rerun.
-- This migration explicitly DROPs every UPDATE guard and recreates the current strict
-- definition, transactionally and idempotently. Fresh DBs are unaffected semantically.
-- Target: SQLite 3.x

PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

BEGIN;

INSERT OR IGNORE INTO ksf_schema_versions (version, name, notes)
VALUES (2, '002_k_semiconductor_flow_desk_update_guard_upgrade', 'Drop and recreate all UPDATE/immutability guard triggers so existing DBs receive the strict remediation definitions.');

DROP TRIGGER IF EXISTS trg_ksf_runs_no_update;
CREATE TRIGGER trg_ksf_runs_no_update
BEFORE UPDATE ON ksf_runs
FOR EACH ROW
WHEN OLD.run_status = 'ARCHIVED'
BEGIN
    SELECT RAISE(ABORT, 'archived run is immutable');
END;

DROP TRIGGER IF EXISTS trg_ksf_runs_identity_immutable;
CREATE TRIGGER trg_ksf_runs_identity_immutable
BEFORE UPDATE OF run_id, symbol, trading_date, available_data_cutoff ON ksf_runs
FOR EACH ROW
WHEN OLD.run_id != NEW.run_id
  OR OLD.symbol != NEW.symbol
  OR OLD.trading_date != NEW.trading_date
  OR OLD.available_data_cutoff != NEW.available_data_cutoff
BEGIN
    SELECT RAISE(ABORT, 'run identity/lineage columns are immutable');
END;

DROP TRIGGER IF EXISTS trg_ksf_runs_provenance_immutable;
CREATE TRIGGER trg_ksf_runs_provenance_immutable
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

DROP TRIGGER IF EXISTS trg_ksf_source_snapshot_matches_run_cutoff_update;
CREATE TRIGGER trg_ksf_source_snapshot_matches_run_cutoff_update
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

DROP TRIGGER IF EXISTS trg_ksf_source_snapshot_update_preserves_feature_lineage;
CREATE TRIGGER trg_ksf_source_snapshot_update_preserves_feature_lineage
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

DROP TRIGGER IF EXISTS trg_ksf_source_snapshots_provenance_immutable;
CREATE TRIGGER trg_ksf_source_snapshots_provenance_immutable
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

DROP TRIGGER IF EXISTS trg_ksf_source_snapshot_matches_metadata_update;
CREATE TRIGGER trg_ksf_source_snapshot_matches_metadata_update
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

DROP TRIGGER IF EXISTS trg_ksf_feature_matches_run_cutoff_update;
CREATE TRIGGER trg_ksf_feature_matches_run_cutoff_update
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

DROP TRIGGER IF EXISTS trg_ksf_feature_snapshot_cutoff_guard_update;
CREATE TRIGGER trg_ksf_feature_snapshot_cutoff_guard_update
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

DROP TRIGGER IF EXISTS trg_ksf_features_provenance_immutable;
CREATE TRIGGER trg_ksf_features_provenance_immutable
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

DROP TRIGGER IF EXISTS trg_ksf_decision_matches_run_cutoff_update;
CREATE TRIGGER trg_ksf_decision_matches_run_cutoff_update
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

DROP TRIGGER IF EXISTS trg_ksf_decision_update_preserves_settlement_lineage;
CREATE TRIGGER trg_ksf_decision_update_preserves_settlement_lineage
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

DROP TRIGGER IF EXISTS trg_ksf_decisions_append_only;
CREATE TRIGGER trg_ksf_decisions_append_only
BEFORE UPDATE ON ksf_decisions
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'decision rows are append-only; corrections require a new run/decision');
END;

DROP TRIGGER IF EXISTS trg_ksf_ai_request_matches_run_cutoff_update;
CREATE TRIGGER trg_ksf_ai_request_matches_run_cutoff_update
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

DROP TRIGGER IF EXISTS trg_ksf_ai_request_update_preserves_response_lineage;
CREATE TRIGGER trg_ksf_ai_request_update_preserves_response_lineage
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

DROP TRIGGER IF EXISTS trg_ksf_ai_requests_append_only;
CREATE TRIGGER trg_ksf_ai_requests_append_only
BEFORE UPDATE ON ksf_ai_requests
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'AI request rows are append-only; corrections require a new request');
END;

DROP TRIGGER IF EXISTS trg_ksf_ai_response_matches_request_update;
CREATE TRIGGER trg_ksf_ai_response_matches_request_update
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

DROP TRIGGER IF EXISTS trg_ksf_ai_responses_append_only;
CREATE TRIGGER trg_ksf_ai_responses_append_only
BEFORE UPDATE ON ksf_ai_responses
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'AI response rows are append-only; corrections require a new response');
END;

DROP TRIGGER IF EXISTS trg_ksf_settlement_matches_decision_update;
CREATE TRIGGER trg_ksf_settlement_matches_decision_update
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

DROP TRIGGER IF EXISTS trg_ksf_settlements_identity_immutable;
CREATE TRIGGER trg_ksf_settlements_identity_immutable
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

COMMIT;
