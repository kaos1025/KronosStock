# K-Semiconductor Flow Desk production one-shot runbook

This repository supplies deployment artifacts only. Do not run these commands
until production change approval and a credential review are complete. The
runner collects read-only market data for `005930` and `000660`; it never calls
an AI provider or performance settlement. Those stages report
`not_activated`. KIS observations remain `INTRADAY_ESTIMATE`, including on the
post-close schedule, because they are not an official close provider.

The dashboard service is not installed. It remains out of scope until
`KSF_READ_TOKEN` is approved; bearer authentication must not be bypassed or
weakened.

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
