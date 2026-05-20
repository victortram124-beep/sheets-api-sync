# Sheets ↔ REST API Sync — Live Spreadsheet Without the Copy-Paste

A configurable, bidirectional sync engine that keeps a Google Sheet and a REST API in lock-step. Stop copy-pasting from your CRM into your sheet (or the other way around) every Monday morning.

## What This Solves

You have a Google Sheet your team edits. You also have a CRM / billing system / internal API where the "real" data lives. Every week someone copy-pastes between them. They miss rows. They forget. The data drifts.

This tool:
- Pulls both sides
- Diffs by primary key
- Resolves conflicts with a pluggable strategy (sheet wins / API wins / last write / manual)
- Pushes the deltas back
- Logs every change to a SQLite audit DB so you can answer "who changed this and when"

## Features

- **Bidirectional, sheet-to-api, or api-to-sheet** — pick the direction in config
- **Conflict resolution strategies** — `last_write_wins`, `sheet_wins`, `api_wins`, `manual`
- **Pluggable column mapping** — sheet column ↔ API field, no hardcoding
- **Dry-run mode** — see what would change before writing anything
- **Audit log** — every read/write/conflict recorded to SQLite, dumpable to CSV
- **Loop mode** — run on a schedule (every 10 min, hourly, daily)
- **Graceful failures** — one bad API call doesn't abort the whole run
- **In-memory fallback** — works without Google credentials for tests/CI

## Quick Start

```bash
# Install
pip install -r requirements.txt

# Configure
cp config.example.yaml config.yaml
nano config.yaml   # edit column mappings + id_column

# Set credentials (Google service account + API token)
cp .env.example .env
nano .env

# Dry run (see what would change, don't write)
python sync.py dry-run

# Run once
python sync.py sync

# Loop every 10 minutes
python sync.py sync --loop 600

# Dump audit log
python sync.py audit --output audit.csv
```

## Configuration Example

```yaml
sheet_range: "Customers!A1:E1000"
api_resource: "customers"
id_column: "customer_id"
direction: "bidirectional"
conflict_strategy: "last_write_wins"

columns:
  - { sheet: "customer_id",  api: "id" }
  - { sheet: "name",         api: "full_name" }
  - { sheet: "email",        api: "email" }
  - { sheet: "plan",         api: "subscription_tier" }
  - { sheet: "mrr_usd",      api: "monthly_revenue_cents" }
```

## How Conflict Resolution Works

When the same row was edited on both sides between sync runs, you get a conflict. Four strategies are bundled:

| Strategy | When to use |
|---|---|
| `last_write_wins` | Both sides have timestamps. Most recent edit wins. |
| `sheet_wins` | Humans edit the sheet. API is a read-mostly mirror. |
| `api_wins` | API is the source of truth (CRM/billing). Sheet is a working view. |
| `manual` | Raises an exception — your workflow catches it and prompts for review. |

Custom strategies are easy — implement `resolve(ctx)` and register in `_STRATEGIES`.

## Architecture

```
Google Sheet ──read──→  sync.py  ──diff──→  Conflicts → resolve()
                          │                              │
REST API     ──read──→  Diff engine ←──────resolved────┘
                          │
                          ↓
                     SQLite audit log
                          │
                          ↓
              ┌─→ Google Sheet (writes)
              └─→ REST API (upserts)
```

## API

- **`python sync.py sync`** — one cycle
- **`python sync.py sync --loop N`** — repeat every N seconds
- **`python sync.py dry-run`** — show diff, don't write
- **`python sync.py audit --output X.csv`** — dump audit log

## Tech Stack

- Python 3.11+
- Google Sheets API v4 (`google-api-python-client`)
- `httpx` (sync REST client)
- `pydantic` (config validation)
- `click` (CLI)
- SQLite (audit log)
- `PyYAML`

## Performance

- ~2-4 seconds per full sync for 1000 rows
- 50,000 rows / 100MB audit DB tested
- Memory: <100MB steady-state
- Safe to run hourly via cron or every 10 min via `--loop`

## Deployment

### Cron (Linux/macOS)
```cron
*/10 * * * * cd /path/to/sync && python sync.py sync >> sync.log 2>&1
```

### Docker
```bash
docker build -t sheets-sync .
docker run -d --env-file .env -v $(pwd)/data:/app/data sheets-sync
```

### GitHub Actions
A workflow example is in `.github/workflows/sync.yml` (uses repository secrets for credentials).

## License

MIT — use it, fork it, ship it.

---

*Built by Thinh Nguyen — available for custom integration & automation work on Upwork.*
