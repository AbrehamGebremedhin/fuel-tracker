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

### Deploy to Render

The repo ships a [`render.yaml`](render.yaml) Blueprint that runs the bot as a **Background Worker**
(no HTTP port, since it long-polls) with a **1 GB persistent disk** mounted at `/data` for the
SQLite database.

1. Push this repo to GitHub.
2. In Render: **New → Blueprint**, pick the repo. It reads `render.yaml`.
3. Set the **`TELEGRAM_BOT_TOKEN`** environment variable (it's marked `sync: false`, so Render
   prompts for it instead of storing it in git).
4. **Apply** — Render builds the Dockerfile and starts the worker.

Notes:
- Background workers and persistent disks are **paid** features (worker from ~$7/mo + disk). A free
  *web* service won't work well: it has no disk (data is wiped on redeploy) and spins down without
  inbound HTTP traffic, which a polling bot never receives.
- The disk is mounted root-owned, which is why the container runs as root.
- Only run one instance per token — don't also run it locally while the Render worker is live.

> **Set in BotFather (manual, optional):** profile picture and the bot's display name —
> these can't be set via the API. The command menu, description and "about" text are
> registered automatically on startup.

## Using the bot

You rarely need to type commands. After `/start` you get a **persistent button keyboard**:

```
[ ➕ Add fuel ] [ 📊 Stats ]
[ 📈 Chart    ] [ 🚗 Cars  ]
[ ❓ Help                  ]
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
| `/delcar <id>` | Delete a car and its history (asks to confirm) |
| `/setrated 18.5` | Manually set rated km/L for the active car |
| `14.01 @ 92184` | Log a fill-up (also accepts `14.01 liter @ 92184 km`) |
| `14.01 @ 92184 = 1200` | Log a fill-up **with cost** (total paid). Or `@ 85/L` for price per litre |
| `/stats` | Averages & totals (incl. cost) for the active car |
| `/history` | Recent fill-ups with per-leg km/L |
| `/chart` | Dashboard: km/L trend + rated band, liters, and a cost/100 km panel when costs are logged |
| `/undo` | Delete the last fill-up |

You can paste **multiple `liters @ km` lines at once** to bulk-import your existing notes.

Cars and history are scoped to your Telegram account, so several people can share the same
bot without seeing each other's data.

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
