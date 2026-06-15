"""Telegram bot handlers."""

from __future__ import annotations

import asyncio
import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import db
from .calc import Stats, compute_stats
from .chart import render_chart
from .config import require_token
from .parsing import parse_addcar, parse_fillups
from .sources import autodata, fueleconomy, goonet
from .sources.base import Economy

logger = logging.getLogger(__name__)

HELP = (
    "*Fuel Tracker*\n\n"
    "1. Add a car: `/addcar Toyota Corolla 2018`\n"
    "   I'll show the engine variants — pick yours and I'll fetch its rated economy.\n"
    "2. Log fill-ups by just sending: `14.01 @ 92184`\n"
    "   (also accepts `14.01 liter @ 92184 km`). Paste many lines at once to import.\n"
    "3. See `/stats` and `/history` anytime.\n\n"
    "*Commands*\n"
    "/addcar <make> <model> <year> - add a car\n"
    "/cars - list your cars\n"
    "/use <id> - switch the active car\n"
    "/delcar <id> - delete a car and its history\n"
    "/setrated <km/L> - set the rated economy manually\n"
    "/stats - averages & totals for the active car\n"
    "/history - recent fill-ups\n"
    "/chart - km/L trend chart\n"
    "/undo - remove the last fill-up\n"
)


def _fmt_rated(car: db.Car) -> str:
    if car.rated_kmpl:
        extra = f" ({car.rated_l100} L/100)" if car.rated_l100 else ""
        return f"{car.rated_kmpl} km/L{extra}"
    return "not set"


def _short_gen(gen_label: str, make: str, model: str) -> str:
    """Trim a generation title to a clean tag for selection buttons.

    "Toyota Corolla XII (E210, facelift 2022)" -> "XII (E210, facelift 2022)";
    "Toyota Corolla Axio" -> "Axio". Avoids the mid-parenthesis truncation.
    """
    s = re.sub(r"\s+", " ", gen_label).strip()
    prefix = f"{make} {model} "
    if s.lower().startswith(prefix.lower()):
        s = s[len(prefix):]
    # Keep up to the first complete "(...)" group so we never cut mid-paren.
    m = re.match(r"^[^(]*\([^)]*\)", s)
    tag = (m.group(0) if m else s).strip()
    return tag[:28]


async def start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP, parse_mode=ParseMode.MARKDOWN)


async def addcar(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    arg = " ".join(ctx.args)
    parsed = parse_addcar(arg)
    if not parsed:
        await update.message.reply_text(
            "Usage: `/addcar <make> <model> <year>`\nExample: `/addcar Toyota Corolla 2018`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    existing = db.find_car(user_id, parsed.make, parsed.model, parsed.year)
    if existing:
        db.set_active(user_id, existing.id)
        await update.message.reply_text(
            f"You already have *{existing.label}* (#{existing.id}). Made it active.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    msg = await update.message.reply_text(
        f"🔎 Searching variants for *{parsed.make} {parsed.model} {parsed.year}*…",
        parse_mode=ParseMode.MARKDOWN,
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
        if chosen:
            others = [e for e in (jdm, epa) if e and e is not chosen]
            await msg.edit_text(
                f"*{parsed.make} {parsed.model} {parsed.year}* added (car #{car_id}) and set active.\n\n"
                + _rated_block(chosen, others)
                + "\nNow send fill-ups like `14.01 @ 92184`.",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await msg.edit_text(
                f"*{parsed.make} {parsed.model} {parsed.year}* added (car #{car_id}) and set active.\n\n"
                "⚠️ Couldn't find variants or rated economy from any source. "
                "Set it manually with `/setrated <km/L>`.\n\n"
                "You can still log fill-ups: `14.01 @ 92184`.",
                parse_mode=ParseMode.MARKDOWN,
            )
        return

    # Stash candidates for the callback (keyed by index in callback_data).
    ctx.user_data["addcar"] = {
        "make": parsed.make,
        "model": parsed.model,
        "year": parsed.year,
        "variants": [(v.name, v.url, v.gen_label) for v in result.variants],
    }
    multi_gen = len({v.gen_label for v in result.variants if v.gen_label}) > 1
    rows: list[list[InlineKeyboardButton]] = []
    for i, v in enumerate(result.variants):
        label = v.name
        if multi_gen and v.gen_label:
            label = f"{v.name}  ·  {_short_gen(v.gen_label, parsed.make, parsed.model)}"
        rows.append([InlineKeyboardButton(label[:62], callback_data=f"var:{i}")])
    rows.append([InlineKeyboardButton("None of these / enter manually", callback_data="var:manual")])

    await msg.edit_text(
        f"Found *{result.model_name}*. Which one is yours?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(rows),
    )


def _rated_block(chosen: Economy, others: list[Economy]) -> str:
    body = (
        f"📋 Rated: *{chosen.km_per_l} km/L* ({chosen.l_per_100} L/100 km)\n"
        f"_Source: {chosen.source} — {chosen.detail}_\n"
    )
    for e in others:
        body += f"_Cross-check ({e.source}): {e.km_per_l} km/L_\n"
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


async def on_variant_selected(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    pending = ctx.user_data.get("addcar")
    if not pending:
        await query.edit_message_text("That selection expired. Send /addcar again.")
        return

    make, model, year = pending["make"], pending["model"], pending["year"]
    choice = query.data.split(":", 1)[1]

    if choice == "manual":
        car_id = _create_car(user_id, make, model, year, None)
        ctx.user_data.pop("addcar", None)
        await query.edit_message_text(
            f"*{make} {model} {year}* added (car #{car_id}) and set active.\n\n"
            "Set the rated economy with `/setrated <km/L>`, or just start logging "
            "fill-ups like `14.01 @ 92184`.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    name, url, _gen = pending["variants"][int(choice)]
    full_model = f"{model} {name}"
    await query.edit_message_text(
        f"⛽ Fetching fuel economy for *{make} {full_model} {year}*…",
        parse_mode=ParseMode.MARKDOWN,
    )

    # Query all three sources concurrently and cross-check them.
    # Priority for the headline figure: exact variant (auto-data) > JDM > US EPA.
    primary, jdm, epa = await asyncio.gather(
        autodata.variant_economy(url, detail=name),
        goonet.lookup(make, model, year, variant=name),
        fueleconomy.lookup(make, model, year),
    )
    chosen = primary or jdm or epa
    car_id = _create_car(user_id, make, full_model, year, chosen, source_url=url)
    ctx.user_data.pop("addcar", None)

    header = f"*{make} {full_model} {year}* added (car #{car_id}) and set active.\n\n"
    if chosen:
        others = [e for e in (primary, jdm, epa) if e and e is not chosen]
        body = _rated_block(chosen, others) + "\nNow send fill-ups like `14.01 @ 92184`."
    else:
        body = (
            "⚠️ Neither source had a rated figure for this variant. "
            "Set it with `/setrated <km/L>`.\n\nYou can still log fill-ups: `14.01 @ 92184`."
        )
    await query.edit_message_text(header + body, parse_mode=ParseMode.MARKDOWN,
                                  disable_web_page_preview=True)


async def cars(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    all_cars = db.list_cars(user_id)
    if not all_cars:
        await update.message.reply_text("No cars yet. Add one with `/addcar Toyota Corolla 2018`.",
                                        parse_mode=ParseMode.MARKDOWN)
        return
    active = db.get_active_car(user_id)
    active_id = active.id if active else None
    lines = ["*Your cars*"]
    for c in all_cars:
        marker = "✅" if c.id == active_id else f"`/use {c.id}`"
        lines.append(f"{marker} *{c.label}* — rated {_fmt_rated(c)}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def use_car(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text("Usage: `/use <car id>` (see `/cars`).",
                                        parse_mode=ParseMode.MARKDOWN)
        return
    car = db.get_car(int(ctx.args[0]), user_id=user_id)
    if not car:
        await update.message.reply_text("No car with that id.")
        return
    db.set_active(user_id, car.id)
    await update.message.reply_text(f"Active car is now *{car.label}*.", parse_mode=ParseMode.MARKDOWN)


async def delcar(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text(
            "Usage: `/delcar <car id>` (see `/cars`).", parse_mode=ParseMode.MARKDOWN
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
        f"⚠️ Delete *{car.label}* and its *{n}* fill-up(s)? This can't be undone.",
        parse_mode=ParseMode.MARKDOWN,
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
    remaining = db.list_cars(user_id)
    tail = ""
    if remaining:
        tail = "\n\nPick a car to make active with `/use <id>` (see `/cars`)."
    await query.edit_message_text(
        f"🗑 Deleted *{car.label}* and its history.{tail}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def setrated(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    car = db.get_active_car(user_id)
    if not car:
        await update.message.reply_text("No active car. Add one with `/addcar` first.",
                                        parse_mode=ParseMode.MARKDOWN)
        return
    try:
        kmpl = float(ctx.args[0].replace(",", "."))
        if kmpl <= 0:
            raise ValueError
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: `/setrated <km/L>`, e.g. `/setrated 18.5`.",
                                        parse_mode=ParseMode.MARKDOWN)
        return
    l100 = round(100 / kmpl, 2)
    db.set_rated(car.id, l100=l100, kmpl=round(kmpl, 2), note="set manually")
    await update.message.reply_text(
        f"Rated economy for *{car.label}* set to *{round(kmpl, 2)} km/L* ({l100} L/100 km).",
        parse_mode=ParseMode.MARKDOWN,
    )


def _stats_text(car: db.Car, stats: Stats | None) -> str:
    head = f"*{car.label}*\nRated: {_fmt_rated(car)}\n"
    if not stats:
        return head + "\nNot enough fill-ups yet — add at least two to see km/L."
    latest = stats.latest_leg
    cmp_line = ""
    if car.rated_kmpl and latest:
        delta = latest.km_per_l - car.rated_kmpl
        sign = "above" if delta >= 0 else "below"
        cmp_line = f"  ({abs(round(delta, 2))} km/L {sign} rated)"
    return (
        head
        + f"\n*Overall:* {stats.overall_km_per_l} km/L  ({stats.overall_l_per_100} L/100)\n"
        + f"Distance: {stats.total_distance:,} km over {stats.fillup_count} fill-ups\n"
        + f"Fuel used: {stats.total_fuel} L\n"
        + f"Best: {stats.best_km_per_l} km/L   Worst: {stats.worst_km_per_l} km/L\n"
        + (f"\n*Latest tank:* {latest.km_per_l} km/L ({latest.l_per_100} L/100){cmp_line}"
           if latest else "")
    )


async def stats(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    car = db.get_active_car(user_id)
    if not car:
        await update.message.reply_text("No active car. Add one with `/addcar` first.",
                                        parse_mode=ParseMode.MARKDOWN)
        return
    s = compute_stats(db.get_fillups(car.id))
    await update.message.reply_text(_stats_text(car, s), parse_mode=ParseMode.MARKDOWN)


async def history(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    car = db.get_active_car(user_id)
    if not car:
        await update.message.reply_text("No active car. Add one with `/addcar` first.",
                                        parse_mode=ParseMode.MARKDOWN)
        return
    s = compute_stats(db.get_fillups(car.id))
    if not s:
        await update.message.reply_text(
            f"*{car.label}*: not enough fill-ups yet.", parse_mode=ParseMode.MARKDOWN
        )
        return
    lines = [f"*{car.label}* — last {min(12, len(s.legs))} legs:"]
    for leg in s.legs[-12:]:
        lines.append(
            f"`{leg.odo_to:>7,}` km  +{leg.liters:>5.2f} L  →  "
            f"*{leg.km_per_l:>5.2f}* km/L  ({leg.l_per_100} L/100)"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def chart(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    car = db.get_active_car(user_id)
    if not car:
        await update.message.reply_text("No active car. Add one with `/addcar` first.",
                                        parse_mode=ParseMode.MARKDOWN)
        return
    s = compute_stats(db.get_fillups(car.id))
    if not s:
        await update.message.reply_text(
            f"*{car.label}*: need at least two fill-ups to draw a chart.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    # Rendering is CPU-bound; keep the event loop free.
    png = await asyncio.to_thread(render_chart, car, s)
    rated = f" · rated {car.rated_kmpl} km/L" if car.rated_kmpl else ""
    caption = (
        f"*{car.label}* — overall {s.overall_km_per_l} km/L, "
        f"latest {s.latest_leg.km_per_l} km/L{rated}"
    )
    await update.message.reply_photo(photo=png, caption=caption, parse_mode=ParseMode.MARKDOWN)


async def undo(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    car = db.get_active_car(user_id)
    if not car:
        await update.message.reply_text("No active car.")
        return
    removed = db.delete_last_fillup(car.id)
    if not removed:
        await update.message.reply_text("No fill-ups to remove.")
        return
    odo, liters = removed
    await update.message.reply_text(
        f"Removed last fill-up: {liters} L @ {odo:,} km from *{car.label}*.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def on_text(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Plain text: try to parse one or more 'liters @ km' fill-up lines."""
    user_id = update.effective_user.id
    text = update.message.text or ""
    parsed, bad = parse_fillups(text)

    if not parsed:
        await update.message.reply_text(
            "I didn't understand that. Send a fill-up like `14.01 @ 92184`, or /help.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    car = db.get_active_car(user_id)
    if not car:
        await update.message.reply_text(
            "Add a car first with `/addcar Toyota Corolla 2018`, then log fill-ups.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    for p in parsed:
        db.add_fillup(car.id, p.odometer, p.liters)

    s = compute_stats(db.get_fillups(car.id))
    count_note = f"Logged {len(parsed)} fill-up(s) for *{car.label}*."
    if len(parsed) == 1 and s and s.latest_leg:
        leg = s.latest_leg
        rated_cmp = ""
        if car.rated_kmpl:
            delta = leg.km_per_l - car.rated_kmpl
            rated_cmp = f" — {'+' if delta >= 0 else ''}{round(delta, 2)} vs rated {car.rated_kmpl}"
        body = (
            f"{count_note}\n\n"
            f"This tank: *{leg.km_per_l} km/L* ({leg.l_per_100} L/100) over {leg.distance:,} km{rated_cmp}\n"
            f"Overall: {s.overall_km_per_l} km/L"
        )
    elif s:
        body = f"{count_note}\nOverall now: *{s.overall_km_per_l} km/L* over {s.total_distance:,} km."
    else:
        body = f"{count_note}\nAdd one more fill-up to start seeing km/L."
    if bad:
        body += f"\n\n⚠️ Skipped {len(bad)} line(s) I couldn't parse."
    await update.message.reply_text(body, parse_mode=ParseMode.MARKDOWN)


def build_application() -> Application:
    token = require_token()
    db.init_db()
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("addcar", addcar))
    app.add_handler(CallbackQueryHandler(on_variant_selected, pattern=r"^var:"))
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
    # Keep it quiet so the token never lands in log files.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    app = build_application()
    logger.info("Fuel Tracker bot starting (polling)…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
