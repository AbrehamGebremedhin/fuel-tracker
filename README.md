# Fuel Tracker Bot

A Telegram bot to track your car's fuel consumption. Add your car (make / model / year),
**pick your exact engine variant** from a list, and the bot looks up the **rated** fuel
economy for that variant. Every time you log a `liters @ km` fill-up it computes the
**actual** km/L for that leg and your running averages. Everything is stored in SQLite.

### Rated-economy sources

When you add a car the bot searches [auto-data.net](https://www.auto-data.net) and shows the
matching engine variants as buttons. After you pick one it resolves the rated economy from
several sources, in order:

1. **[auto-data.net](https://www.auto-data.net)** — the exact variant's combined `l/100 km` (EU figures).
2. **[goo-net.com](https://www.goo-net.com)** — Japanese-domestic catalog (JC08 / 10·15 mode km/L),
   matched to your variant by engine displacement + transmission. Covers JDM models
   (Platz, Vitz, Fit, Note…) the other two don't.
3. **[fueleconomy.gov](https://www.fueleconomy.gov)** — US EPA API, combined MPG → km/L.

The chosen figure plus any other sources (as cross-checks) are shown. If none has it,
set the value yourself with `/setrated`.

## Quick start

```bash
uv sync
cp .env.example .env      # then put your bot token in it
uv run fuel-tracker
```

Get a bot token from [@BotFather](https://t.me/BotFather) and paste it into `.env` as
`TELEGRAM_BOT_TOKEN=...`.

### Run with Docker

```bash
cp .env.example .env      # put your bot token in it
docker compose up -d      # build + run in the background
docker compose logs -f    # follow logs
```

The SQLite database lives in the named volume `fueldata` (mounted at `/data`), so your cars and
history survive restarts and rebuilds. The container exposes no ports (the bot uses long polling).
To run without Compose:

```bash
docker build -t fuel-tracker .
docker run -d --name fuel-tracker --env-file .env -v fueldata:/data --restart unless-stopped fuel-tracker
```

> **Only one instance per token.** Telegram allows a single polling consumer, so stop any local
> `uv run fuel-tracker` before starting the container (otherwise you'll see a `Conflict` error).

### Deploy to Render (free)

The bot adapts to its host: with no `PORT`/`RENDER_EXTERNAL_URL` it long-polls (local/dev); on a
Render **Web Service** it serves Telegram via **webhook**, binding the port Render requires. Because
the free tier has no persistent disk, storage uses **[Turso](https://turso.tech)** (libSQL over
HTTP) so your data survives restarts/sleeps. The repo ships a [`render.yaml`](render.yaml) Blueprint
for this.

**1. Create a free Turso database** and grab its URL + token:

```bash
turso db create fuel-tracker
turso db show fuel-tracker --url        # -> libsql://fuel-tracker-<org>.turso.io
turso db tokens create fuel-tracker     # -> the auth token
```

**2. Deploy:** push this repo to GitHub → Render **New → Blueprint** → pick the repo. When prompted,
set these (all `sync: false`, so they're entered as secrets, never committed):

- `TELEGRAM_BOT_TOKEN`
- `TURSO_DATABASE_URL` — the `libsql://…` URL
- `TURSO_AUTH_TOKEN` — the token

Render builds the Dockerfile, the webhook binds the port, and the bot registers its webhook on
startup. The schema is created automatically on first run.

Notes:
- Free web services **sleep after ~15 min idle**; the first message after a sleep wakes it (~50 s
  cold start). Add an external uptime ping if you want it always warm.
- Leave `TURSO_*` unset to fall back to the local SQLite file (what local/dev and Docker use).
- **Paid alternative:** a **Background Worker** + persistent disk needs no webhook and keeps plain
  SQLite, but costs ~$7/mo. Switch `render.yaml`'s `type: web` → `type: worker` and add a `disk:`.
- Only run one instance per token — don't also run it locally while Render is live.

> **Set in BotFather (manual, optional):** profile picture and the bot's display name —
> these can't be set via the API. The command menu, description and "about" text are
> registered automatically on startup.

## Using the bot

You rarely need to type commands. After `/start` you get a **persistent button keyboard**:

```
[ ➕ Add fuel ] [ 📊 Stats    ]
[ 📈 Chart    ] [ 🚗 Cars     ]
[ 🆚 Compare  ] [ 📋 Fill-ups ]
[ 📄 Export   ] [ ❓ Help     ]
```

Tap **➕ Add fuel** and send `14.01 @ 92184`; after logging you get one-tap
**[📈 Chart] [📊 Stats] [↩️ Undo]** buttons. The `/` command menu and the Menu button list
everything too.

Optionally record what you paid — `14.01 @ 92184 = 1200` (total) or `14.01 @ 92184 @ 85/L`
(price per litre, in your own currency). Once any fill-up has a cost, `/stats` shows spend and
cost/100 km, and `/chart` adds a cost panel.

## Commands

| Command | What it does |
|---------|--------------|
| `/start` | Intro + help |
| `/addcar Toyota Corolla 2018` | Search variants; pick yours and it fetches the rated economy |
| `/cars` | List your cars / switch the active one |
| `/use <id>` | Switch active car |
| `/editcar <id> Toyota Corolla 2019` | Fix a car's make/model/year without losing its fill-up history |
| `/delcar <id>` | Delete a car and its history (asks to confirm) |
| `/setrated 18.5` | Manually set rated km/L for the active car |
| `/goal 15` | Set a km/L target for the active car (no args clears it) |
| `/units metric` \| `/units imperial` | Switch `/stats` between km/L and mpg display |
| `14.01 @ 92184` | Log a fill-up (also accepts `14.01 liter @ 92184 km`) |
| `14.01 @ 92184 = 1200` | Log a fill-up **with cost** (total paid). Or `@ 85/L` for price per litre |
| `8 @ 92184 partial` | Log a partial fill-up (didn't fill to the top) |
| `/stats` | Averages & totals (incl. cost, and goal progress) for the active car |
| `/compare` | Compare overall km/L, best/worst leg, and rated economy across all your cars |
| `/history` | Recent fill-ups with per-leg km/L |
| `/chart` | Dashboard: km/L trend + rated band, liters, and a cost/100 km panel when costs are logged |
| `/fillups` | List fill-ups with their ids (for `/delfill` and `/editfill`) |
| `/delfill <id>` | Delete a specific fill-up (asks to confirm) |
| `/editfill <id> <liters> @ <km>` | Fix a fill-up in place (cost and `partial` work too) |
| `/undo` | Delete the last fill-up |
| `/export` | Download the active car's fill-up history as CSV |

You can paste **multiple `liters @ km` lines at once** to bulk-import your existing notes.

Didn't fill to the top? Add `partial`: `8 @ 92184 partial`. Partial fills don't close a leg,
so km/L for that tank shows once you log a full fill-up.

Cars and history are scoped to your Telegram account, so several people can share the same
bot without seeing each other's data.

If a car goes quiet for a while, the bot nudges you (at most once a day) to log a fill-up
the next time you message it — no background scheduler, so it works even on hosts that sleep.

## How km/L is calculated

Uses the standard fill-to-full method: the fuel you add at a stop is the fuel consumed since
the previous stop. For each leg:

```
distance = odometer_now - odometer_previous
km/L     = distance / liters_added_now
```

Overall = (last odometer − first odometer) / (sum of all liters except the first baseline fill).

## Roadmap: Mini App dashboard (future)

A [Telegram Mini App](https://core.telegram.org/bots/webapps) would give an app-like, interactive
in-chat dashboard (reusing [fuel_tracker.html](fuel_tracker.html) + Chart.js). It's not built yet
because it needs a public **HTTPS** host for the web page (e.g. GitHub Pages / Vercel) plus a data
bridge — the bot serving a small read-only JSON endpoint keyed by the Web App's `initData`, or a
signed token in the launch URL. The current build is fully native Telegram and needs no hosting.
