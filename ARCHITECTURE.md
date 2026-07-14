# System Documentation — Fuel Tracker Bot

For end-user commands and setup, see [README.md](README.md). This document covers internals:
module responsibilities, data model, request flow, and deployment topology.

## Overview

A single-process Telegram bot (`python-telegram-bot`, async). No web server, no queue, no
scheduler — every feature rides on an incoming Telegram update. Storage is SQLite locally,
or Turso (libSQL over HTTP) when deployed to a host without a persistent disk.

```
Telegram update
      │
      ▼
 bot.py handlers ──► parsing.py (text → structured input)
      │                     calc.py  (fill-to-full economy math)
      │                     chart.py (matplotlib PNG rendering)
      ▼
   db.py  ──────────► sqlite3  (local)
                  └──► turso.py ──► Turso HTTP API (cloud)
      ▲
      │
sources/* (autodata, goonet, fueleconomy) — rated-economy lookups, called only from
addcar/variant-selection handlers, never from the storage or calc layers.
```

## Module reference

| Module | Responsibility |
|---|---|
| [`__main__.py`](src/fuel_tracker/__main__.py) | Entry point (`uv run fuel-tracker`) — just calls `bot.run()`. |
| [`config.py`](src/fuel_tracker/config.py) | Reads `.env` / environment: bot token, DB path, Turso creds. |
| [`bot.py`](src/fuel_tracker/bot.py) | All Telegram handlers, command wiring, and reply text/keyboard assembly. The largest module — everything Telegram-facing lives here. |
| [`db.py`](src/fuel_tracker/db.py) | Storage layer: schema, migrations, and CRUD for cars / fill-ups / per-user state. Picks SQLite or Turso per call via `_connect()`. |
| [`turso.py`](src/fuel_tracker/turso.py) | Hand-rolled libSQL HTTP (Hrana v2) client exposing a `sqlite3`-compatible `execute`/`fetchone`/`fetchall` surface, so `db.py` doesn't branch on backend. |
| [`calc.py`](src/fuel_tracker/calc.py) | Pure functions: fill-to-full leg/stats math, time-based projections, unit formatting, trend detection. No I/O — fully unit-testable. |
| [`parsing.py`](src/fuel_tracker/parsing.py) | Regex parsing of free-text fill-up lines (`14.01 @ 92184 = 1200`) and the `/addcar` argument. |
| [`chart.py`](src/fuel_tracker/chart.py) | Renders the `/chart` dashboard image (matplotlib, headless `Agg` backend) — km/L trend, rated band, liters, cost. |
| [`keyboards.py`](src/fuel_tracker/keyboards.py) | Reply/inline keyboard builders, including the variant-selection keyboard (groups by generation, hybrids last). |
| [`sources/base.py`](src/fuel_tracker/sources/base.py) | Shared `Economy`/`Variant` dataclasses and MPG↔km/L conversion used by all three source scrapers. |
| [`sources/autodata.py`](src/fuel_tracker/sources/autodata.py) | Scrapes auto-data.net: generation/variant search, then per-variant `l/100km` economy. |
| [`sources/goonet.py`](src/fuel_tracker/sources/goonet.py) | Scrapes goo-net.com (JDM catalog), matches by engine displacement + transmission. |
| [`sources/fueleconomy.py`](src/fuel_tracker/sources/fueleconomy.py) | Calls the fueleconomy.gov (US EPA) JSON API, converts MPG → km/L. |
| [`scripts/backup_turso.py`](scripts/backup_turso.py) | One-off manual dump of the live Turso tables to `backups/*.json`, meant to be run before a schema migration. |
| [`tests/test_logic.py`](tests/test_logic.py) | Offline tests (no token, no network) for `calc`, `parsing`, and `db` — run with `uv run python tests/test_logic.py`. |

## Data model

Three tables (schema in [`db.py`](src/fuel_tracker/db.py)):

- **`cars`** — `id, user_id, make, model, year, rated_l100, rated_kmpl, source_url, rated_note, goal_kmpl, created_at`. One row per car a user has added.
- **`fillups`** — `id, car_id, odometer, liters, cost, created_at, is_full`. `is_full=0` marks a partial fill-up (doesn't close a fill-to-full leg). Cascades on car deletion.
- **`user_state`** — `user_id (PK), active_car_id, units, last_reminder_sent`. One row per Telegram user: which car is active, metric/imperial display preference, and the last day a reminder was sent (dedupes to once/day).

`db.init_db()` runs `CREATE TABLE IF NOT EXISTS` plus a few `ALTER TABLE ... ADD COLUMN`
guards for columns added after the original schema (`cost`, `is_full`, `goal_kmpl`, `units`,
`last_reminder_sent`) — this is the only migration mechanism; there's no version table.

All queries scope by `user_id`/`car_id` ownership at the `db.py` layer (e.g. `get_car(id, user_id=...)`), so one bot instance serves multiple Telegram users without cross-visibility.

## Storage backend selection

`db._connect()` picks the backend per call:

```python
if config.TURSO_DATABASE_URL and config.TURSO_AUTH_TOKEN:
    return TursoConnection(...)   # HTTP-based, used on hosts with no disk (Render free tier)
conn = sqlite3.connect(path or DB_PATH)  # local file, used everywhere else
```

`TursoConnection` ([`turso.py`](src/fuel_tracker/turso.py)) implements just enough of the
`sqlite3` cursor/connection surface (`execute`, `executescript`, `fetchone`, `fetchall`,
context-manager `with`) that `db.py` never branches on which backend is active. It talks to
Turso's `/v2/pipeline` HTTP endpoint directly via `httpx` — no native `libsql` client
dependency, so the Docker image needs no extra system packages.

## Request flow — logging a fill-up

1. User sends free text (or taps **➕ Add fuel** then replies). `bot.on_text` receives it.
2. `parsing.parse_fillups` regex-parses one or more `liters @ km [= cost | @ price/L] [partial]` lines.
3. For each parsed line, `db.add_fillup` inserts a row against the user's active car.
4. `calc.compute_stats` recomputes fill-to-full legs from all fill-ups for that car (no incremental state — always derived fresh from the row set).
5. Reply text branches on whether this closed a full-to-full leg, was a partial, or was a bulk import, and attaches `after_fillup_keyboard()` (Chart / Stats / Undo shortcuts).

## Request flow — adding a car (rated-economy resolution)

1. `/addcar Toyota Corolla 2018` → `parsing.parse_addcar` splits trailing 4-digit year from make/model.
2. `sources.autodata.search_variants` looks up matching generations/variants on auto-data.net.
   - **If variants found:** user picks one via `keyboards.build_variant_keyboard` (inline buttons, grouped by generation). `on_variant_selected` then queries **all three sources concurrently** (`asyncio.gather`): `autodata.variant_economy` (exact variant), `goonet.lookup` (JDM catalog, matched by displacement+transmission), `fueleconomy.lookup` (US EPA). Priority: autodata > goonet > fueleconomy.
   - **If no variants found:** falls back to concurrently querying goonet + fueleconomy directly (no variant disambiguation possible).
3. Chosen figure is stored on the car; other sources are shown as cross-checks. If none resolve, the user can set it manually via `/setrated`.

## The overdue-fill-up reminder

There is no background scheduler (a free-tier host that sleeps would never fire one
reliably). Instead, `_reminder_check` is registered as a `TypeHandler` on **every** incoming
update (group `-1`, so it runs alongside whatever handler actually processes the update). It
recomputes `calc.time_stats` for the user's active car and sends at most one DM per day once
the projected next-fill-up date has passed. See the `ponytail:` comment in
[`bot.py`](src/fuel_tracker/bot.py) — this recomputes stats on every message, which is fine
at hobby scale but would need caching under real load.

## Deployment modes

`bot.run()` picks polling vs. webhook based on environment, and `db._connect()` picks SQLite
vs. Turso — these two choices are independent but line up in practice:

| Environment | Transport | Storage | Trigger |
|---|---|---|---|
| Local dev (`uv run fuel-tracker`) | Long polling | Local SQLite file | No `PORT`/`RENDER_EXTERNAL_URL` set |
| Docker / `docker compose` | Long polling | Local SQLite in the `fueldata` volume | Same — no port env vars set |
| Render Web Service (free) | Webhook, binds `$PORT` | Turso (no persistent disk on free tier) | `PORT` + `RENDER_EXTERNAL_URL` set by Render |
| Render Background Worker (paid, not default) | Long polling | Local SQLite on a persistent disk | Manual `render.yaml` edit (`type: web` → `type: worker` + `disk:`) |

Only one instance may poll/serve per bot token at a time (Telegram allows a single
consumer) — see the Dockerfile/README notes about not running local + container
simultaneously.

## Testing

`tests/test_logic.py` is a single self-contained script (no pytest, no fixtures) covering
`calc`, `parsing`, and `db` against a throwaway SQLite file — no network or bot token
needed. Run with `uv run python tests/test_logic.py`. There is no test coverage for
`bot.py` handlers or the `sources/*` scrapers (both need live Telegram/network state).
