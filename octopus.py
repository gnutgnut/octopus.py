#!/usr/bin/env python3
"""Octopus Energy Electricity Tracker - CLI entry point."""

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv, set_key
from tabulate import tabulate

from octopus_api import OctopusAPI, OctopusAPIError, extract_product_code
from octopus_db import OctopusDB

PROJECT_DIR = Path(__file__).resolve().parent
ENV_FILE = PROJECT_DIR / ".env"

try:
    GIT_SHA = subprocess.check_output(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=PROJECT_DIR, stderr=subprocess.DEVNULL
    ).decode().strip()
except Exception:
    GIT_SHA = "unknown"

log = logging.getLogger("octopus")


# ── Helpers ──────────────────────────────────────────────────────────

def setup_logging(quiet: bool = False, level: str = "INFO"):
    fmt = "%(asctime)s %(levelname)s %(message)s"
    if quiet:
        # Cron mode: only warnings/errors to stderr
        logging.basicConfig(level=logging.WARNING, format=fmt, stream=sys.stderr)
    else:
        logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO),
                            format=fmt, stream=sys.stderr)


def load_config(db_override: str | None = None) -> dict:
    load_dotenv(ENV_FILE)
    cfg = {
        "api_key": os.getenv("OCTOPUS_API_KEY"),
        "account": os.getenv("OCTOPUS_ACCOUNT"),
        "mpan": os.getenv("OCTOPUS_MPAN"),
        "serial": os.getenv("OCTOPUS_SERIAL"),
        "tariff_code": os.getenv("OCTOPUS_TARIFF_CODE"),
        "db_path": db_override or os.getenv("OCTOPUS_DB_PATH",
                                             str(PROJECT_DIR / "octopus.db")),
        "log_level": os.getenv("OCTOPUS_LOG_LEVEL", "INFO"),
        "telegram_bot_token": os.getenv("TELEGRAM_BOT_TOKEN"),
        "telegram_chat_id": os.getenv("TELEGRAM_CHAT_ID"),
        "alert_threshold": float(os.getenv("OCTOPUS_ALERT_THRESHOLD", "1000")),
        "device_id": os.getenv("OCTOPUS_DEVICE_ID"),
        "telegram_report_demand": os.getenv("TELEGRAM_REPORT_DEMAND", "true").lower() == "true",
        "report_demand_threshold": float(os.getenv("OCTOPUS_REPORT_DEMAND_THRESHOLD", "2000")),
    }
    return cfg


def require_config(cfg: dict, *keys: str):
    missing = [k for k in keys if not cfg.get(k)]
    if missing:
        names = ", ".join(f"OCTOPUS_{k.upper()}" for k in missing)
        print(f"Error: Missing config: {names}", file=sys.stderr)
        print("Run 'octopus.py init' or set values in .env", file=sys.stderr)
        sys.exit(1)


def days_ago(n: int) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=n)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def output_result(data, headers: list[str] | None = None, as_json: bool = False):
    """Print data as a table or JSON."""
    if as_json:
        print(json.dumps(data, indent=2, default=str))
    elif isinstance(data, list) and data:
        if headers:
            rows = [[row.get(h, "") for h in headers] for row in data]
            print(tabulate(rows, headers=headers, tablefmt="simple",
                           floatfmt=".4f"))
        else:
            print(tabulate([d.values() for d in data],
                           headers=data[0].keys(), tablefmt="simple",
                           floatfmt=".4f"))
    elif isinstance(data, dict):
        for k, v in data.items():
            print(f"  {k}: {v}")
    else:
        print("No data.")


# ── Telegram alerts ──────────────────────────────────────────────────

def send_telegram(token: str, chat_id: str, message: str):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, json={"chat_id": chat_id, "text": message},
                         timeout=10)
    resp.raise_for_status()
    log.info("Telegram message sent to chat %s", chat_id)


def check_usage_alerts(cfg: dict, db: OctopusDB, api: OctopusAPI):
    """Check live demand from Home Mini and alert on threshold crossings."""
    tg_token = cfg.get("telegram_bot_token")
    chat_id = cfg.get("telegram_chat_id")
    if not tg_token or not chat_id:
        return

    device_id = cfg.get("device_id")
    if not device_id:
        log.debug("No OCTOPUS_DEVICE_ID configured, skipping live demand check")
        return

    muted = db.get_setting("muted") == "true"
    threshold = cfg["alert_threshold"]

    # Fetch live demand via GraphQL
    try:
        gql_token = api.get_graphql_token()
        reading = api.get_live_demand(gql_token, device_id)
    except (OctopusAPIError, requests.RequestException) as e:
        log.error("Failed to get live demand: %s", e)
        return

    if reading is None:
        log.debug("No live telemetry data available")
        return

    demand = float(reading["demand"])
    read_at = reading["readAt"]
    log.info("Live demand: %.0fW at %s", demand, read_at[:16])

    if muted:
        log.info("Notifications muted, skipping Telegram sends")
        return

    # Report demand when above reporting threshold
    if cfg.get("telegram_report_demand") and demand >= cfg["report_demand_threshold"]:
        warn = "\u26a0\ufe0f " if demand >= 3000 else ""
        status_msg = f"{warn}Demand: {demand:.0f}W at {read_at[:16]}"
        try:
            send_telegram(tg_token, chat_id, status_msg)
        except requests.RequestException as e:
            log.error("Failed to send demand status: %s", e)

    # Determine current state
    direction = "high" if demand >= threshold else "low"

    # Skip if the last alert was already this direction
    last = db.last_alert()
    if last and last["direction"] == direction:
        log.debug("Skipping duplicate %s alert", direction)
        return

    prev_demand = last["curr_kwh"] if last else 0.0

    # Build and send transition alert
    arrow = "\u2b06\ufe0f" if direction == "high" else "\u2b07\ufe0f"
    label = "High" if direction == "high" else "Low"
    msg = (f"{arrow} {label} usage alert\n"
           f"Demand: {demand:.0f}W at {read_at[:16]}\n"
           f"Threshold: {threshold:.0f}W")

    try:
        send_telegram(tg_token, chat_id, msg)
        db.log_alert(direction, prev_demand, demand, threshold)
    except requests.RequestException as e:
        log.error("Failed to send Telegram alert: %s", e)


# ── Telegram bot ──────────────────────────────────────────────────────

def get_telegram_updates(token: str, offset: int | None = None,
                         timeout: int = 30) -> list[dict]:
    """Long-poll the Telegram getUpdates endpoint."""
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    resp = requests.get(url, params=params, timeout=timeout + 10)
    resp.raise_for_status()
    data = resp.json()
    return data.get("result", [])


def handle_bot_command(cfg: dict, db: OctopusDB, text: str, chat_id: str,
                       tg_token: str):
    """Parse and dispatch a bot command, replying via Telegram."""
    def reply(msg: str):
        send_telegram(tg_token, chat_id, f"\U0001F419 {msg}")

    parts = text.strip().split(None, 1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    # Strip @botname suffix from commands (e.g. /status@MyBot)
    if "@" in cmd:
        cmd = cmd.split("@")[0]
    env_path = str(ENV_FILE)
    if db.get_setting("pending_command"):
        db.set_setting("pending_command", "")

    if cmd == "/threshold":
        if not arg:
            db.set_setting("pending_command", "threshold")
            reply("Enter threshold in watts:")
            return
        try:
            watts = int(arg)
        except ValueError:
            reply("Invalid number. Usage: /threshold <watts>")
            return
        set_key(env_path, "OCTOPUS_ALERT_THRESHOLD", str(watts))
        cfg["alert_threshold"] = float(watts)
        reply(f"Alert threshold set to {watts}W")

    elif cmd == "/report":
        if not arg:
            db.set_setting("pending_command", "report")
            reply("Enter threshold in watts (or 'off'):")
            return
        if arg.lower() == "off":
            set_key(env_path, "TELEGRAM_REPORT_DEMAND", "false")
            cfg["telegram_report_demand"] = False
            reply("Demand reporting disabled")
        else:
            try:
                watts = int(arg)
            except ValueError:
                reply("Invalid value. Usage: /report <watts|off>")
                return
            set_key(env_path, "OCTOPUS_REPORT_DEMAND_THRESHOLD", str(watts))
            set_key(env_path, "TELEGRAM_REPORT_DEMAND", "true")
            cfg["report_demand_threshold"] = float(watts)
            cfg["telegram_report_demand"] = True
            reply(f"Demand reporting enabled at {watts}W threshold")

    elif cmd == "/mute":
        db.set_setting("muted", "true")
        reply("Notifications muted")

    elif cmd == "/unmute":
        db.set_setting("muted", "false")
        reply("Notifications resumed")

    elif cmd == "/status":
        muted = db.get_setting("muted") == "true"
        report = "on" if cfg.get("telegram_report_demand") else "off"
        lines = [
            "Current config:",
            f"  Alert threshold: {cfg['alert_threshold']:.0f}W",
            f"  Report demand: {report}",
            f"  Report threshold: {cfg['report_demand_threshold']:.0f}W",
            f"  Muted: {'yes' if muted else 'no'}",
        ]
        # Try to include latest demand reading
        device_id = cfg.get("device_id")
        if device_id and cfg.get("api_key"):
            try:
                api = OctopusAPI(cfg["api_key"])
                gql_token = api.get_graphql_token()
                reading = api.get_live_demand(gql_token, device_id)
                if reading:
                    lines.append(
                        f"  Live demand: {float(reading['demand']):.0f}W "
                        f"at {reading['readAt'][:16]}")
            except Exception as e:
                log.warning("Failed to fetch demand for /status: %s", e)
        # Cron jobs
        try:
            crontab = subprocess.check_output(
                ["crontab", "-l"], stderr=subprocess.DEVNULL
            ).decode()
            cron_lines = [l.strip() for l in crontab.splitlines()
                          if "octopus.py" in l and not l.startswith("#")]
            if cron_lines:
                lines.append("Cron jobs:")
                for cl in cron_lines:
                    lines.append(f"  {cl}")
        except Exception:
            pass
        lines.append(f"Version: {GIT_SHA}")
        reply("\n".join(lines))

    elif cmd == "/help":
        reply("\n".join([
            "Available commands:",
            "  /threshold <watts> - set alert threshold",
            "  /report <watts|off> - set demand report threshold or disable",
            "  /mute - silence all notifications",
            "  /unmute - resume notifications",
            "  /status - show current config + live demand",
            "  /help - show this message",
        ]))

    else:
        reply("Unknown command. Send /help for usage.")


def cmd_bot(cfg: dict, args):
    """Run long-polling Telegram bot to accept commands."""
    tg_token = cfg.get("telegram_bot_token")
    chat_id = cfg.get("telegram_chat_id")
    if not tg_token or not chat_id:
        print("Error: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in .env",
              file=sys.stderr)
        sys.exit(1)

    # Register command menu with Telegram
    bot_commands = [
        {"command": "threshold", "description": "Set alert threshold (watts)"},
        {"command": "report", "description": "Set demand report threshold or disable"},
        {"command": "mute", "description": "Silence all notifications"},
        {"command": "unmute", "description": "Resume notifications"},
        {"command": "status", "description": "Show current config + live demand"},
        {"command": "help", "description": "List available commands"},
    ]
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{tg_token}/setMyCommands",
            json={"commands": bot_commands}, timeout=10)
        resp.raise_for_status()
        log.info("Registered bot command menu with Telegram")
    except requests.RequestException as e:
        log.warning("Failed to register bot commands: %s", e)

    db = OctopusDB(cfg["db_path"])
    db.init_schema()

    # Resume from last processed update
    saved_offset = db.get_setting("telegram_update_offset")
    offset = int(saved_offset) if saved_offset else None

    # Convert SIGTERM to SystemExit so the finally block runs
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    log.info("Bot started, listening for commands (offset=%s)", offset)
    print("Bot started. Listening for Telegram commands... (Ctrl+C to stop)")

    try:
        send_telegram(tg_token, chat_id,
                      f"\U0001F419 Bot online ({GIT_SHA})")
    except requests.RequestException as e:
        log.warning("Failed to send startup banner: %s", e)

    try:
        while True:
            try:
                updates = get_telegram_updates(tg_token, offset=offset)
            except requests.RequestException as e:
                log.error("Failed to get updates: %s", e)
                time.sleep(5)
                continue

            for update in updates:
                update_id = update["update_id"]
                offset = update_id + 1

                msg = update.get("message", {})
                msg_chat_id = str(msg.get("chat", {}).get("id", ""))
                text = msg.get("text", "")

                if msg_chat_id != chat_id:
                    log.warning("Ignoring message from unauthorized chat %s",
                                msg_chat_id)
                    continue

                if not text:
                    continue

                # Check for pending command awaiting an argument
                if not text.startswith("/"):
                    pending = db.get_setting("pending_command")
                    if pending and pending in ("threshold", "report"):
                        db.set_setting("pending_command", "")
                        text = f"/{pending} {text}"
                    else:
                        continue

                log.info("Received command: %s", text)
                try:
                    handle_bot_command(cfg, db, text, chat_id, tg_token)
                except requests.RequestException as e:
                    log.error("Failed to handle command: %s", e)

            if updates:
                db.set_setting("telegram_update_offset", str(offset))
    finally:
        try:
            send_telegram(tg_token, chat_id, "\U0001F419 Bot shutting down")
        except Exception:
            pass
        db.close()


# ── Commands ─────────────────────────────────────────────────────────

def cmd_init(cfg: dict, args):
    """Fetch account details and write MPAN/serial/tariff to .env."""
    require_config(cfg, "api_key", "account")
    api = OctopusAPI(cfg["api_key"])

    print(f"Fetching account details for {cfg['account']}...")
    details = api.get_electricity_details(cfg["account"])

    print(f"  MPAN:   {details['mpan']}")
    print(f"  Serial: {details['serial']}")
    print(f"  Tariff: {details['tariff_code']}")

    # Write to .env
    env_path = str(ENV_FILE)
    set_key(env_path, "OCTOPUS_MPAN", details["mpan"])
    set_key(env_path, "OCTOPUS_SERIAL", details["serial"])
    set_key(env_path, "OCTOPUS_TARIFF_CODE", details["tariff_code"])
    print(f"\nWritten to {env_path}")


def cmd_sync(cfg: dict, args):
    """Fetch consumption, rates, and standing charges from API."""
    require_config(cfg, "api_key", "mpan", "serial", "tariff_code")
    api = OctopusAPI(cfg["api_key"])
    db = OctopusDB(cfg["db_path"])
    db.init_schema()

    # Determine date range
    if args.from_date:
        period_from = args.from_date
    elif args.days:
        period_from = days_ago(args.days)
    else:
        # Smart resume: check last sync, default 30 days
        last = db.last_sync("consumption")
        if last and last.get("period_to"):
            period_from = last["period_to"]
            log.info("Resuming from last sync: %s", period_from)
        else:
            period_from = days_ago(30)

    period_to = args.to_date if args.to_date else now_iso()

    if not args.quiet:
        print(f"Syncing from {period_from} to {period_to}")

    # Consumption
    if not args.quiet:
        print("  Fetching consumption...")
    records = api.get_consumption(cfg["mpan"], cfg["serial"],
                                  period_from, period_to)
    count = db.upsert_consumption(records)
    db.log_sync("consumption", period_from, period_to, count)
    if not args.quiet:
        print(f"  -> {count} consumption records")

    # Unit rates
    if not args.quiet:
        print("  Fetching unit rates...")
    rates = api.get_unit_rates(cfg["tariff_code"], period_from, period_to)
    rcount = db.upsert_unit_rates(rates)
    db.log_sync("unit_rates", period_from, period_to, rcount)
    if not args.quiet:
        print(f"  -> {rcount} unit rate records")

    # Standing charges
    if not args.quiet:
        print("  Fetching standing charges...")
    charges = api.get_standing_charges(cfg["tariff_code"], period_from, period_to)
    scount = db.upsert_standing_charges(charges)
    db.log_sync("standing_charges", period_from, period_to, scount)
    if not args.quiet:
        print(f"  -> {scount} standing charge records")

    if not args.quiet:
        print("Sync complete.")

    check_usage_alerts(cfg, db, api)

    db.close()


def cmd_demand(cfg: dict, args):
    """Check live demand from Home Mini and send alerts."""
    require_config(cfg, "api_key")
    api = OctopusAPI(cfg["api_key"])
    db = OctopusDB(cfg["db_path"])
    db.init_schema()

    check_usage_alerts(cfg, db, api)

    db.close()


def cmd_usage(cfg: dict, args):
    """Show consumption data from DB."""
    db = OctopusDB(cfg["db_path"])
    db.init_schema()

    days = args.days or 7
    period_from = days_ago(days)
    period_to = now_iso()

    if args.group:
        data = db.get_consumption_grouped(period_from, period_to, args.group)
        headers = ["period", "total_kwh", "readings"]
    else:
        data = db.get_consumption(period_from, period_to)
        headers = ["interval_start", "interval_end", "kwh"]

    if not data:
        print("No consumption data. Run 'sync' first.")
    else:
        output_result(data, headers=headers, as_json=args.json)

    db.close()


def cmd_rates(cfg: dict, args):
    """Show unit rates from DB."""
    db = OctopusDB(cfg["db_path"])
    db.init_schema()

    days = args.days or 7
    period_from = days_ago(days)
    period_to = now_iso()

    data = db.get_unit_rates(period_from, period_to)
    if not data:
        print("No rate data. Run 'sync' first.")
    else:
        headers = ["valid_from", "valid_to", "value_exc_vat", "value_inc_vat"]
        output_result(data, headers=headers, as_json=args.json)

    db.close()


def cmd_cost(cfg: dict, args):
    """Calculate costs: consumption x unit rates + standing charges."""
    db = OctopusDB(cfg["db_path"])
    db.init_schema()

    days = args.days or 7
    group = args.group or "day"
    period_from = days_ago(days)
    period_to = now_iso()

    cost_data = db.get_cost_data(period_from, period_to, group)
    if not cost_data:
        print("No cost data. Run 'sync' first.")
        db.close()
        return

    # Add standing charges per period
    for row in cost_data:
        period = row["period"]
        if group == "day":
            sc = db.get_standing_charge_for_date(period)
            row["standing_pence"] = sc or 0.0
        elif group == "week":
            # Approximate: 7 days per week
            sc = db.get_standing_charge_for_date(period_from[:10])
            row["standing_pence"] = (sc or 0.0) * 7
        elif group == "month":
            # Use first of month, approximate 30 days
            sc = db.get_standing_charge_for_date(period + "-01")
            row["standing_pence"] = (sc or 0.0) * 30

        usage_cost = row["usage_cost_pence"] or 0.0
        standing = row["standing_pence"]
        row["total_pence"] = usage_cost + standing
        row["total_gbp"] = row["total_pence"] / 100.0

    headers = ["period", "total_kwh", "usage_cost_pence", "standing_pence",
               "total_pence", "total_gbp"]

    if args.json:
        output_result(cost_data, as_json=True)
    else:
        # Format for display
        display_data = []
        for row in cost_data:
            display_data.append({
                "period": row["period"],
                "kWh": f"{row['total_kwh']:.2f}",
                "usage (p)": f"{row['usage_cost_pence'] or 0:.2f}",
                "standing (p)": f"{row['standing_pence']:.2f}",
                "total (p)": f"{row['total_pence']:.2f}",
                "total (£)": f"{row['total_gbp']:.2f}",
            })
        print(tabulate([d.values() for d in display_data],
                        headers=display_data[0].keys(), tablefmt="simple"))

    db.close()


def cmd_export(cfg: dict, args):
    """Export all DB data to JSON."""
    db = OctopusDB(cfg["db_path"])
    db.init_schema()

    data = db.export_all()
    data["exported_at"] = now_iso()

    output_path = args.output or "octopus_export.json"
    with open(output_path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    print(f"Exported to {output_path}")
    print(f"  consumption:      {len(data['consumption'])} records")
    print(f"  unit_rates:       {len(data['unit_rates'])} records")
    print(f"  standing_charges: {len(data['standing_charges'])} records")
    print(f"  sync_log:         {len(data['sync_log'])} entries")

    db.close()


# ── CLI argument parser ──────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="octopus.py",
        description="Octopus Energy Electricity Tracker",
    )
    parser.add_argument("--quiet", "-q", action="store_true",
                        help="Suppress non-error output (cron-friendly)")
    parser.add_argument("--json", "-j", action="store_true",
                        help="Output as JSON")
    parser.add_argument("--db", type=str, default=None,
                        help="Path to SQLite database")

    sub = parser.add_subparsers(dest="command", help="Available commands")

    # init
    sub.add_parser("init", help="Fetch account details, populate .env")

    # sync
    sp_sync = sub.add_parser("sync", help="Fetch data from API, store in DB")
    sp_sync.add_argument("--days", type=int, default=None,
                         help="Number of days to sync (default: smart resume or 30)")
    sp_sync.add_argument("--from", dest="from_date", type=str,
                         help="Start date (ISO 8601)")
    sp_sync.add_argument("--to", dest="to_date", type=str,
                         help="End date (ISO 8601)")

    # demand
    sub.add_parser("demand", help="Check live demand and send alerts (lightweight, safe for 1-min cron)")

    # usage
    sp_usage = sub.add_parser("usage", help="Show consumption data")
    sp_usage.add_argument("--days", type=int, default=None,
                          help="Number of days (default: 7)")
    sp_usage.add_argument("--group", choices=["day", "week", "month"],
                          help="Group by period")

    # rates
    sp_rates = sub.add_parser("rates", help="Show unit rates")
    sp_rates.add_argument("--days", type=int, default=None,
                          help="Number of days (default: 7)")

    # cost
    sp_cost = sub.add_parser("cost", help="Calculate costs")
    sp_cost.add_argument("--days", type=int, default=None,
                         help="Number of days (default: 7)")
    sp_cost.add_argument("--group", choices=["day", "week", "month"],
                         default=None, help="Group by period (default: day)")

    # export
    sp_export = sub.add_parser("export", help="Export all data to JSON")
    sp_export.add_argument("--output", "-o", type=str,
                           help="Output file path (default: octopus_export.json)")

    # bot
    sub.add_parser("bot", help="Run Telegram bot listener (long-running)")

    return parser


# ── Main ─────────────────────────────────────────────────────────────

def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    cfg = load_config(db_override=args.db)
    setup_logging(quiet=args.quiet, level=cfg["log_level"])

    # Propagate --quiet and --json to args for commands that need them
    if not hasattr(args, "quiet"):
        args.quiet = False
    if not hasattr(args, "json"):
        args.json = False

    commands = {
        "init": cmd_init,
        "sync": cmd_sync,
        "demand": cmd_demand,
        "usage": cmd_usage,
        "rates": cmd_rates,
        "cost": cmd_cost,
        "export": cmd_export,
        "bot": cmd_bot,
    }

    try:
        commands[args.command](cfg, args)
    except OctopusAPIError as e:
        log.error("API error: %s", e)
        if not args.quiet:
            print(f"API error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        log.error("Unexpected error: %s", e, exc_info=True)
        if not args.quiet:
            print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
