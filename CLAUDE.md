# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Octopus Energy electricity tracker for Raspberry Pi. Syncs consumption, unit rates, and standing charges from the Octopus Energy REST API into a local SQLite database. Provides CLI commands for querying usage/costs and a long-running Telegram bot for live demand alerts via the Octopus GraphQL API (Home Mini smart meter telemetry).

## Running

```bash
python3 octopus.py <command> [options]
```

Commands: `init`, `sync`, `demand`, `usage`, `rates`, `cost`, `export`, `bot`, `motd`

There are no tests, no linter, and no build step. This is a single-user tool deployed directly on a Raspberry Pi.

## Architecture

Three Python files, no frameworks:

- **`octopus.py`** — CLI entry point. Parses args, loads config from `.env`, dispatches to `cmd_*` functions. Contains all Telegram bot logic (long-polling, command handling), alert checking, and output formatting. Uses `tabulate` for table output.
- **`octopus_api.py`** — Stateless API client (`OctopusAPI` class). REST endpoints use HTTP basic auth via `requests.Session`. GraphQL (live demand from Home Mini) uses a separate JWT obtained via `get_graphql_token()`. Pagination is handled in `_get_paginated()` by following `next` URLs (params cleared after first request).
- **`octopus_db.py`** — SQLite layer (`OctopusDB` class). Lazy connection with WAL mode. All writes use `INSERT OR REPLACE` (upsert on primary key). Schema has 6 tables: `consumption`, `unit_rates`, `standing_charges`, `sync_log`, `alerts`, `settings`.

## Key Design Patterns

- **Config**: All config comes from `.env` via `python-dotenv`. The `init` command auto-populates meter details by calling the account API and writing back to `.env` with `set_key()`. The bot's `/threshold` and `/report` commands also write to `.env` at runtime.
- **Cost calculation**: Costs are computed at query time via SQL join (`consumption × unit_rates`). Standing charges are added per-period in Python after the SQL query, since they apply per-day not per-half-hour.
- **Tariff parsing**: `TARIFF_RE = r'^[EG]-[12]R-(.+)-[A-P]$'` extracts the product code from a tariff code (e.g., `E-1R-VAR-22-11-01-C` → `VAR-22-11-01`).
- **Alerts**: Based on live watt demand from Home Mini (GraphQL), not historical consumption. Direction-based dedup: only alerts on high↔low transitions, tracked in `alerts` table.
- **Bot state**: `settings` table stores `muted`, `pending_command`, and `telegram_update_offset` for persistence across restarts.

## Deployment

- **Cron** (`setup_cron.sh`): demand check every minute, full sync every 30 minutes, MOTD cache every minute
- **Systemd** (`setup_bot_service.sh`): user service for the long-running Telegram bot (`octopus.py bot`)
- **MOTD** (`update-motd.sh`): reads cached output from `/tmp/octobot-motd`
- **Auto-deploy** (`deploy.sh`): cron polls origin/master every 3 min, pulls if changed, syntax-checks `.py` files, restarts bot, notifies via Telegram. Rolls back on syntax errors.

## Environment

- Raspberry Pi (Python 3.10+), use `--break-system-packages` for pip installs
- Dependencies: `requests`, `python-dotenv`, `tabulate`
- Encrypted config backup in `personal/.env.gpg` (AES-256, restore with `gpg -d personal/.env.gpg > .env`)
