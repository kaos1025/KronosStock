#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${KRONOSTOCK_APP_DIR:-/srv/agent-workspaces/KronosStock}"
cd "$APP_DIR"

source .venv/bin/activate
set -a
source ./.env
set +a

python - <<'PY'
import logging

from bot.scheduler import run_dry_run_cycle
from common.config import settings
from inference import kr_data_fetcher

logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
print("[kronostock] provider", settings.market_data_provider)
print("[kronostock] watchlist", settings.watchlist)
rows = kr_data_fetcher.fetch_watchlist(days=settings.forecast_lookback_days)
print("[kronostock] buffer_rows", rows)
result = run_dry_run_cycle(send_alert=True, persist_portfolio=True)
print("[kronostock] signals_count", len(result.signals))
print("[kronostock] orders_count", len(result.orders))
print("[kronostock] portfolio_key", result.portfolio_key)
print("[kronostock] actions", {code: signal.action.value for code, signal in result.signals.items()})
print("[kronostock] message_head")
print("\n".join(result.message.splitlines()[:16]))
PY
