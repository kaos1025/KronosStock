# K-Semiconductor Flow Desk production one-shot runbook

This repository supplies deployment artifacts only. Do not run these commands
until production change approval and a credential review are complete. The
runner collects read-only market data for `005930` and `000660`, materializes a
canonical scoring run, and writes deterministic 1/5/20-day decisions. It never
calls an AI provider or performance settlement. Those stages report
`not_activated`. KIS observations remain `INTRADAY_ESTIMATE`, including on the
post-close schedule, because they are not an official close provider.

The dashboard service is not installed. It remains out of scope until
`KSF_READ_TOKEN` is approved; bearer authentication must not be bypassed or
weakened.

## Paper-agent release boundary

The paper agent is a distinct, disabled-by-default release. Apply migrations
`001` then `002` to the production KSF SQLite database through the approved
KSF runner release procedure; do not hand-edit schema rows. Before release,
back up both KSF and paper databases with SQLite `.backup`, record file hashes,
and verify schema versions `1` and `2`, `integrity_check=ok`, and an empty
`foreign_key_check` result.

Install the wrapper and service template only after review. The wrapper derives
one KST `SESSION_ID` and `CYCLE_AT`, selects exactly
`$PAPER_AGENT_BUNDLE_DIR/YYYY-MM-DD.json`, rejects missing files and symlinks,
and passes all content validation to `strategy.paper_cycle` before the paper DB
is opened. The immutable bundle must use `ksf-response-bundle-v1`, contain only
symbol-keyed normalized responses, and bind its own hash to the session, cycle,
KSF file hash, model identity, run ID, decision ID, and feature snapshot hash.
Never place raw provider payloads, prompts, credentials, headers, or tokens in
the bundle.

For the first smoke, keep `PAPER_AGENT_ENABLED=false` and confirm the bounded
`paper-cycle status=disabled` output. Then, under an approved maintenance
window, use `PAPER_AGENT_ENABLED=true`, `PAPER_AGENT_MODE=shadow`, and leave
`PAPER_AGENT_FILLS_ENABLED=false`. A successful shadow smoke must show exactly
two exact-session lineages, no missing/stale required feature, matching hashes,
one exact configured-horizon decision per symbol, a committed paper cycle, and
zero paper orders/fills. Do not enable the unit or timer as part of the smoke.

Fills are a separate change: `PAPER_AGENT_MODE=fills` is insufficient unless
`PAPER_AGENT_FILLS_ENABLED=true` is also explicitly approved. Internal fills
and shadow operation must never share an enable switch.

Monitor only bounded metadata: service result, session, mode, decision counts,
replay status, SQLite integrity/foreign-key results, KSF run freshness, bundle
age, and hash-match failures. Alert on a missing exact-session run, duplicate
horizon, stale/partial bundle, provenance mismatch, unexpected replay, any
paper order/fill in shadow, or journal output containing payload-like data.
Do not log response bodies or environment values.

To roll back, disable the paper unit/timer first, restore the reviewed prior
service and wrapper artifacts, and retain the failed databases and bundle as
restricted audit evidence. Restore a database only from the pre-release SQLite
backup during a maintenance window; verify hashes and integrity before any
restart. Keep both paper enable flags false until root cause and provenance are
reviewed. KSF collection can remain active if its own integrity and scoring
checks pass; paper-agent rollback does not authorize changing the KSF timer.

## Zero-gap cutover rule

The old dry-run timer remains active through backup, install, `daemon-reload`,
and the direct KSF one-shot smoke. Any smoke failure stops the cutover: leave the
old dry-run timer active, do not enable the KSF timer, inspect the bounded runner
summary/journal output, and roll back the installed KSF unit files if needed.
Only after the smoke and SQLite integrity/status checks pass should the KSF timer
be enabled; then immediately disable the old dry-run timer. Acceptance requires
runner JSON `status=ok`, both supported symbols with empty `missing_required` and
`stale_required`, `PRAGMA integrity_check` = `ok`, and no `PRAGMA foreign_key_check`
rows. Never enable both timers for the same scheduled window beyond this controlled handoff.

## Backup, install, smoke, and handoff

The smoke is a production ledger write and must only be run after approval.
Output is a bounded JSON summary without credentials or source payloads. The
wrapper explicitly loads the private `/srv/agent-workspaces/KronosStock/.env`
without printing values and fails closed if that env file is absent.

```bash
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="/srv/kronostock/backups/ksf-$STAMP"
sudo install -d -o deploy -g deploy -m 700 "$BACKUP"
sudo test ! -e /srv/kronostock/data/ksf_ledger.sqlite3 || sudo -u deploy sqlite3 /srv/kronostock/data/ksf_ledger.sqlite3 ".backup '$BACKUP/ksf_ledger.sqlite3'"
sudo test ! -e /etc/systemd/system/kronostock-ksf.service || sudo cp -a /etc/systemd/system/kronostock-ksf.service "$BACKUP/"
sudo test ! -e /etc/systemd/system/kronostock-ksf.timer || sudo cp -a /etc/systemd/system/kronostock-ksf.timer "$BACKUP/"
sudo test ! -e /etc/systemd/system/kronostock-dry-run.service || sudo cp -a /etc/systemd/system/kronostock-dry-run.service "$BACKUP/"
sudo test ! -e /etc/systemd/system/kronostock-dry-run.timer || sudo cp -a /etc/systemd/system/kronostock-dry-run.timer "$BACKUP/"

sudo install -d -o deploy -g deploy -m 700 /srv/kronostock/data
sudo install -o deploy -g deploy -m 700 scripts/deploy/kronostock-ksf-once.sh /srv/kronostock/kronostock-ksf-once.sh
sudo install -o root -g root -m 644 deploy/systemd/kronostock-ksf.service /etc/systemd/system/kronostock-ksf.service
sudo install -o root -g root -m 644 deploy/systemd/kronostock-ksf.timer /etc/systemd/system/kronostock-ksf.timer
sudo systemctl daemon-reload

# Smoke while the old dry-run timer is still active. Stop here on any failure.
sudo -u deploy /srv/kronostock/kronostock-ksf-once.sh
sudo systemctl status kronostock-ksf.service kronostock-ksf.timer kronostock-dry-run.timer --no-pager
sudo systemctl list-timers kronostock-ksf.timer kronostock-dry-run.timer --no-pager
sudo journalctl -u kronostock-ksf.service -n 50 --no-pager
sudo -u deploy sqlite3 /srv/kronostock/data/ksf_ledger.sqlite3 "PRAGMA integrity_check; PRAGMA foreign_key_check;"

# Only after the smoke and DB/status checks pass, hand the schedule over.
sudo systemctl enable --now kronostock-ksf.timer
sudo systemctl disable --now kronostock-dry-run.timer
sudo systemctl list-timers kronostock-ksf.timer kronostock-dry-run.timer --no-pager
```

## Disable and rollback

Rollback restores the backed-up dry-run units and enables the old timer again.
If the smoke fails before the schedule handoff, the old timer should already be
active; keep it active and remove/repair the KSF artifacts before retrying.

```bash
sudo systemctl disable --now kronostock-ksf.timer
sudo rm /etc/systemd/system/kronostock-ksf.timer /etc/systemd/system/kronostock-ksf.service
sudo systemctl daemon-reload
sudo systemctl reset-failed kronostock-ksf.service

# Set BACKUP to the exact directory created above.
BACKUP=/srv/kronostock/backups/ksf-YYYYMMDD-HHMMSS
sudo cp -a "$BACKUP/kronostock-dry-run.service" /etc/systemd/system/kronostock-dry-run.service
sudo cp -a "$BACKUP/kronostock-dry-run.timer" /etc/systemd/system/kronostock-dry-run.timer
sudo systemctl daemon-reload
sudo systemctl enable --now kronostock-dry-run.timer

# Restore the ledger only during a maintenance window, after retaining the failed copy.
sudo systemctl stop kronostock-ksf.service
sudo test ! -e "$BACKUP/ksf_ledger.sqlite3" || sudo -u deploy sqlite3 "$BACKUP/ksf_ledger.sqlite3" ".backup '/srv/kronostock/data/ksf_ledger.sqlite3'"
sudo chown deploy:deploy /srv/kronostock/data/ksf_ledger.sqlite3
sudo chmod 600 /srv/kronostock/data/ksf_ledger.sqlite3
```
