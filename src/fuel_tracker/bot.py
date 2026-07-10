"""Telegram bot handlers."""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import os
from datetime import date
from html import escape as _escape

from telegram import (
    BotCommand,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
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
    TypeHandler,
    filters,
)

from . import config, db, keyboards
from .calc import (
    Stats,
    TimeStats,
    compute_stats,
    fmt_distance,
    fmt_economy,
    fmt_volume,
    should_remind,
    time_stats,
    trend_insights,
)
from .chart import render_chart
from .config import require_token
from .keyboards import (
    BTN_ADD,
    BTN_CARS,
    BTN_CHART,
    BTN_COMPARE,
    BTN_EXPORT,
    BTN_FILLUPS,
    BTN_HELP,
    BTN_STATS,
    MAIN_KEYBOARD,
    add_car_hint_keyboard,
    after_fillup_keyboard,
)
from .parsing import parse_addcar, parse_fillup_line, parse_fillups
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
    "   Didn't fill to the top? Add <code>partial</code>: <code>8 @ 92184 partial</code>.\n"
    "3. Tap the buttons below, or use any command:\n\n"
    "<b>Cars</b>\n"
    "<code>/addcar &lt;make&gt; &lt;model&gt; &lt;year&gt;</code> — add a car\n"
    "<code>/cars</code> — list your cars\n"
    "<code>/use &lt;car id&gt;</code> — switch active car\n"
    "<code>/editcar &lt;car id&gt; &lt;make&gt; &lt;model&gt; &lt;year&gt;</code> — fix a typo, "
    "keeps its history\n"
    "<code>/delcar &lt;car id&gt;</code> — delete a car and its history\n"
    "<code>/setrated &lt;km/L&gt;</code> — set rated economy manually\n"
    "<code>/goal &lt;km/L&gt;</code> — set a target economy (send with no number to clear)\n"
    "<code>/units metric|imperial</code> — km/L vs mpg display in /stats\n\n"
    "<b>Fill-ups</b>\n"
    "<code>/stats</code> — economy stats for the active car\n"
    "<code>/compare</code> — compare all your cars\n"
    "<code>/history</code> — last 12 legs with a sparkline\n"
    "<code>/chart</code> — km/L trend chart image\n"
    "<code>/fillups</code> — list fill-ups with their ids\n"
    "<code>/delfill &lt;id&gt;</code> — delete a specific fill-up\n"
    "<code>/editfill &lt;id&gt; &lt;liters&gt; @ &lt;km&gt;</code> — fix a typo in a fill-up\n"
    "<code>/export</code> — download fill-ups as a CSV file\n"
    "<code>/undo</code> — remove the last fill-up"
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


async def compare(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    all_cars = db.list_cars(user_id)
    if not all_cars:
        await _need_car(update.message)
        return
    lines = ["<b>Compare your cars</b>"]
    for c in all_cars:
        s = compute_stats(db.get_fillups(c.id))
        if not s:
            lines.append(f"<b>{esc(c.label)}</b> — not enough fill-ups yet")
            continue
        rated = f" (rated {c.rated_kmpl})" if c.rated_kmpl else ""
        lines.append(
            f"<b>{esc(c.label)}</b> — {s.overall_km_per_l} km/L overall{rated}, "
            f"best {s.best_km_per_l}, worst {s.worst_km_per_l}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=HTML)


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


# --- editcar ------------------------------------------------------------------

async def editcar(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    args = ctx.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text(
            "Usage: <code>/editcar &lt;car id&gt; &lt;make&gt; &lt;model&gt; &lt;year&gt;</code> "
            "(see /cars for ids). Fixes a typo without losing its fill-up history.",
            parse_mode=HTML,
        )
        return
    car_id = int(args[0])
    parsed = parse_addcar(" ".join(args[1:]))
    if not parsed:
        await update.message.reply_text(
            "Usage: <code>/editcar &lt;car id&gt; &lt;make&gt; &lt;model&gt; &lt;year&gt;</code>",
            parse_mode=HTML,
        )
        return
    if not db.update_car_info(car_id, user_id, parsed.make, parsed.model, parsed.year):
        await update.message.reply_text("No car with that id.")
        return
    await update.message.reply_text(
        f"Updated car #{car_id} to <b>{esc(parsed.make)} {esc(parsed.model)} "
        f"{parsed.year}</b>.",
        parse_mode=HTML,
    )


# --- goal -----------------------------------------------------------------

async def goal(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    car = db.get_active_car(user_id)
    if not car:
        await _need_car(update.message)
        return
    if not ctx.args:
        db.set_goal(car.id, user_id, None)
        await update.message.reply_text(f"Goal cleared for <b>{esc(car.label)}</b>.", parse_mode=HTML)
        return
    try:
        kmpl = float(ctx.args[0].replace(",", "."))
        if kmpl <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "Usage: <code>/goal &lt;km/L&gt;</code>, e.g. <code>/goal 16</code>. "
            "Send <code>/goal</code> with no number to clear it.",
            parse_mode=HTML,
        )
        return
    db.set_goal(car.id, user_id, round(kmpl, 2))
    await update.message.reply_text(
        f"🎯 Goal for <b>{esc(car.label)}</b> set to <b>{round(kmpl, 2)} km/L</b>.",
        parse_mode=HTML,
    )


# --- units ------------------------------------------------------------------

async def units_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    choice = (ctx.args[0].lower() if ctx.args else "")
    if choice not in ("metric", "imperial"):
        current = db.get_units(user_id)
        await update.message.reply_text(
            f"Current units: <b>{current}</b>.\n"
            "Usage: <code>/units metric</code> (km, L, km/L) or "
            "<code>/units imperial</code> (mi, gal, mpg) — affects /stats display only.",
            parse_mode=HTML,
        )
        return
    db.set_units(user_id, choice)
    await update.message.reply_text(f"Units set to <b>{choice}</b>.", parse_mode=HTML)


# --- stats / history / chart / undo -----------------------------------------

def _time_block(ts: TimeStats | None) -> str:
    if not ts:
        return ""
    cost = f" · ~{ts.monthly_cost:g}/mo" if ts.monthly_cost is not None else ""
    next_cost = f" (~{ts.next_fill_cost:g})" if ts.next_fill_cost is not None else ""
    return (
        f"\n\n⏱ <b>Over time</b> ({ts.span_days} days)\n"
        f"{ts.km_per_day:g} km/day · fill every ~{ts.days_between_fills:g} days\n"
        f"~{ts.monthly_distance:,} km/mo · ~{ts.monthly_fuel:g} L/mo{cost}\n"
        f"📅 Next fill-up ~<b>{ts.next_fill_date:%b %d}</b>{next_cost}"
    )


def _insights_block(stats: Stats, rated_kmpl: float | None) -> str:
    lines = trend_insights(stats, rated_kmpl)
    if not lines:
        return ""
    return "\n\n🔍 <b>Trend</b>\n" + "\n".join(esc(line) for line in lines)


def _stats_text(car: db.Car, stats: Stats | None, _fillups: list[tuple] | None = None,
                 units: str = "metric") -> str:
    head = f"<b>{esc(car.label)}</b>\nRated: {esc(_fmt_rated(car))}\n"
    _fillups = _fillups or []
    if not stats:
        return head + "\nNot enough fill-ups yet — add at least two to see km/L."
    latest = stats.latest_leg
    cmp_line = ""
    if car.rated_kmpl and latest:
        delta = latest.km_per_l - car.rated_kmpl
        word = "above" if delta >= 0 else "below"
        cmp_line = f"  ({abs(round(delta, 2))} km/L {word} rated)"
    goal_line = ""
    if car.goal_kmpl:
        delta = stats.overall_km_per_l - car.goal_kmpl
        word = "above" if delta >= 0 else "below"
        goal_line = f"\n🎯 Goal {fmt_economy(car.goal_kmpl, units)} — {abs(round(delta, 2))} km/L {word}\n"
    l100 = f"  ({stats.overall_l_per_100} L/100)" if units == "metric" else ""
    latest_l100 = f" ({latest.l_per_100} L/100)" if latest and units == "metric" else ""
    return (
        head
        + f"\n<b>Overall:</b> {fmt_economy(stats.overall_km_per_l, units)}{l100}\n"
        + f"Distance: {fmt_distance(stats.total_distance, units)} over {stats.fillup_count} fill-ups\n"
        + f"Fuel used: {fmt_volume(stats.total_fuel, units)}\n"
        + f"Best: {fmt_economy(stats.best_km_per_l, units)}   "
        + f"Worst: {fmt_economy(stats.worst_km_per_l, units)}\n"
        + goal_line
        + (f"\n💰 <b>Cost:</b> {stats.total_cost:g} total · "
           f"{stats.avg_cost_per_100:g}/100 km · {stats.avg_price_per_l:g}/L\n"
           if stats.has_cost else "")
        + (f"\n<b>Latest tank:</b> {fmt_economy(latest.km_per_l, units)}"
           f"{latest_l100}{cmp_line}" if latest else "")
        + _time_block(time_stats(_fillups, stats))
        + _insights_block(stats, car.rated_kmpl)
    )


async def _reply_stats(message: Message, user_id: int) -> None:
    car = db.get_active_car(user_id)
    if not car:
        await _need_car(message)
        return
    fillups = db.get_fillups(car.id)
    s = compute_stats(fillups)
    units = db.get_units(user_id)
    await message.reply_text(_stats_text(car, s, fillups, units), parse_mode=HTML)


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
    fillups = db.get_fillups(car.id)
    s = compute_stats(fillups)
    if not s:
        await message.reply_text(
            f"<b>{esc(car.label)}</b>: need at least two fill-ups to draw a chart.",
            parse_mode=HTML,
        )
        return
    ts = time_stats(fillups, s)
    png = await asyncio.to_thread(render_chart, car, s, ts)  # CPU-bound; keep loop free
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


# --- overdue-fillup nudge ----------------------------------------------------

async def _reminder_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Piggyback on any incoming update to check for an overdue fill-up.

    A time-scheduled background job wouldn't fire reliably on a free-tier host that
    sleeps when idle, but a user interaction always wakes the process — so the check
    rides along on every update instead. At most one DM per car per day.
    ponytail: recomputes stats on every update; fine at hobby scale, cache if it ever shows up.
    """
    user = update.effective_user
    if not user:
        return
    car = db.get_active_car(user.id)
    if not car:
        return
    fillups = db.get_fillups(car.id)
    s = compute_stats(fillups)
    if not s:
        return
    ts = time_stats(fillups, s)
    if not ts:
        return
    today = date.today()
    if not should_remind(ts.next_fill_date, db.get_last_reminder(user.id), today):
        return
    db.set_last_reminder(user.id, today.isoformat())
    await ctx.bot.send_message(
        chat_id=user.id,
        text=(f"⛽ Heads up — <b>{esc(car.label)}</b> looks due for a fill-up "
              f"(projected ~{ts.next_fill_date:%b %d})."),
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


# --- fillups list / delete-by-id / export ------------------------------------

def _fmt_fill(odo: int, liters: float, cost: float | None, is_full: bool) -> str:
    cost_s = f" = {cost:g}" if cost is not None else ""
    tag = " (partial)" if not is_full else ""
    return f"{liters:g} L @ {odo:,} km{cost_s}{tag}"

async def fillups_cmd(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    car = db.get_active_car(user_id)
    if not car:
        await _need_car(update.message)
        return
    rows = db.list_fillups_with_id(car.id)
    if not rows:
        await update.message.reply_text(f"<b>{esc(car.label)}</b>: no fill-ups yet.", parse_mode=HTML)
        return
    lines = [
        f"<b>{esc(car.label)}</b> — last {len(rows)} fill-up(s)",
        "<code>/delfill &lt;id&gt;</code> removes one, "
        "<code>/editfill &lt;id&gt; &lt;liters&gt; @ &lt;km&gt;</code> fixes one.\n",
    ]
    for fid, odo, liters, cost, created_at, is_full in rows:
        lines.append(f"<code>#{fid}</code>  {_fmt_fill(odo, liters, cost, is_full)} — {created_at}")
    await update.message.reply_text("\n".join(lines), parse_mode=HTML)


async def delfill(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    car = db.get_active_car(user_id)
    if not car:
        await _need_car(update.message)
        return
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text(
            "Usage: <code>/delfill &lt;id&gt;</code> (see /fillups).", parse_mode=HTML
        )
        return
    fid = int(ctx.args[0])
    row = db.get_fillup(fid, car.id)
    if not row:
        await update.message.reply_text("No fill-up with that id (see /fillups).")
        return
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🗑 Yes, delete", callback_data=f"delfill:{car.id}:{fid}"),
        InlineKeyboardButton("Cancel", callback_data="delfill:cancel"),
    ]])
    await update.message.reply_text(
        f"⚠️ Delete fill-up #{fid} — {_fmt_fill(*row)}? This can't be undone.",
        reply_markup=keyboard,
    )


async def on_delfill(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    payload = query.data.split(":", 1)[1]
    if payload == "cancel":
        await query.edit_message_text("Cancelled — nothing was deleted.")
        return
    car_id_s, fid_s = payload.split(":")
    car = db.get_car(int(car_id_s), user_id=user_id)
    if not car:
        await query.edit_message_text("That car no longer exists.")
        return
    removed = db.delete_fillup(int(fid_s), car.id)
    if not removed:
        await query.edit_message_text("That fill-up no longer exists.")
        return
    odo, liters = removed
    await query.edit_message_text(f"🗑 Deleted fill-up: {liters:g} L @ {odo:,} km.")


async def editfill(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    car = db.get_active_car(user_id)
    if not car:
        await _need_car(update.message)
        return
    args = ctx.args or []
    usage = (
        "Usage: <code>/editfill &lt;id&gt; &lt;liters&gt; @ &lt;km&gt;</code> (see /fillups for ids).\n"
        "Example: <code>/editfill 42 14.01 @ 92184</code> — cost and "
        "<code>partial</code> work too, same as when logging."
    )
    parsed = parse_fillup_line(" ".join(args[1:])) if args and args[0].isdigit() else None
    if not parsed:
        await update.message.reply_text(usage, parse_mode=HTML)
        return
    fid = int(args[0])
    old = db.get_fillup(fid, car.id)
    if not old:
        await update.message.reply_text("No fill-up with that id (see /fillups).")
        return
    db.update_fillup(fid, car.id, parsed.odometer, parsed.liters,
                     parsed.cost, parsed.is_full)
    await update.message.reply_text(
        f"✏️ Fill-up #{fid} updated:\n"
        f"was:  {_fmt_fill(*old)}\n"
        f"now:  {_fmt_fill(parsed.odometer, parsed.liters, parsed.cost, parsed.is_full)}",
        parse_mode=HTML,
        reply_markup=after_fillup_keyboard(),
    )


async def export_cmd(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    car = db.get_active_car(user_id)
    if not car:
        await _need_car(update.message)
        return
    rows = sorted(db.list_fillups_with_id(car.id, limit=None))
    if not rows:
        await update.message.reply_text(f"<b>{esc(car.label)}</b>: no fill-ups yet.", parse_mode=HTML)
        return
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "odometer_km", "liters", "cost", "is_full", "created_at"])
    for fid, odo, liters, cost, created_at, is_full in rows:
        writer.writerow([fid, odo, liters, cost if cost is not None else "", int(is_full), created_at])
    filename = f"{car.label.replace(' ', '_')}_fillups.csv"
    await update.message.reply_document(
        document=InputFile(io.BytesIO(buf.getvalue().encode("utf-8")), filename=filename)
    )


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
    if text == BTN_COMPARE:
        return await compare(update, ctx)
    if text == BTN_FILLUPS:
        return await fillups_cmd(update, ctx)
    if text == BTN_EXPORT:
        return await export_cmd(update, ctx)
    if text == BTN_HELP:
        return await start(update, ctx)
    if text == BTN_ADD:
        await update.message.reply_text(
            "Send your fill-up as <code>liters @ km</code>, e.g. <code>14.01 @ 92184</code>.\n"
            "Didn't fill to the top? Add <code>partial</code>, e.g. "
            "<code>8 @ 92184 partial</code>.",
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
        db.add_fillup(car.id, p.odometer, p.liters, p.cost, p.is_full)

    s = compute_stats(db.get_fillups(car.id))
    count_note = f"✅ Logged {len(parsed)} fill-up(s) for <b>{esc(car.label)}</b>."
    just_added = parsed[0]
    # A partial fill doesn't close a leg, so s.latest_leg (if any) is stale — it belongs
    # to an earlier full-to-full stretch, not the fill-up just logged.
    if len(parsed) == 1 and not just_added.is_full:
        body = (f"{count_note}\n⛽ Marked as a partial fill — economy for this tank will "
                f"show once you log a full fill-up.")
    elif len(parsed) == 1 and s and s.latest_leg and s.latest_leg.odo_to == just_added.odometer:
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
    BotCommand("compare", "Compare your cars"),
    BotCommand("setrated", "Set rated km/L manually"),
    BotCommand("editcar", "Fix a car's make/model/year"),
    BotCommand("delcar", "Delete a car"),
    BotCommand("undo", "Remove last fill-up"),
    BotCommand("fillups", "List fill-ups with their ids"),
    BotCommand("delfill", "Delete a specific fill-up"),
    BotCommand("editfill", "Edit a specific fill-up"),
    BotCommand("export", "Export fill-ups as CSV"),
    BotCommand("goal", "Set a km/L target"),
    BotCommand("units", "metric/imperial display"),
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
    app.add_handler(CommandHandler("compare", compare))
    app.add_handler(CommandHandler("use", use_car))
    app.add_handler(CommandHandler("delcar", delcar))
    app.add_handler(CallbackQueryHandler(on_delete, pattern=r"^del:"))
    app.add_handler(CommandHandler("setrated", setrated))
    app.add_handler(CommandHandler("editcar", editcar))
    app.add_handler(CommandHandler("goal", goal))
    app.add_handler(CommandHandler("units", units_cmd))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("history", history))
    app.add_handler(CommandHandler("chart", chart))
    app.add_handler(CommandHandler("undo", undo))
    app.add_handler(CommandHandler("fillups", fillups_cmd))
    app.add_handler(CommandHandler("delfill", delfill))
    app.add_handler(CommandHandler("editfill", editfill))
    app.add_handler(CallbackQueryHandler(on_delfill, pattern=r"^delfill:"))
    app.add_handler(CommandHandler("export", export_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(TypeHandler(Update, _reminder_check), group=-1)
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
            # Free tier sleeps after inactivity. The command that wakes the service
            # sits in Telegram's pending queue during the ~30-60s cold start; dropping
            # it here would lose exactly that first command. Keep it so it's delivered
            # once the webhook server is up.
            drop_pending_updates=False,
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        logger.info("Fuel Tracker bot starting (polling)…")
        app.run_polling(allowed_updates=Update.ALL_TYPES)
