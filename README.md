# Octopus Energy Electricity Tracker

A command-line tool for tracking electricity consumption, rates, and costs from the [Octopus Energy API](https://developer.octopus.energy/docs/api/). Stores data locally in SQLite and optionally sends usage alerts via Telegram.

Built for a Raspberry Pi running on cron, but works anywhere with Python 3.10+.

## Setup

```bash
pip install requests python-dotenv tabulate
```

Copy `.env.example` to `.env` and fill in your API key and account number:

```bash
cp .env.example .env
```

Then auto-populate your meter details:

```bash
python3 octopus.py init
```

This writes your MPAN, serial number, and tariff code to `.env`.

## Usage

### Sync data from the API

```bash
python3 octopus.py sync              # smart resume from last sync, or 30 days
python3 octopus.py sync --days 7     # last 7 days
python3 octopus.py sync -q           # quiet mode (cron-friendly, errors only)
```

### View consumption

```bash
python3 octopus.py usage --days 7               # half-hourly readings
python3 octopus.py usage --days 30 --group day   # daily totals
python3 octopus.py usage --group month           # monthly totals
```

### View unit rates

```bash
python3 octopus.py rates --days 7
```

### Calculate costs

```bash
python3 octopus.py cost --days 7                 # daily breakdown
python3 octopus.py cost --days 30 --group week   # weekly totals
python3 octopus.py cost --json                   # JSON output
```

### Export all data

```bash
python3 octopus.py export -o backup.json
```

## Telegram Alerts

Get notified when your daily usage crosses a threshold (e.g. shed heater kicking in).

1. Create a bot with [@BotFather](https://t.me/BotFather) on Telegram
2. Message your bot, then grab your chat ID from `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. Add to `.env`:

```
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
TELEGRAM_CHAT_ID=123456789
OCTOPUS_ALERT_THRESHOLD=1.0
```

Alerts fire at the end of each `sync` when the two most recent complete days cross the threshold in either direction (high or low). Duplicate alerts for the same direction are suppressed.

## Cron

Run a daily sync at 6am:

```cron
0 6 * * * python3 /home/jay/octopus/octopus.py sync -q >> /home/jay/octopus/cron.log 2>&1
```

## Project Structure

| File | Purpose |
|------|---------|
| `octopus.py` | CLI entry point, commands, output formatting, alert logic |
| `octopus_api.py` | Octopus Energy REST API client with pagination |
| `octopus_db.py` | SQLite schema, upsert/query operations, alert tracking |
| `.env` | Your config (API key, account, meter details, Telegram) |
| `.env.example` | Template with placeholder values |
