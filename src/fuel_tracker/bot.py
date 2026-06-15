"""Telegram bot handlers."""

from __future__ import annotations

import asyncio
import logging
import os
from html import escape as _escape

from telegram import (
    BotCommand,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MenuButtonCommands,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import config, db, keyboards
from .calc import Stats, compute_stats
from .chart import render_chart
from .config import require_token
from .keyboards import (
    BTN_ADD,
    BTN_CARS,
    BTN_CHART,
    BTN_HELP,
    BTN_STATS,
    MAIN_KEYBOARD,
    add_car_hint_keyboard,
    after_fillup_keyboard,
)
from .parsing import parse_addcar, parse_fillups
from .sources import autodata, fueleconomy, goonet
from .sources.base import Economy

logger = logging.getLogger(__name__)
HTML = ParseMode.HTML

HELP_BODY = (
    "Track your car's <b>real-world</b> fuel economy (km/L).\n\n"
    "1. Add a car: <code>/addcar Toyota Corolla 2018</code>\n"
    "   Pick your engine variant and I'll fetch its rated economy.\n"
    "2. Log a fill-up by sending <code>14.01 @ 92184</code>\n"
    "   (also <code>14.01 liter @ 92184 km</code>). Paste many lines to import.\n"
    "   Add cost: <code>14.01 @ 92184 = 1200</code> (total) or <code>@ 85/L</code> (per litre).\n"
    "3. Tap the buttons below or use the menu for stats &amp; charts."
)


def esc(value: object) -> str:
    """Escape a dynamic value for HTML parse mode."""
    return _escape(str(value), quote=False)


def _fmt_rated(car: db.Car) -> str:
    if car.rated_kmpl:
        extra = f" ({car.rated_l100} L/100)" if car.rated_l100 else ""
        return f"{car.rated_kmpl} km/L{extra}"
    return "not set"


_BLOCKS = "▁▂▃▄▅▆▇█"


def _sparkline(values: list[float]) -> str:
    if not values:
        return ""
    lo, hi = min(values), max(values)
    if hi == lo:
        return _BLOCKS[3] * len(values)
    span = hi - lo
    return "".join(_BLOCKS[int((v - lo) / span * (len(_BLOCKS) - 1))] for v in values)


# --- shared "no active car" nudge -------------------------------------------

async def _need_car(message: Message) -> None:
    await message.reply_text(
        "You don't have an active car yet. Add one to get started 👇",
        reply_markup=add_car_hint_keyboard(),
    )


# --- rated-economy resolution & formatting ----------------------------------

def _rated_block(chosen: Economy, others: list[Economy]) -> str:
    body = (
        f"📋 Rated: <b>{chosen.km_per_l} km/L</b> ({chosen.l_per_100} L/100 km)\n"
        f"<i>Source: {esc(chosen.source)} — {esc(chosen.detail)}</i>\n"
    )
    for e in others:
        body += f"<i>Cross-check ({esc(e.source)}): {e.km_per_l} km/L</i>\n"
    return body


def _create_car(user_id: int, make: str, model: str, year: int,
                eco: Economy | None, *, source_url: str | None = None) -> int:
    car_id = db.add_car(
        user_id, make, model, year,
        rated_l100=eco.l_per_100 if eco else None,
        rated_kmpl=eco.km_per_l if eco else None,
        source_url=source_url,
        rated_note=(f"{eco.source} — {eco.detail}" if eco else None),
    )
    db.set_active(user_id, car_id)
    return car_id


# --- /start, /help ----------------------------------------------------------

async def start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    await update.message.reply_text(
        "<b>⛽ Fuel Tracker</b>\n\n" + HELP_BODY,
        parse_mode=HTML,
        reply_markup=MAIN_KEYBOARD,
    )
    if not db.list_cars(user_id):
        await update.message.reply_text(
            "Start by adding your car:", reply_markup=add_car_hint_keyboard()
        )


# --- add car ----------------------------------------------------------------

async def addcar(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _do_addcar(update.message, ctx, update.effective_user.id, " ".join(ctx.args))


async def _do_addcar(message: Message, ctx: ContextTypes.DEFAULT_TYPE,
                     user_id: int, arg: str) -> None:
    parsed = parse_addcar(arg)
    if not parsed:
        await message.reply_text(
            "Usage: <code>/addcar &lt;make&gt; &lt;model&gt; &lt;year&gt;</code>\n"
            "Example: <code>/addcar Toyota Corolla 2018</code>",
            parse_mode=HTML,
        )
        return

    existing = db.find_car(user_id, parsed.make, parsed.model, parsed.year)
    if existing:
        db.set_active(user_id, existing.id)
        await message.reply_text(
            f"You already have <b>{esc(existing.label)}</b> (#{existing.id}). Made it active.",
            parse_mode=HTML,
        )
        return

    msg = await message.reply_text(
        f"🔎 Searching variants for <b>{esc(parsed.make)} {esc(parsed.model)} {parsed.year}</b>…",
        parse_mode=HTML,
    )

    result = await autodata.search_variants(parsed.make, parsed.model, parsed.year)
    if not result or not result.variants:
        # No variant list — query the JDM catalog and the US EPA concurrently.
        jdm, epa = await asyncio.gather(
            goonet.lookup(parsed.make, parsed.model, parsed.year),
            fueleconomy.lookup(parsed.make, parsed.model, parsed.year),
        )
        chosen = jdm or epa
        car_id = _create_car(user_id, parsed.make, parsed.model, parsed.year, chosen)
        header = (f"<b>{esc(parsed.make)} {esc(parsed.model)} {parsed.year}</b> "
                  f"added (car #{car_id}) and set active.\n\n")
        if chosen:
            others = [e for e in (jdm, epa) if e and e is not chosen]
            await msg.edit_text(
                header + _rated_block(chosen, others) + "\nNow send a fill-up like "
                "<code>14.01 @ 92184</code>.",
                parse_mode=HTML,
            )
        else:
            await msg.edit_text(
                header + "⚠️ Couldn't find variants or rated economy from any source. "
                "Set it manually with <code>/setrated &lt;km/L&gt;</code>.\n\n"
                "You can still log fill-ups: <code>14.01 @ 92184</code>.",
                parse_mode=HTML,
            )
        return

    ctx.user_data["addcar"] = {
        "make": parsed.make,
        "model": parsed.model,
        "year": parsed.year,
        "variants": [(v.name, v.url, v.gen_label) for v in result.variants],
    }
    await msg.edit_text(
        f"Found <b>{esc(result.model_name)}</b>. Which one is yours?",
        parse_mode=HTML,
        reply_markup=keyboards.build_variant_keyboard(
            result.variants, parsed.make, parsed.model
        ),
    )


async def on_variant_selected(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    choice = query.data.split(":", 1)[1]

    if choice == "cancel":
        ctx.user_data.pop("addcar", None)
        await query.edit_message_text("Cancelled — no car added.")
        return

    pending = ctx.user_data.get("addcar")
    if not pending:
        await query.edit_message_text("That selection expired. Send /addcar again.")
        return
    make, model, year = pending["make"], pending["model"], pending["year"]

    if choice == "manual":
        car_id = _create_car(user_id, make, model, year, None)
        ctx.user_data.pop("addcar", None)
        await query.edit_message_text(
            f"<b>{esc(make)} {esc(model)} {year}</b> added (car #{car_id}) and set active.\n\n"
            "Set the rated economy with <code>/setrated &lt;km/L&gt;</code>, or just start "
            "logging fill-ups like <code>14.01 @ 92184</code>.",
            parse_mode=HTML,
        )
        return

    name, url, _gen = pending["variants"][int(choice)]
    full_model = f"{model} {name}"
    await query.edit_message_text(
        f"⛽ Fetching fuel economy for <b>{esc(make)} {esc(full_model)} {year}</b>…",
        parse_mode=HTML,
    )

    # All three sources concurrently. Headline priority: exact variant > JDM > US EPA.
    primary, jdm, epa = await asyncio.gather(
        autodata.variant_economy(url, detail=name),
        goonet.lookup(make, model, year, variant=name),
        fueleconomy.lookup(make, model, year),
    )
    chosen = primary or jdm or epa
    car_id = _create_car(user_id, make, full_model, year, chosen, source_url=url)
    ctx.user_data.pop("addcar", None)

    header = (f"<b>{esc(make)} {esc(full_model)} {year}</b> added "
              f"(car #{car_id}) and set active.\n\n")
    if chosen:
        others = [e for e in (primary, jdm, epa) if e and e is not chosen]
        body = _rated_block(chosen, others) + "\nNow send a fill-up like <code>14.01 @ 92184</code>."
    else:
        body = (
            "⚠️ No source had a rated figure for this variant. "
            "Set it with <code>/setrated &lt;km/L&gt;</code>.\n\n"
            "You can still log fill-ups: <code>14.01 @ 92184</code>."
        )
    await query.edit_message_text(header + body, parse_mode=HTML, disable_web_page_preview=True)


# --- cars / use / delete ----------------------------------------------------

async def _reply_cars(message: Message, user_id: int) -> None:
    all_cars = db.list_cars(user_id)
    if not all_cars:
        await _need_car(message)
        return
    active = db.get_active_car(user_id)
    active_id = active.id if active else None
    lines = ["<b>Your cars</b>"]
    for c in all_cars:
        marker = "✅" if c.id == active_id else f"<code>/use {c.id}</code>"
        lines.append(f"{marker} <b>{esc(c.label)}</b> — rated {esc(_fmt_rated(c))}")
    await message.reply_text("\n".join(lines), parse_mode=HTML)


async def cars(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply_cars(update.message, update.effective_user.id)


async def use_car(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text(
            "Usage: <code>/use &lt;car id&gt;</code> (see /cars).", parse_mode=HTML
        )
        return
    car = db.get_car(int(ctx.args[0]), user_id=user_id)
    if not car:
        await update.message.reply_text("No car with that id.")
        return
    db.set_active(user_id, car.id)
    await update.message.reply_text(
        f"Active car is now <b>{esc(car.label)}</b>.", parse_mode=HTML
    )


async def delcar(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text(
            "Usage: <code>/delcar &lt;car id&gt;</code> (see /cars).", parse_mode=HTML
        )
        return
    car = db.get_car(int(ctx.args[0]), user_id=user_id)
    if not car:
        await update.message.reply_text("No car with that id.")
        return
    n = len(db.get_fillups(car.id))
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🗑 Yes, delete", callback_data=f"del:{car.id}"),
        InlineKeyboardButton("Cancel", callback_data="del:cancel"),
    ]])
    await update.message.reply_text(
        f"⚠️ Delete <b>{esc(car.label)}</b> and its <b>{n}</b> fill-up(s)? This can't be undone.",
        parse_mode=HTML,
        reply_markup=keyboard,
    )


async def on_delete(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    choice = query.data.split(":", 1)[1]
    if choice == "cancel":
        await query.edit_message_text("Cancelled — nothing was deleted.")
        return
    car = db.delete_car(int(choice), user_id)
    if not car:
        await query.edit_message_text("That car no longer exists.")
        return
    tail = "\n\nPick a car with <code>/use &lt;id&gt;</code> (see /cars)." if db.list_cars(user_id) else ""
    await query.edit_message_text(
        f"🗑 Deleted <b>{esc(car.label)}</b> and its history.{tail}", parse_mode=HTML
    )


# --- setrated ---------------------------------------------------------------

async def setrated(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    car = db.get_active_car(user_id)
    if not car:
        await _need_car(update.message)
        return
    try:
        kmpl = float(ctx.args[0].replace(",", "."))
        if kmpl <= 0:
            raise ValueError
    except (IndexError, ValueError):
        await update.message.reply_text(
            "Usage: <code>/setrated &lt;km/L&gt;</code>, e.g. <code>/setrated 18.5</code>.",
            parse_mode=HTML,
        )
        return
    l100 = round(100 / kmpl, 2)
    db.set_rated(car.id, l100=l100, kmpl=round(kmpl, 2), note="set manually")
    await update.message.reply_text(
        f"Rated economy for <b>{esc(car.label)}</b> set to "
        f"<b>{round(kmpl, 2)} km/L</b> ({l100} L/100 km).",
        parse_mode=HTML,
    )


# --- stats / history / chart / undo -----------------------------------------

def _stats_text(car: db.Car, stats: Stats | None) -> str:
    head = f"<b>{esc(car.label)}</b>\nRated: {esc(_fmt_rated(car))}\n"
    if not stats:
        return head + "\nNot enough fill-ups yet — add at least two to see km/L."
    latest = stats.latest_leg
    cmp_line = ""
    if car.rated_kmpl and latest:
        delta = latest.km_per_l - car.rated_kmpl
        word = "above" if delta >= 0 else "below"
        cmp_line = f"  ({abs(round(delta, 2))} km/L {word} rated)"
    return (
        head
        + f"\n<b>Overall:</b> {stats.overall_km_per_l} km/L  ({stats.overall_l_per_100} L/100)\n"
        + f"Distance: {stats.total_distance:,} km over {stats.fillup_count} fill-ups\n"
        + f"Fuel used: {stats.total_fuel} L\n"
        + f"Best: {stats.best_km_per_l} km/L   Worst: {stats.worst_km_per_l} km/L\n"
        + (f"\n💰 <b>Cost:</b> {stats.total_cost:g} total · "
           f"{stats.avg_cost_per_100:g}/100 km · {stats.avg_price_per_l:g}/L\n"
           if stats.has_cost else "")
        + (f"\n<b>Latest tank:</b> {latest.km_per_l} km/L "
           f"({latest.l_per_100} L/100){cmp_line}" if latest else "")
    )


async def _reply_stats(message: Message, user_id: int) -> None:
    car = db.get_active_car(user_id)
    if not car:
        await _need_car(message)
        return
    s = compute_stats(db.get_fillups(car.id))
    await message.reply_text(_stats_text(car, s), parse_mode=HTML)


async def _reply_history(message: Message, user_id: int) -> None:
    car = db.get_active_car(user_id)
    if not car:
        await _need_car(message)
        return
    s = compute_stats(db.get_fillups(car.id))
    if not s:
        await message.reply_text(
            f"<b>{esc(car.label)}</b>: not enough fill-ups yet.", parse_mode=HTML
        )
        return
    spark = _sparkline([leg.km_per_l for leg in s.legs])
    lines = [
        f"<b>{esc(car.label)}</b> — last {min(12, len(s.legs))} legs",
        f"<code>{spark}</code>  {s.worst_km_per_l}–{s.best_km_per_l} km/L\n",
    ]
    for leg in s.legs[-12:]:
        lines.append(
            f"<code>{leg.odo_to:>7,} km  +{leg.liters:>5.2f}L</code>  →  "
            f"<b>{leg.km_per_l:.2f}</b> km/L"
        )
    await message.reply_text("\n".join(lines), parse_mode=HTML)


async def _reply_chart(message: Message, user_id: int) -> None:
    car = db.get_active_car(user_id)
    if not car:
        await _need_car(message)
        return
    s = compute_stats(db.get_fillups(car.id))
    if not s:
        await message.reply_text(
            f"<b>{esc(car.label)}</b>: need at least two fill-ups to draw a chart.",
            parse_mode=HTML,
        )
        return
    png = await asyncio.to_thread(render_chart, car, s)  # CPU-bound; keep loop free
    rated = f" · rated {car.rated_kmpl} km/L" if car.rated_kmpl else ""
    caption = (
        f"<b>{esc(car.label)}</b> — overall {s.overall_km_per_l} km/L, "
        f"latest {s.latest_leg.km_per_l} km/L{rated}"
    )
    await message.reply_photo(photo=png, caption=caption, parse_mode=HTML)


async def _reply_undo(message: Message, user_id: int) -> None:
    car = db.get_active_car(user_id)
    if not car:
        await _need_car(message)
        return
    removed = db.delete_last_fillup(car.id)
    if not removed:
        await message.reply_text("No fill-ups to remove.")
        return
    odo, liters = removed
    await message.reply_text(
        f"↩️ Removed last fill-up: {liters} L @ {odo:,} km from <b>{esc(car.label)}</b>.",
        parse_mode=HTML,
    )


async def stats(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply_stats(update.message, update.effective_user.id)


async def history(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply_history(update.message, update.effective_user.id)


async def chart(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply_chart(update.message, update.effective_user.id)


async def undo(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply_undo(update.message, update.effective_user.id)


# --- inline quick actions ---------------------------------------------------

async def on_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = update.effective_user.id
    action = query.data.split(":", 1)[1]
    if action == "chart":
        await query.answer()
        await _reply_chart(query.message, user_id)
    elif action == "stats":
        await query.answer()
        await _reply_stats(query.message, user_id)
    elif action == "undo":
        await query.answer("Removed ✓")
        await _reply_undo(query.message, user_id)
    elif action == "addcar_hint":
        await query.answer()
        ctx.user_data["await_addcar"] = True
        await query.message.reply_text(
            "Reply with your car as <b>make model year</b>, e.g. "
            "<code>Toyota Corolla 2018</code>.",
            parse_mode=HTML,
            reply_markup=ForceReply(input_field_placeholder="Toyota Corolla 2018"),
        )


async def on_noop(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()  # generation header row — not selectable


# --- free text (buttons + fill-ups) -----------------------------------------

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()

    # Persistent reply-keyboard taps.
    if text == BTN_STATS:
        return await _reply_stats(update.message, user_id)
    if text == BTN_CHART:
        return await _reply_chart(update.message, user_id)
    if text == BTN_CARS:
        return await _reply_cars(update.message, user_id)
    if text == BTN_HELP:
        return await start(update, ctx)
    if text == BTN_ADD:
        await update.message.reply_text(
            "Send your fill-up as <code>liters @ km</code>, e.g. <code>14.01 @ 92184</code>.",
            parse_mode=HTML,
            reply_markup=ForceReply(input_field_placeholder="14.01 @ 92184"),
        )
        return

    # Reply to the "Add a car" force-reply prompt.
    if ctx.user_data.pop("await_addcar", False):
        return await _do_addcar(update.message, ctx, user_id, text)

    # Otherwise: one or more "liters @ km" fill-up lines.
    parsed, bad = parse_fillups(text)
    if not parsed:
        await update.message.reply_text(
            "I didn't understand that. Send a fill-up like <code>14.01 @ 92184</code>, or /help.",
            parse_mode=HTML,
        )
        return

    car = db.get_active_car(user_id)
    if not car:
        await _need_car(update.message)
        return

    for p in parsed:
        db.add_fillup(car.id, p.odometer, p.liters, p.cost)

    s = compute_stats(db.get_fillups(car.id))
    count_note = f"✅ Logged {len(parsed)} fill-up(s) for <b>{esc(car.label)}</b>."
    if len(parsed) == 1 and s and s.latest_leg:
        leg = s.latest_leg
        rated_cmp = ""
        if car.rated_kmpl:
            delta = leg.km_per_l - car.rated_kmpl
            rated_cmp = f" — {'+' if delta >= 0 else ''}{round(delta, 2)} vs rated {car.rated_kmpl}"
        cost_line = ""
        if leg.cost is not None:
            cost_line = (f"\n💰 Cost: {leg.cost:g} "
                         f"({leg.cost_per_100:g}/100 km · {leg.price_per_l:g}/L)")
        body = (
            f"{count_note}\n\n"
            f"This tank: <b>{leg.km_per_l} km/L</b> ({leg.l_per_100} L/100) "
            f"over {leg.distance:,} km{rated_cmp}{cost_line}\n"
            f"Overall: {s.overall_km_per_l} km/L"
        )
    elif s:
        body = (f"{count_note}\nOverall now: <b>{s.overall_km_per_l} km/L</b> "
                f"over {s.total_distance:,} km.")
    else:
        body = f"{count_note}\nAdd one more fill-up to start seeing km/L."
    if bad:
        body += f"\n\n⚠️ Skipped {len(bad)} line(s) I couldn't parse."
    await update.message.reply_text(body, parse_mode=HTML, reply_markup=after_fillup_keyboard())


# --- application setup ------------------------------------------------------

_COMMANDS = [
    BotCommand("addcar", "Add a car (make model year)"),
    BotCommand("cars", "List your cars"),
    BotCommand("use", "Switch active car"),
    BotCommand("stats", "Economy stats"),
    BotCommand("chart", "km/L trend chart"),
    BotCommand("history", "Recent fill-ups"),
    BotCommand("setrated", "Set rated km/L manually"),
    BotCommand("delcar", "Delete a car"),
    BotCommand("undo", "Remove last fill-up"),
    BotCommand("help", "How to use the bot"),
]


async def _post_init(app: Application) -> None:
    """Register the command menu, descriptions and menu button (runs once at startup)."""
    await app.bot.set_my_commands(_COMMANDS)
    await app.bot.set_my_short_description(
        "Track your car's real km/L — add a car, log liters @ km, see stats & charts."
    )
    await app.bot.set_my_description(
        "Fuel Tracker logs your fill-ups and computes real-world fuel economy (km/L).\n\n"
        "Add your car, pick its engine variant, and I'll fetch the rated economy from "
        "auto-data.net, the JDM catalog and the US EPA. Then send 'liters @ km' after each "
        "fill-up for instant stats and charts."
    )
    await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())


def build_application() -> Application:
    token = require_token()
    if config.TURSO_DATABASE_URL and config.TURSO_AUTH_TOKEN:
        from .turso import http_url
        logger.info("Storage: Turso at %s", http_url(config.TURSO_DATABASE_URL))
    else:
        logger.info("Storage: local SQLite at %s", config.DB_PATH)
    db.init_db()
    app = Application.builder().token(token).post_init(_post_init).build()
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("addcar", addcar))
    app.add_handler(CallbackQueryHandler(on_variant_selected, pattern=r"^var:"))
    app.add_handler(CallbackQueryHandler(on_action, pattern=r"^act:"))
    app.add_handler(CallbackQueryHandler(on_noop, pattern=r"^noop$"))
    app.add_handler(CommandHandler("cars", cars))
    app.add_handler(CommandHandler("use", use_car))
    app.add_handler(CommandHandler("delcar", delcar))
    app.add_handler(CallbackQueryHandler(on_delete, pattern=r"^del:"))
    app.add_handler(CommandHandler("setrated", setrated))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("history", history))
    app.add_handler(CommandHandler("chart", chart))
    app.add_handler(CommandHandler("undo", undo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app


def run() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
    )
    # httpx logs every request URL at INFO — and the URL embeds the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    app = build_application()

    # On a host that provides a port + public URL (e.g. a Render web service), serve
    # via webhook so the process binds a port. Otherwise fall back to long polling.
    port = os.getenv("PORT")
    external = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("WEBHOOK_URL")
    if port and external:
        token = require_token()
        logger.info("Fuel Tracker bot starting (webhook on :%s)…", port)
        app.run_webhook(
            listen="0.0.0.0",
            port=int(port),
            url_path=token,
            webhook_url=f"{external.rstrip('/')}/{token}",
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        logger.info("Fuel Tracker bot starting (polling)…")
        app.run_polling(allowed_updates=Update.ALL_TYPES)
