#!/usr/bin/env python3
"""Octopus Energy Electricity Tracker - CLI entry point."""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv, set_key
from tabulate import tabulate

from octopus_api import OctopusAPI, OctopusAPIError, extract_product_code
from octopus_db import OctopusDB

PROJECT_DIR = Path(__file__).resolve().parent
ENV_FILE = PROJECT_DIR / ".env"

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

    # Optional: always report current demand
    if cfg.get("telegram_report_demand"):
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
