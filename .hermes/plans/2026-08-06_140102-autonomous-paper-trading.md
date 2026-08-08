# Autonomous Paper Trading Implementation Plan

> **For Hermes:** Implement task-by-task with strict TDD and two-stage read-only review. Commit, push, production config changes, timer changes, and activation remain separate user approval gates.

**Goal:** Add Alpha Vantage-backed global inputs and a fully autonomous, internal paper-only trading agent that can propose and execute simulated BUY/SELL actions without per-order human approval while remaining structurally unable to place real broker orders.

**Architecture:** Preserve KSF as the immutable data/decision source, then add a separate event-sourced paper ledger. The AI policy produces a schema-validated order proposal; a deterministic risk engine can reject or resize it; a paper broker fills accepted orders only on a later eligible market observation. No KIS virtual/live order adapter is imported or implemented.

**Tech Stack:** Python 3.12, SQLite append-only ledger, existing KSF feature/decision pipeline, APScheduler/systemd, FastAPI read-only dashboard, pytest/fakeredis only for legacy compatibility.

---

## Scope boundary

### Implement

- Alpha Vantage credential readiness and NVDA/SOXX/MU read-only collection.
- Internal paper account, decision snapshot, proposal, risk review, order, fill, cash/position, valuation, and daily equity records.
- Autonomous single-symbol AI proposal contract with deterministic fallback/abstention.
- Deterministic risk engine, idempotency, stale-data protection, kill switch, fee/tax/slippage model.
- Next-session simulated execution; no same-close fill from a same-close decision.
- Read-only paper portfolio/API/dashboard and Telegram digest.
- Disabled-by-default production runner and systemd timer integration.

### Defer

- KIS virtual brokerage orders.
- Live brokerage orders.
- Short selling, margin, leverage, derivatives, partial fills, limit-order queue simulation.
- DRAM/NAND/HBM licensed data until source rights are approved.
- Expansion beyond `005930` and `000660` until the first strategy has paper evidence.

### Forbidden

- Importing or calling any order/account-mutation endpoint.
- Treating sampled path fractions as calibrated probabilities.
- Trading on stale, missing-required, restricted-required, future-dated, or same-close look-ahead data.
- Letting free-form model text bypass deterministic policy.
- Printing or persisting Alpha Vantage/API credentials.

## Default policy

- Account currency/initial cash: KRW 10,000,000.
- Universe: `005930`, `000660` only.
- Long-only; no borrowing or negative cash/position.
- Max position: 20% of current NAV per symbol.
- Max aggregate equity exposure: 40% of NAV.
- Min order notional: KRW 50,000.
- Max one accepted order per symbol per decision session.
- Max four fills per trading day across the account.
- Daily loss stop: -2% from prior completed-session equity.
- Portfolio drawdown kill threshold: -10% from high-water mark.
- Fees/slippage: configurable and persisted with every fill; defaults must be documented, not claimed as broker-exact.
- Decision cadence: one post-close proposal after KSF data is complete.
- Execution cadence: next eligible completed/open observation, never the price used to create the decision.
- `PAPER_TRADING_ENABLED=false` by default.
- `PAPER_TRADING_KILL_SWITCH=true` by default until explicit production activation.

## Task 1: Alpha Vantage readiness and safe collection

**Files**
- Modify: `common/config.py`
- Modify: `ksf/global_collector.py`
- Modify: `ksf/production_runner.py`
- Modify: `scripts/deploy/run-ksf-production.sh` only if credential loading contract requires it; never embed variable values.
- Modify: `.env.example`
- Test: `tests/test_ksf_global_collector.py`
- Test: `tests/test_ksf_production_runner.py`

**TDD contract**

1. RED: missing key emits `MISSING_OPTIONAL` without network.
2. RED: configured key sends the documented read-only request but logs neither key nor key-bearing URL.
3. RED: vendor throttle/note/error payloads become explicit missing/rate-limited states, not zero values.
4. RED: all three peers are collected with bounded pacing and idempotent persistence.
5. RED: future-dated/after-cutoff observations are rejected.
6. GREEN: implement minimal config and collection behavior.
7. Run focused tests, full suite, secret scan, then one approval-scoped read-only live probe for NVDA/SOXX/MU.

**Credential injection**

- Production variable: `ALPHAVANTAGE_API_KEY`.
- Store only in the protected production env file already loaded by the KSF runner.
- Never pass in CLI arguments, URLs printed to logs, fixtures, diffs, or dashboard responses.

## Task 2: Paper ledger migration

**Files**
- Create: `paper_trading/__init__.py`
- Create: `paper_trading/migrations/001_autonomous_paper_trading_ledgers.sql`
- Create: `paper_trading/ledger.py`
- Test: `tests/test_paper_trading_ledger_migration.py`

**Database boundary**

- Use a dedicated persistent paper database (production target: `/srv/kronostock/data/paper_trading.sqlite3`).
- Do not add paper tables to the KSF research ledger or its dashboard snapshot.
- Copy `run_id`, `decision_id`, `feature_snapshot_sha256`, and `available_data_cutoff` as immutable external lineage values after validating them against the read-only KSF ledger at cycle start.
- Do not claim cross-database foreign keys: SQLite cannot enforce them across database files. Enforce internal paper-ledger foreign keys plus application-level KSF lineage verification.

**Tables**

- `paper_accounts`: account identity, currency, initial cash, enabled state, policy version.
- `paper_agent_decisions`: KSF run/decision lineage, immutable feature hash, data cutoff, agent action, rationale hash, abstention reason.
- `paper_order_proposals`: requested side/target exposure, idempotency key, model metadata.
- `paper_risk_reviews`: deterministic decision, reject codes, resized quantity/notional, policy snapshot hash.
- `paper_orders`: accepted simulated order state.
- `paper_fills`: execution observation, raw reference hash, fill price, quantity, fee, tax, slippage.
- `paper_cash_events`: initial deposit and fill-related cash movements.
- `paper_position_lots`: quantity/cost lineage.
- `paper_equity_snapshots`: cash, market value, NAV, daily PnL, drawdown.
- `paper_agent_runs`: run-level success/failure/kill-switch audit.

**Database constraints**

- Append-only immutable identity/provenance fields.
- Foreign keys from proposals to KSF decisions/runs and from fills to orders.
- Unique idempotency key per account/session/symbol/action.
- CHECK constraints prohibit negative quantity, invalid side, nonpositive price, and future cutoff.
- State transitions are narrowly allowed; delete triggers abort.
- Migration is idempotent and passes integrity/foreign-key checks.

## Task 3: Structured agent proposal boundary

**Files**
- Create: `strategy/paper_agent.py`
- Create: `tests/test_paper_agent.py`
- Reuse carefully: `ksf/feature_engine.py`, `ksf/ai_explanation.py`

**Contract**

- Input: exactly one symbol, immutable KSF feature/decision snapshot, current paper holdings, policy limits, available-data cutoff.
- Output schema: `symbol`, `action` (`ENTER`, `HOLD`, `REDUCE`, `EXIT`, `ABSTAIN`), `target_exposure_pct`, cited evidence IDs, invalidation conditions, data gaps, model/provider/version.
- Do not call this a probability model unless calibration evidence exists.
- Missing/restricted/stale/future required features force `ABSTAIN` before model invocation.
- Invalid JSON, extra fields, uncited claims, cross-symbol comparison, invented prices/probabilities, timeout, or provider failure force deterministic `ABSTAIN`.
- Forecast state and portfolio action remain separate records.

**TDD contract**

- RED for every fail-closed path before implementation.
- Network/model transport injected; all tests offline.
- Raw free-form output never reaches order construction.

## Task 4: Deterministic risk engine

**Files**
- Create: `strategy/paper_risk.py`
- Create: `tests/test_paper_risk.py`

**Checks in order**

1. Trading enabled and kill switch off.
2. Account/run/session is not already processed.
3. KSF cutoff and source observations are eligible.
4. Action is valid for current holdings (`EXIT` with no position becomes no-op).
5. Cash cannot become negative.
6. Position and aggregate exposure caps.
7. Daily order/fill cap.
8. Daily loss and high-water drawdown stops.
9. Minimum notional and integer share rounding.
10. Persist accepted/rejected review with reason codes.

The risk engine owns final quantity. The agent never owns quantity directly.

## Task 5: Paper broker and next-session execution

**Files**
- Replace/extend carefully: `strategy/paper_trader.py`
- Create: `strategy/paper_broker.py`
- Create: `tests/test_paper_broker.py`
- Preserve compatibility tests for legacy `PaperPortfolio` until migration is complete.

**Execution contract**

- Proposal created from session T cannot fill using the same observation that created it.
- Fill from the next eligible market observation with persisted source timestamp.
- Configurable deterministic slippage/fee/tax model.
- BUY cannot exceed available cash after all costs.
- SELL cannot exceed held shares.
- Re-running the same fill event is idempotent.
- Transaction writes fill, cash event, position update, and equity snapshot atomically.
- No broker/KIS imports in module dependency graph.

## Task 6: Autonomous cycle orchestration

**Files**
- Create: `strategy/paper_cycle.py`
- Modify: `bot/scheduler.py`
- Modify: `scripts/deploy/kronostock-dry-run-once.sh`
- Modify/Create: `deploy/systemd/kronostock-paper-agent.service`
- Modify/Create: `deploy/systemd/kronostock-paper-agent.timer`
- Test: `tests/test_paper_cycle.py`
- Test: `tests/test_scheduler_dry_run.py`

**Cycle**

1. Acquire canonical lock.
2. Load latest completed KSF decisions and data cutoff.
3. Settle pending previous-session paper orders from eligible prices.
4. Mark portfolio/equity.
5. Apply kill-switch/risk-state checks.
6. Generate per-symbol structured proposals.
7. Deterministically risk-review proposals.
8. Queue accepted next-session paper orders.
9. Persist complete audit trail.
10. Send sanitized digest; alert failure must not roll back ledger.

A cycle failure must not partially mutate portfolio state. Re-run must reuse/reject duplicate idempotency keys.

## Task 7: Read-only paper dashboard/API

**Files**
- Modify: `dashboard/app.py`
- Create or modify: `strategy/paper_web_api.py`
- Test: `tests/test_paper_web_api.py`

**Routes**

- `GET /paper`: account NAV, cash, exposure, PnL, drawdown, kill-switch state.
- `GET /paper/orders`: proposals, deterministic risk result, order/fill status.
- `GET /paper/decisions/{symbol}`: cited AI decision snapshot and abstention/reject reasons.

**Safety**

- Read-only GET routes only.
- No enable/disable/order buttons or POST routes.
- No credentials, raw model prompts, raw vendor payloads, internal policy URLs, or private taxonomy.
- Distinguish `AI proposed`, `risk rejected`, `queued`, `filled`, `abstained`.

## Task 8: Strategy validation before autonomous activation

**Files**
- Create: `strategy/paper_evaluation.py`
- Create: `tests/test_paper_evaluation.py`
- Update: `README.md`

**Evidence ladder**

- Predictive validity: compare exact deployed signal with random walk, no-action, and simple flow/momentum baselines.
- Probability validity: do not claim calibrated probabilities until reliability/Brier evidence exists.
- Strategy validity: walk-forward execution with next-session timing, fees, tax, slippage, turnover, MDD.
- Paper operational validity: persist decisions before outcomes and run disabled shadow mode first.

**Activation sequence**

1. Unit/integration/full suite green.
2. Independent read-only code/security review.
3. Local replay with deterministic fixtures.
4. Production migration backup + integrity verification.
5. Production shadow mode: proposals/rejections only, no simulated fills.
6. User approval gate.
7. Enable internal paper fills only.
8. Monitor for at least 20 completed sessions before any strategy-validity claim.

## Verification commands

```bash
.venv/bin/pytest -q tests/test_ksf_global_collector.py
.venv/bin/pytest -q tests/test_paper_trading_ledger_migration.py
.venv/bin/pytest -q tests/test_paper_agent.py
.venv/bin/pytest -q tests/test_paper_risk.py
.venv/bin/pytest -q tests/test_paper_broker.py
.venv/bin/pytest -q tests/test_paper_cycle.py
.venv/bin/pytest -q tests/test_paper_web_api.py
.venv/bin/pytest -q
git diff --check
```

Add a static dependency scan proving the paper runtime contains no real/virtual broker order function, and an adversarial test proving repeated timers cannot duplicate a proposal, order, fill, or cash movement.

## Approval gates

Separate approvals are required for:

1. implementation start;
2. commit;
3. push/deploy (main push triggers VPS deployment);
4. writing `ALPHAVANTAGE_API_KEY` to production secrets;
5. applying migration 003 to the production ledger;
6. enabling shadow-mode timer;
7. enabling autonomous paper fills.

No approval in this plan authorizes KIS virtual or live brokerage orders.
