-- Autonomous paper trading ledger schema (Task 2)
-- Target: dedicated SQLite file, fully separate from the KSF research ledger.
--
-- Boundary rules:
-- * ksf_run_id / ksf_decision_id / feature_snapshot_sha256 / available_data_cutoff
--   are immutable external lineage values copied from the read-only KSF ledger.
--   SQLite cannot enforce cross-database foreign keys, so no FK is declared on
--   them; lineage is verified at application level. All internal paper-table
--   relationships use enforced foreign keys.
-- * Every business/event table is append-only: UPDATE and DELETE abort via
--   triggers. Only paper_schema_versions stays migration-manageable.
-- * Money and share quantities are integer KRW / integer shares only.
-- * State (order status, account enabled, kill switch) is derived from
--   append-only events; no mutable balance/state rows exist.
-- * Reason codes are allowlisted; only hashes of model rationale are stored.
--   No raw model text, no credential material, no external service payloads.
--
-- Rerun safety: tables/indexes use IF NOT EXISTS; triggers are dropped and
-- recreated so a rerun always installs the canonical strict definitions
-- (avoids the weak same-name trigger gap fixed by KSF migration 002).

PRAGMA foreign_keys = ON;

BEGIN;

CREATE TABLE IF NOT EXISTS paper_schema_versions (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')),
    notes TEXT
);

INSERT OR IGNORE INTO paper_schema_versions (version, name, notes)
VALUES (1, '001_autonomous_paper_trading_ledgers',
        'Event-sourced paper-only ledger: accounts, agent decisions, order proposals, deterministic risk reviews, simulated orders/fills, cash/position events, NAV snapshots, kill-switch events.');

-- ---------------------------------------------------------------------------
-- Accounts (immutable identity; enabled state lives in paper_account_events)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS paper_accounts (
    account_id TEXT PRIMARY KEY,
    currency TEXT NOT NULL CHECK (currency = 'KRW'),
    initial_cash_krw INTEGER NOT NULL CHECK (typeof(initial_cash_krw) = 'integer' AND initial_cash_krw > 0),
    policy_version TEXT NOT NULL CHECK (length(policy_version) > 0),
    created_at TEXT NOT NULL CHECK (
        julianday(created_at) IS NOT NULL
        AND (substr(created_at, -1) = 'Z' OR (substr(created_at, -6, 1) IN ('+','-') AND substr(created_at, -3, 1) = ':'))
    )
);

CREATE TABLE IF NOT EXISTS paper_account_events (
    account_event_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES paper_accounts(account_id),
    seq INTEGER NOT NULL CHECK (typeof(seq) = 'integer' AND seq >= 1),
    event_type TEXT NOT NULL CHECK (event_type IN ('CREATED','ENABLED','DISABLED')),
    reason_code TEXT CHECK (reason_code IS NULL OR reason_code IN ('INITIAL','MANUAL','KILL_SWITCH','POLICY')),
    event_at TEXT NOT NULL CHECK (
        julianday(event_at) IS NOT NULL
        AND (substr(event_at, -1) = 'Z' OR (substr(event_at, -6, 1) IN ('+','-') AND substr(event_at, -3, 1) = ':'))
    ),
    UNIQUE (account_id, seq),
    CHECK ((seq = 1) = (event_type = 'CREATED'))
);

-- ---------------------------------------------------------------------------
-- Agent decisions (KSF lineage copied by value; no cross-database FK possible)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS paper_agent_decisions (
    decision_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES paper_accounts(account_id),
    symbol TEXT NOT NULL CHECK (symbol IN ('005930','000660')),
    ksf_run_id TEXT NOT NULL CHECK (length(ksf_run_id) > 0),
    ksf_decision_id TEXT NOT NULL CHECK (length(ksf_decision_id) > 0),
    feature_snapshot_sha256 TEXT NOT NULL CHECK (
        length(feature_snapshot_sha256) = 64
        AND feature_snapshot_sha256 = lower(feature_snapshot_sha256)
        AND feature_snapshot_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    available_data_cutoff TEXT NOT NULL CHECK (
        julianday(available_data_cutoff) IS NOT NULL
        AND (substr(available_data_cutoff, -1) = 'Z' OR (substr(available_data_cutoff, -6, 1) IN ('+','-') AND substr(available_data_cutoff, -3, 1) = ':'))
    ),
    action TEXT NOT NULL CHECK (action IN ('BUY','SELL','HOLD','ABSTAIN')),
    reason_code TEXT NOT NULL CHECK (reason_code IN (
        'FLOW_SIGNAL_POSITIVE','FLOW_SIGNAL_NEGATIVE','NO_EDGE',
        'DATA_STALE','DATA_MISSING','DATA_RESTRICTED',
        'MODEL_UNAVAILABLE','MODEL_OUTPUT_INVALID',
        'KILL_SWITCH_ACTIVE','RISK_LIMIT_REACHED','MARKET_CLOSED'
    )),
    rationale_sha256 TEXT NOT NULL CHECK (
        length(rationale_sha256) = 64
        AND rationale_sha256 = lower(rationale_sha256)
        AND rationale_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL CHECK (
        julianday(created_at) IS NOT NULL
        AND (substr(created_at, -1) = 'Z' OR (substr(created_at, -6, 1) IN ('+','-') AND substr(created_at, -3, 1) = ':'))
    ),
    CHECK (julianday(available_data_cutoff) <= julianday(created_at)),
    UNIQUE (account_id, ksf_decision_id)
);

CREATE INDEX IF NOT EXISTS ix_paper_agent_decisions_account_symbol
ON paper_agent_decisions(account_id, symbol, created_at DESC);

-- ---------------------------------------------------------------------------
-- Order proposals (BUY/SELL only; HOLD/ABSTAIN never become proposals)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS paper_order_proposals (
    proposal_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL UNIQUE REFERENCES paper_agent_decisions(decision_id),
    account_id TEXT NOT NULL REFERENCES paper_accounts(account_id),
    symbol TEXT NOT NULL CHECK (symbol IN ('005930','000660')),
    side TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    quantity INTEGER NOT NULL CHECK (typeof(quantity) = 'integer' AND quantity > 0),
    target_exposure_bp INTEGER NOT NULL CHECK (typeof(target_exposure_bp) = 'integer' AND target_exposure_bp BETWEEN 1 AND 10000),
    idempotency_key TEXT NOT NULL UNIQUE CHECK (length(idempotency_key) > 0),
    model_name TEXT NOT NULL CHECK (length(model_name) > 0),
    model_version TEXT NOT NULL CHECK (length(model_version) > 0),
    proposed_at TEXT NOT NULL CHECK (
        julianday(proposed_at) IS NOT NULL
        AND (substr(proposed_at, -1) = 'Z' OR (substr(proposed_at, -6, 1) IN ('+','-') AND substr(proposed_at, -3, 1) = ':'))
    )
);

DROP TRIGGER IF EXISTS trg_paper_order_proposals_match_decision;
CREATE TRIGGER trg_paper_order_proposals_match_decision
BEFORE INSERT ON paper_order_proposals
FOR EACH ROW
WHEN EXISTS (
    SELECT 1 FROM paper_agent_decisions d
    WHERE d.decision_id = NEW.decision_id
      AND (d.action != NEW.side OR d.account_id != NEW.account_id OR d.symbol != NEW.symbol)
)
BEGIN
    SELECT RAISE(ABORT, 'paper_order_proposals: side/account/symbol must match the referenced agent decision');
END;

-- ---------------------------------------------------------------------------
-- Deterministic risk reviews (approve / reject / resize + policy hash)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS paper_risk_reviews (
    review_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL UNIQUE REFERENCES paper_order_proposals(proposal_id),
    verdict TEXT NOT NULL CHECK (verdict IN ('APPROVE','REJECT','RESIZE')),
    reject_reason_code TEXT CHECK (reject_reason_code IS NULL OR reject_reason_code IN (
        'TRADING_DISABLED','KILL_SWITCH_ACTIVE','DUPLICATE_SESSION','STALE_DATA','INVALID_ACTION',
        'INSUFFICIENT_CASH','INSUFFICIENT_POSITION','POSITION_LIMIT','EXPOSURE_LIMIT',
        'DAILY_FILL_LIMIT','DAILY_LOSS_STOP','DRAWDOWN_STOP','MIN_NOTIONAL'
    )),
    approved_quantity INTEGER CHECK (approved_quantity IS NULL OR (typeof(approved_quantity) = 'integer' AND approved_quantity > 0)),
    policy_sha256 TEXT NOT NULL CHECK (
        length(policy_sha256) = 64
        AND policy_sha256 = lower(policy_sha256)
        AND policy_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    reviewed_at TEXT NOT NULL CHECK (
        julianday(reviewed_at) IS NOT NULL
        AND (substr(reviewed_at, -1) = 'Z' OR (substr(reviewed_at, -6, 1) IN ('+','-') AND substr(reviewed_at, -3, 1) = ':'))
    ),
    CHECK ((verdict = 'REJECT') = (reject_reason_code IS NOT NULL)),
    CHECK ((verdict = 'REJECT') = (approved_quantity IS NULL))
);

-- ---------------------------------------------------------------------------
-- Orders (identity only; state derived from paper_order_events)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS paper_orders (
    order_id TEXT PRIMARY KEY,
    review_id TEXT NOT NULL UNIQUE REFERENCES paper_risk_reviews(review_id),
    proposal_id TEXT NOT NULL UNIQUE REFERENCES paper_order_proposals(proposal_id),
    account_id TEXT NOT NULL REFERENCES paper_accounts(account_id),
    symbol TEXT NOT NULL CHECK (symbol IN ('005930','000660')),
    side TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    quantity INTEGER NOT NULL CHECK (typeof(quantity) = 'integer' AND quantity > 0),
    idempotency_key TEXT NOT NULL UNIQUE CHECK (length(idempotency_key) > 0),
    created_at TEXT NOT NULL CHECK (
        julianday(created_at) IS NOT NULL
        AND (substr(created_at, -1) = 'Z' OR (substr(created_at, -6, 1) IN ('+','-') AND substr(created_at, -3, 1) = ':'))
    )
);

DROP TRIGGER IF EXISTS trg_paper_orders_require_accepted_review;
CREATE TRIGGER trg_paper_orders_require_accepted_review
BEFORE INSERT ON paper_orders
FOR EACH ROW
WHEN EXISTS (
    SELECT 1 FROM paper_risk_reviews r
    JOIN paper_order_proposals p ON p.proposal_id = r.proposal_id
    WHERE r.review_id = NEW.review_id
      AND (r.verdict NOT IN ('APPROVE','RESIZE')
           OR r.proposal_id != NEW.proposal_id
           OR r.approved_quantity != NEW.quantity
           OR p.account_id != NEW.account_id
           OR p.symbol != NEW.symbol
           OR p.side != NEW.side)
)
BEGIN
    SELECT RAISE(ABORT, 'paper_orders: order requires an APPROVE/RESIZE review with matching proposal account/symbol/side/quantity');
END;

CREATE TABLE IF NOT EXISTS paper_order_events (
    order_event_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES paper_orders(order_id),
    seq INTEGER NOT NULL CHECK (typeof(seq) = 'integer' AND seq >= 1),
    event_type TEXT NOT NULL CHECK (event_type IN ('ACCEPTED','FILLED','CANCELLED','EXPIRED')),
    event_at TEXT NOT NULL CHECK (
        julianday(event_at) IS NOT NULL
        AND (substr(event_at, -1) = 'Z' OR (substr(event_at, -6, 1) IN ('+','-') AND substr(event_at, -3, 1) = ':'))
    ),
    UNIQUE (order_id, seq),
    CHECK ((seq = 1) = (event_type = 'ACCEPTED'))
);

DROP TRIGGER IF EXISTS trg_paper_order_events_no_after_terminal;
CREATE TRIGGER trg_paper_order_events_no_after_terminal
BEFORE INSERT ON paper_order_events
FOR EACH ROW
WHEN EXISTS (
    SELECT 1 FROM paper_order_events e
    WHERE e.order_id = NEW.order_id AND e.event_type IN ('FILLED','CANCELLED','EXPIRED')
)
BEGIN
    SELECT RAISE(ABORT, 'paper_order_events: order already reached a terminal state');
END;

DROP TRIGGER IF EXISTS trg_paper_order_events_filled_needs_fill;
CREATE TRIGGER trg_paper_order_events_filled_needs_fill
BEFORE INSERT ON paper_order_events
FOR EACH ROW
WHEN NEW.event_type = 'FILLED'
 AND NOT EXISTS (SELECT 1 FROM paper_fills f WHERE f.order_id = NEW.order_id)
BEGIN
    SELECT RAISE(ABORT, 'paper_order_events: FILLED event requires a matching paper fill');
END;

-- ---------------------------------------------------------------------------
-- Fills (one full fill per accepted order; no partial fills in this phase)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS paper_fills (
    fill_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL UNIQUE REFERENCES paper_orders(order_id),
    account_id TEXT NOT NULL REFERENCES paper_accounts(account_id),
    symbol TEXT NOT NULL CHECK (symbol IN ('005930','000660')),
    side TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    fill_price_krw INTEGER NOT NULL CHECK (typeof(fill_price_krw) = 'integer' AND fill_price_krw > 0),
    quantity INTEGER NOT NULL CHECK (typeof(quantity) = 'integer' AND quantity > 0),
    fee_krw INTEGER NOT NULL CHECK (typeof(fee_krw) = 'integer' AND fee_krw >= 0),
    tax_krw INTEGER NOT NULL CHECK (typeof(tax_krw) = 'integer' AND tax_krw >= 0),
    -- slippage_krw is informational only: fill_price_krw already is the executed
    -- price (slippage included), so slippage must never be charged to cash again.
    slippage_krw INTEGER NOT NULL CHECK (typeof(slippage_krw) = 'integer' AND slippage_krw >= 0),
    observed_at TEXT NOT NULL CHECK (
        julianday(observed_at) IS NOT NULL
        AND (substr(observed_at, -1) = 'Z' OR (substr(observed_at, -6, 1) IN ('+','-') AND substr(observed_at, -3, 1) = ':'))
    ),
    observation_sha256 TEXT NOT NULL CHECK (
        length(observation_sha256) = 64
        AND observation_sha256 = lower(observation_sha256)
        AND observation_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    filled_at TEXT NOT NULL CHECK (
        julianday(filled_at) IS NOT NULL
        AND (substr(filled_at, -1) = 'Z' OR (substr(filled_at, -6, 1) IN ('+','-') AND substr(filled_at, -3, 1) = ':'))
    ),
    CHECK (julianday(observed_at) <= julianday(filled_at))
);

DROP TRIGGER IF EXISTS trg_paper_fills_require_accepted_order;
CREATE TRIGGER trg_paper_fills_require_accepted_order
BEFORE INSERT ON paper_fills
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1 FROM paper_order_events e
    WHERE e.order_id = NEW.order_id AND e.event_type = 'ACCEPTED'
)
 OR EXISTS (
    SELECT 1 FROM paper_order_events e
    WHERE e.order_id = NEW.order_id AND e.event_type IN ('FILLED','CANCELLED','EXPIRED')
)
BEGIN
    SELECT RAISE(ABORT, 'paper_fills: fill requires an ACCEPTED, non-terminal paper order');
END;

DROP TRIGGER IF EXISTS trg_paper_fills_match_order;
CREATE TRIGGER trg_paper_fills_match_order
BEFORE INSERT ON paper_fills
FOR EACH ROW
WHEN EXISTS (
    SELECT 1 FROM paper_orders o
    WHERE o.order_id = NEW.order_id
      AND (o.account_id != NEW.account_id OR o.symbol != NEW.symbol
           OR o.side != NEW.side OR o.quantity != NEW.quantity)
)
BEGIN
    SELECT RAISE(ABORT, 'paper_fills: account/symbol/side/quantity must match the referenced order');
END;

-- ---------------------------------------------------------------------------
-- Cash events (integer KRW; balances are derived, never stored)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS paper_cash_events (
    cash_event_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES paper_accounts(account_id),
    fill_id TEXT REFERENCES paper_fills(fill_id),
    event_type TEXT NOT NULL CHECK (event_type IN ('INITIAL_DEPOSIT','FILL_DEBIT','FILL_CREDIT','FEE','TAX')),
    amount_krw INTEGER NOT NULL CHECK (typeof(amount_krw) = 'integer' AND amount_krw != 0),
    event_at TEXT NOT NULL CHECK (
        julianday(event_at) IS NOT NULL
        AND (substr(event_at, -1) = 'Z' OR (substr(event_at, -6, 1) IN ('+','-') AND substr(event_at, -3, 1) = ':'))
    ),
    CHECK ((event_type = 'INITIAL_DEPOSIT') = (fill_id IS NULL)),
    CHECK (CASE event_type
               WHEN 'INITIAL_DEPOSIT' THEN amount_krw > 0
               WHEN 'FILL_CREDIT' THEN amount_krw > 0
               ELSE amount_krw < 0
           END),
    UNIQUE (fill_id, event_type)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_paper_cash_events_initial_deposit
ON paper_cash_events(account_id) WHERE event_type = 'INITIAL_DEPOSIT';

CREATE INDEX IF NOT EXISTS ix_paper_cash_events_account ON paper_cash_events(account_id);

-- ---------------------------------------------------------------------------
-- Position events (integer share deltas; positions are derived)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS paper_position_events (
    position_event_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES paper_accounts(account_id),
    symbol TEXT NOT NULL CHECK (symbol IN ('005930','000660')),
    fill_id TEXT NOT NULL UNIQUE REFERENCES paper_fills(fill_id),
    quantity_delta INTEGER NOT NULL CHECK (typeof(quantity_delta) = 'integer' AND quantity_delta != 0),
    event_at TEXT NOT NULL CHECK (
        julianday(event_at) IS NOT NULL
        AND (substr(event_at, -1) = 'Z' OR (substr(event_at, -6, 1) IN ('+','-') AND substr(event_at, -3, 1) = ':'))
    )
);

CREATE INDEX IF NOT EXISTS ix_paper_position_events_account_symbol
ON paper_position_events(account_id, symbol);

-- ---------------------------------------------------------------------------
-- NAV snapshots (per completed session; nav must equal cash + market value)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS paper_nav_snapshots (
    nav_snapshot_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES paper_accounts(account_id),
    session_date TEXT NOT NULL CHECK (session_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    cash_krw INTEGER NOT NULL CHECK (typeof(cash_krw) = 'integer' AND cash_krw >= 0),
    position_value_krw INTEGER NOT NULL CHECK (typeof(position_value_krw) = 'integer' AND position_value_krw >= 0),
    nav_krw INTEGER NOT NULL CHECK (typeof(nav_krw) = 'integer'),
    snapshot_at TEXT NOT NULL CHECK (
        julianday(snapshot_at) IS NOT NULL
        AND (substr(snapshot_at, -1) = 'Z' OR (substr(snapshot_at, -6, 1) IN ('+','-') AND substr(snapshot_at, -3, 1) = ':'))
    ),
    CHECK (nav_krw = cash_krw + position_value_krw),
    UNIQUE (account_id, session_date)
);

-- ---------------------------------------------------------------------------
-- Kill-switch events (state derived from latest event; fail-closed in code)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS paper_kill_switch_events (
    kill_switch_event_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES paper_accounts(account_id),
    seq INTEGER NOT NULL CHECK (typeof(seq) = 'integer' AND seq >= 1),
    event_type TEXT NOT NULL CHECK (event_type IN ('ENGAGED','RELEASED')),
    reason_code TEXT NOT NULL CHECK (reason_code IN (
        'MANUAL','STARTUP_DEFAULT','DAILY_LOSS_STOP','DRAWDOWN_STOP','DATA_INTEGRITY','OPERATOR_STOP'
    )),
    event_at TEXT NOT NULL CHECK (
        julianday(event_at) IS NOT NULL
        AND (substr(event_at, -1) = 'Z' OR (substr(event_at, -6, 1) IN ('+','-') AND substr(event_at, -3, 1) = ':'))
    ),
    UNIQUE (account_id, seq)
);

-- ---------------------------------------------------------------------------
-- Ledger integrity triggers (Task 2 blockers 1-5)
--
-- * Parent inserts atomically emit their canonical deterministic child events
--   at schema level, so direct SQL cannot create a paper_accounts row without
--   CREATED + INITIAL_DEPOSIT, a paper_orders row without ACCEPTED, or a
--   paper_fills row without cash/position/FILLED events. A failure in any
--   emitted child aborts the statement and rolls back the parent insert.
-- * Fill-linked cash/position events must exactly match the referenced fill
--   (account, side, gross amount, fee, tax, position delta, filled_at).
--   slippage_krw is informational only and is never charged to cash.
-- * Risk quantity semantics and monotonic causal timestamps are enforced in
--   schema; the KRX next-session calendar rule is out of scope (broker task).
-- * Derived cash/share sufficiency at fill INSERT intentionally duplicates
--   later risk checks as a final ledger fail-safe.
-- ---------------------------------------------------------------------------

-- Blocker 4: monotonic causal timestamps ------------------------------------

DROP TRIGGER IF EXISTS trg_paper_order_proposals_causal_time;
CREATE TRIGGER trg_paper_order_proposals_causal_time
BEFORE INSERT ON paper_order_proposals
FOR EACH ROW
WHEN EXISTS (
    SELECT 1 FROM paper_agent_decisions d
    WHERE d.decision_id = NEW.decision_id
      AND julianday(NEW.proposed_at) < julianday(d.created_at)
)
BEGIN
    SELECT RAISE(ABORT, 'paper_order_proposals: proposed_at must not precede decision created_at');
END;

DROP TRIGGER IF EXISTS trg_paper_risk_reviews_causal_time;
CREATE TRIGGER trg_paper_risk_reviews_causal_time
BEFORE INSERT ON paper_risk_reviews
FOR EACH ROW
WHEN EXISTS (
    SELECT 1 FROM paper_order_proposals p
    WHERE p.proposal_id = NEW.proposal_id
      AND julianday(NEW.reviewed_at) < julianday(p.proposed_at)
)
BEGIN
    SELECT RAISE(ABORT, 'paper_risk_reviews: reviewed_at must not precede proposal proposed_at');
END;

DROP TRIGGER IF EXISTS trg_paper_orders_causal_time;
CREATE TRIGGER trg_paper_orders_causal_time
BEFORE INSERT ON paper_orders
FOR EACH ROW
WHEN EXISTS (
    SELECT 1 FROM paper_risk_reviews r
    WHERE r.review_id = NEW.review_id
      AND julianday(NEW.created_at) < julianday(r.reviewed_at)
)
BEGIN
    SELECT RAISE(ABORT, 'paper_orders: created_at must not precede review reviewed_at');
END;

DROP TRIGGER IF EXISTS trg_paper_account_events_causal_time;
CREATE TRIGGER trg_paper_account_events_causal_time
BEFORE INSERT ON paper_account_events
FOR EACH ROW
WHEN julianday(NEW.event_at) < (SELECT julianday(a.created_at) FROM paper_accounts a WHERE a.account_id = NEW.account_id)
  OR julianday(NEW.event_at) < (SELECT MAX(julianday(e.event_at)) FROM paper_account_events e WHERE e.account_id = NEW.account_id)
BEGIN
    SELECT RAISE(ABORT, 'paper_account_events: event_at must not precede account creation or prior events');
END;

DROP TRIGGER IF EXISTS trg_paper_kill_switch_events_causal_time;
CREATE TRIGGER trg_paper_kill_switch_events_causal_time
BEFORE INSERT ON paper_kill_switch_events
FOR EACH ROW
WHEN julianday(NEW.event_at) < (SELECT julianday(a.created_at) FROM paper_accounts a WHERE a.account_id = NEW.account_id)
  OR julianday(NEW.event_at) < (SELECT MAX(julianday(e.event_at)) FROM paper_kill_switch_events e WHERE e.account_id = NEW.account_id)
BEGIN
    SELECT RAISE(ABORT, 'paper_kill_switch_events: event_at must not precede account creation or prior events');
END;

DROP TRIGGER IF EXISTS trg_paper_order_events_causal_time;
CREATE TRIGGER trg_paper_order_events_causal_time
BEFORE INSERT ON paper_order_events
FOR EACH ROW
WHEN julianday(NEW.event_at) < (SELECT julianday(o.created_at) FROM paper_orders o WHERE o.order_id = NEW.order_id)
  OR julianday(NEW.event_at) < (SELECT MAX(julianday(e.event_at)) FROM paper_order_events e WHERE e.order_id = NEW.order_id)
BEGIN
    SELECT RAISE(ABORT, 'paper_order_events: event_at must not precede order creation or prior events');
END;

-- observed_at strictly after order created_at; filled_at >= observed_at is a
-- table-level CHECK on paper_fills.
DROP TRIGGER IF EXISTS trg_paper_fills_causal_time;
CREATE TRIGGER trg_paper_fills_causal_time
BEFORE INSERT ON paper_fills
FOR EACH ROW
WHEN EXISTS (
    SELECT 1 FROM paper_orders o
    WHERE o.order_id = NEW.order_id
      AND julianday(NEW.observed_at) <= julianday(o.created_at)
)
BEGIN
    SELECT RAISE(ABORT, 'paper_fills: observed_at must be strictly after order created_at');
END;

-- Blocker 3: risk quantity semantics (REJECT shape is a table-level CHECK) ---

DROP TRIGGER IF EXISTS trg_paper_risk_reviews_quantity_semantics;
CREATE TRIGGER trg_paper_risk_reviews_quantity_semantics
BEFORE INSERT ON paper_risk_reviews
FOR EACH ROW
WHEN EXISTS (
    SELECT 1 FROM paper_order_proposals p
    WHERE p.proposal_id = NEW.proposal_id
      AND ((NEW.verdict = 'APPROVE' AND NEW.approved_quantity != p.quantity)
        OR (NEW.verdict = 'RESIZE' AND NEW.approved_quantity >= p.quantity))
)
BEGIN
    SELECT RAISE(ABORT, 'paper_risk_reviews: APPROVE quantity must equal proposal quantity; RESIZE must be strictly smaller');
END;

-- Blocker 5: derived balance fail-safes at fill INSERT -----------------------

DROP TRIGGER IF EXISTS trg_paper_fills_buy_requires_cash;
CREATE TRIGGER trg_paper_fills_buy_requires_cash
BEFORE INSERT ON paper_fills
FOR EACH ROW
WHEN NEW.side = 'BUY'
 AND (SELECT COALESCE(SUM(amount_krw), 0) FROM paper_cash_events WHERE account_id = NEW.account_id)
     < NEW.fill_price_krw * NEW.quantity + NEW.fee_krw + NEW.tax_krw
BEGIN
    SELECT RAISE(ABORT, 'paper_fills: derived cash below BUY gross + fee + tax');
END;

DROP TRIGGER IF EXISTS trg_paper_fills_sell_requires_shares;
CREATE TRIGGER trg_paper_fills_sell_requires_shares
BEFORE INSERT ON paper_fills
FOR EACH ROW
WHEN NEW.side = 'SELL'
 AND (SELECT COALESCE(SUM(quantity_delta), 0) FROM paper_position_events
      WHERE account_id = NEW.account_id AND symbol = NEW.symbol) < NEW.quantity
BEGIN
    SELECT RAISE(ABORT, 'paper_fills: derived held shares below SELL quantity');
END;

-- Blocker 2: fill-linked events must exactly match the referenced fill -------

DROP TRIGGER IF EXISTS trg_paper_cash_events_match_fill;
CREATE TRIGGER trg_paper_cash_events_match_fill
BEFORE INSERT ON paper_cash_events
FOR EACH ROW
WHEN NEW.fill_id IS NOT NULL
 AND NOT EXISTS (
    SELECT 1 FROM paper_fills f
    WHERE f.fill_id = NEW.fill_id
      AND f.account_id = NEW.account_id
      AND NEW.event_at = f.filled_at
      AND ((NEW.event_type = 'FILL_DEBIT' AND f.side = 'BUY' AND NEW.amount_krw = -(f.fill_price_krw * f.quantity))
        OR (NEW.event_type = 'FILL_CREDIT' AND f.side = 'SELL' AND NEW.amount_krw = f.fill_price_krw * f.quantity)
        OR (NEW.event_type = 'FEE' AND f.fee_krw > 0 AND NEW.amount_krw = -f.fee_krw)
        OR (NEW.event_type = 'TAX' AND f.tax_krw > 0 AND NEW.amount_krw = -f.tax_krw))
)
BEGIN
    SELECT RAISE(ABORT, 'paper_cash_events: fill-linked event must exactly match the referenced fill');
END;

DROP TRIGGER IF EXISTS trg_paper_position_events_match_fill;
CREATE TRIGGER trg_paper_position_events_match_fill
BEFORE INSERT ON paper_position_events
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1 FROM paper_fills f
    WHERE f.fill_id = NEW.fill_id
      AND f.account_id = NEW.account_id
      AND f.symbol = NEW.symbol
      AND NEW.event_at = f.filled_at
      AND NEW.quantity_delta = CASE f.side WHEN 'BUY' THEN f.quantity ELSE -f.quantity END
)
BEGIN
    SELECT RAISE(ABORT, 'paper_position_events: event must exactly match the referenced fill');
END;

-- Blocker 1: parent inserts atomically emit canonical child events -----------

DROP TRIGGER IF EXISTS trg_paper_accounts_emit_children;
CREATE TRIGGER trg_paper_accounts_emit_children
AFTER INSERT ON paper_accounts
FOR EACH ROW
BEGIN
    INSERT INTO paper_account_events (account_event_id, account_id, seq, event_type, reason_code, event_at)
    VALUES (NEW.account_id || ':evt:1', NEW.account_id, 1, 'CREATED', 'INITIAL', NEW.created_at);
    INSERT INTO paper_cash_events (cash_event_id, account_id, fill_id, event_type, amount_krw, event_at)
    VALUES (NEW.account_id || ':cash:initial', NEW.account_id, NULL, 'INITIAL_DEPOSIT', NEW.initial_cash_krw, NEW.created_at);
END;

DROP TRIGGER IF EXISTS trg_paper_orders_emit_accepted;
CREATE TRIGGER trg_paper_orders_emit_accepted
AFTER INSERT ON paper_orders
FOR EACH ROW
BEGIN
    INSERT INTO paper_order_events (order_event_id, order_id, seq, event_type, event_at)
    VALUES (NEW.order_id || ':evt:1', NEW.order_id, 1, 'ACCEPTED', NEW.created_at);
END;

DROP TRIGGER IF EXISTS trg_paper_fills_emit_children;
CREATE TRIGGER trg_paper_fills_emit_children
AFTER INSERT ON paper_fills
FOR EACH ROW
BEGIN
    INSERT INTO paper_cash_events (cash_event_id, account_id, fill_id, event_type, amount_krw, event_at)
    VALUES (NEW.fill_id || ':cash:trade', NEW.account_id, NEW.fill_id,
            CASE NEW.side WHEN 'BUY' THEN 'FILL_DEBIT' ELSE 'FILL_CREDIT' END,
            CASE NEW.side WHEN 'BUY' THEN -(NEW.fill_price_krw * NEW.quantity) ELSE NEW.fill_price_krw * NEW.quantity END,
            NEW.filled_at);
    INSERT INTO paper_cash_events (cash_event_id, account_id, fill_id, event_type, amount_krw, event_at)
    SELECT NEW.fill_id || ':cash:fee', NEW.account_id, NEW.fill_id, 'FEE', -NEW.fee_krw, NEW.filled_at
    WHERE NEW.fee_krw > 0;
    INSERT INTO paper_cash_events (cash_event_id, account_id, fill_id, event_type, amount_krw, event_at)
    SELECT NEW.fill_id || ':cash:tax', NEW.account_id, NEW.fill_id, 'TAX', -NEW.tax_krw, NEW.filled_at
    WHERE NEW.tax_krw > 0;
    INSERT INTO paper_position_events (position_event_id, account_id, symbol, fill_id, quantity_delta, event_at)
    VALUES (NEW.fill_id || ':pos', NEW.account_id, NEW.symbol, NEW.fill_id,
            CASE NEW.side WHEN 'BUY' THEN NEW.quantity ELSE -NEW.quantity END,
            NEW.filled_at);
    INSERT INTO paper_order_events (order_event_id, order_id, seq, event_type, event_at)
    VALUES (NEW.order_id || ':evt:filled', NEW.order_id,
            (SELECT COALESCE(MAX(seq), 0) + 1 FROM paper_order_events WHERE order_id = NEW.order_id),
            'FILLED', NEW.filled_at);
END;

-- ---------------------------------------------------------------------------
-- Append-only guards: every business/event table rejects UPDATE and DELETE.
-- ---------------------------------------------------------------------------

DROP TRIGGER IF EXISTS trg_paper_accounts_no_update;
CREATE TRIGGER trg_paper_accounts_no_update
BEFORE UPDATE ON paper_accounts FOR EACH ROW
BEGIN SELECT RAISE(ABORT, 'paper_accounts is append-only'); END;

DROP TRIGGER IF EXISTS trg_paper_accounts_no_delete;
CREATE TRIGGER trg_paper_accounts_no_delete
BEFORE DELETE ON paper_accounts FOR EACH ROW
BEGIN SELECT RAISE(ABORT, 'paper_accounts is append-only'); END;

DROP TRIGGER IF EXISTS trg_paper_account_events_no_update;
CREATE TRIGGER trg_paper_account_events_no_update
BEFORE UPDATE ON paper_account_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT, 'paper_account_events is append-only'); END;

DROP TRIGGER IF EXISTS trg_paper_account_events_no_delete;
CREATE TRIGGER trg_paper_account_events_no_delete
BEFORE DELETE ON paper_account_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT, 'paper_account_events is append-only'); END;

DROP TRIGGER IF EXISTS trg_paper_agent_decisions_no_update;
CREATE TRIGGER trg_paper_agent_decisions_no_update
BEFORE UPDATE ON paper_agent_decisions FOR EACH ROW
BEGIN SELECT RAISE(ABORT, 'paper_agent_decisions is append-only'); END;

DROP TRIGGER IF EXISTS trg_paper_agent_decisions_no_delete;
CREATE TRIGGER trg_paper_agent_decisions_no_delete
BEFORE DELETE ON paper_agent_decisions FOR EACH ROW
BEGIN SELECT RAISE(ABORT, 'paper_agent_decisions is append-only'); END;

DROP TRIGGER IF EXISTS trg_paper_order_proposals_no_update;
CREATE TRIGGER trg_paper_order_proposals_no_update
BEFORE UPDATE ON paper_order_proposals FOR EACH ROW
BEGIN SELECT RAISE(ABORT, 'paper_order_proposals is append-only'); END;

DROP TRIGGER IF EXISTS trg_paper_order_proposals_no_delete;
CREATE TRIGGER trg_paper_order_proposals_no_delete
BEFORE DELETE ON paper_order_proposals FOR EACH ROW
BEGIN SELECT RAISE(ABORT, 'paper_order_proposals is append-only'); END;

DROP TRIGGER IF EXISTS trg_paper_risk_reviews_no_update;
CREATE TRIGGER trg_paper_risk_reviews_no_update
BEFORE UPDATE ON paper_risk_reviews FOR EACH ROW
BEGIN SELECT RAISE(ABORT, 'paper_risk_reviews is append-only'); END;

DROP TRIGGER IF EXISTS trg_paper_risk_reviews_no_delete;
CREATE TRIGGER trg_paper_risk_reviews_no_delete
BEFORE DELETE ON paper_risk_reviews FOR EACH ROW
BEGIN SELECT RAISE(ABORT, 'paper_risk_reviews is append-only'); END;

DROP TRIGGER IF EXISTS trg_paper_orders_no_update;
CREATE TRIGGER trg_paper_orders_no_update
BEFORE UPDATE ON paper_orders FOR EACH ROW
BEGIN SELECT RAISE(ABORT, 'paper_orders is append-only'); END;

DROP TRIGGER IF EXISTS trg_paper_orders_no_delete;
CREATE TRIGGER trg_paper_orders_no_delete
BEFORE DELETE ON paper_orders FOR EACH ROW
BEGIN SELECT RAISE(ABORT, 'paper_orders is append-only'); END;

DROP TRIGGER IF EXISTS trg_paper_order_events_no_update;
CREATE TRIGGER trg_paper_order_events_no_update
BEFORE UPDATE ON paper_order_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT, 'paper_order_events is append-only'); END;

DROP TRIGGER IF EXISTS trg_paper_order_events_no_delete;
CREATE TRIGGER trg_paper_order_events_no_delete
BEFORE DELETE ON paper_order_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT, 'paper_order_events is append-only'); END;

DROP TRIGGER IF EXISTS trg_paper_fills_no_update;
CREATE TRIGGER trg_paper_fills_no_update
BEFORE UPDATE ON paper_fills FOR EACH ROW
BEGIN SELECT RAISE(ABORT, 'paper_fills is append-only'); END;

DROP TRIGGER IF EXISTS trg_paper_fills_no_delete;
CREATE TRIGGER trg_paper_fills_no_delete
BEFORE DELETE ON paper_fills FOR EACH ROW
BEGIN SELECT RAISE(ABORT, 'paper_fills is append-only'); END;

DROP TRIGGER IF EXISTS trg_paper_cash_events_no_update;
CREATE TRIGGER trg_paper_cash_events_no_update
BEFORE UPDATE ON paper_cash_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT, 'paper_cash_events is append-only'); END;

DROP TRIGGER IF EXISTS trg_paper_cash_events_no_delete;
CREATE TRIGGER trg_paper_cash_events_no_delete
BEFORE DELETE ON paper_cash_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT, 'paper_cash_events is append-only'); END;

DROP TRIGGER IF EXISTS trg_paper_position_events_no_update;
CREATE TRIGGER trg_paper_position_events_no_update
BEFORE UPDATE ON paper_position_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT, 'paper_position_events is append-only'); END;

DROP TRIGGER IF EXISTS trg_paper_position_events_no_delete;
CREATE TRIGGER trg_paper_position_events_no_delete
BEFORE DELETE ON paper_position_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT, 'paper_position_events is append-only'); END;

DROP TRIGGER IF EXISTS trg_paper_nav_snapshots_no_update;
CREATE TRIGGER trg_paper_nav_snapshots_no_update
BEFORE UPDATE ON paper_nav_snapshots FOR EACH ROW
BEGIN SELECT RAISE(ABORT, 'paper_nav_snapshots is append-only'); END;

DROP TRIGGER IF EXISTS trg_paper_nav_snapshots_no_delete;
CREATE TRIGGER trg_paper_nav_snapshots_no_delete
BEFORE DELETE ON paper_nav_snapshots FOR EACH ROW
BEGIN SELECT RAISE(ABORT, 'paper_nav_snapshots is append-only'); END;

DROP TRIGGER IF EXISTS trg_paper_kill_switch_events_no_update;
CREATE TRIGGER trg_paper_kill_switch_events_no_update
BEFORE UPDATE ON paper_kill_switch_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT, 'paper_kill_switch_events is append-only'); END;

DROP TRIGGER IF EXISTS trg_paper_kill_switch_events_no_delete;
CREATE TRIGGER trg_paper_kill_switch_events_no_delete
BEFORE DELETE ON paper_kill_switch_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT, 'paper_kill_switch_events is append-only'); END;

COMMIT;
