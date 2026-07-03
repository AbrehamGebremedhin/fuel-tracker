"""Reply / inline keyboards and the variant-selection keyboard builder."""

from __future__ import annotations

import re

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from .sources.base import Variant

# --- persistent reply keyboard ---------------------------------------------

BTN_ADD = "➕ Add fuel"
BTN_STATS = "📊 Stats"
BTN_CHART = "📈 Chart"
BTN_CARS = "🚗 Cars"
BTN_COMPARE = "🆚 Compare"
BTN_FILLUPS = "📋 Fill-ups"
BTN_EXPORT = "📄 Export"
BTN_HELP = "❓ Help"

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton(BTN_ADD), KeyboardButton(BTN_STATS)],
        [KeyboardButton(BTN_CHART), KeyboardButton(BTN_CARS)],
        [KeyboardButton(BTN_COMPARE), KeyboardButton(BTN_FILLUPS)],
        [KeyboardButton(BTN_EXPORT), KeyboardButton(BTN_HELP)],
    ],
    resize_keyboard=True,
    is_persistent=True,
)


# --- inline keyboards -------------------------------------------------------

def after_fillup_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📈 Chart", callback_data="act:chart"),
        InlineKeyboardButton("📊 Stats", callback_data="act:stats"),
        InlineKeyboardButton("↩️ Undo", callback_data="act:undo"),
    ]])


def add_car_hint_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("➕ Add a car", callback_data="act:addcar_hint"),
    ]])


# --- variant selection ------------------------------------------------------

_HYBRID_RE = re.compile(r"hybrid|e-cvt|phev|plug-?in", re.IGNORECASE)


def _is_hybrid(name: str) -> bool:
    return bool(_HYBRID_RE.search(name))


def short_gen(gen_label: str, make: str, model: str) -> str:
    """Trim a generation title to a clean tag for selection headers/buttons.

    "Toyota Corolla XII (E210, facelift 2022)" -> "XII (E210, facelift 2022)";
    "Toyota Corolla Axio" -> "Axio". Avoids the mid-parenthesis truncation.
    """
    s = re.sub(r"\s+", " ", gen_label).strip()
    prefix = f"{make} {model} "
    if s.lower().startswith(prefix.lower()):
        s = s[len(prefix):]
    m = re.match(r"^[^(]*\([^)]*\)", s)  # keep up to the first complete "(...)"
    tag = (m.group(0) if m else s).strip()
    return tag[:28]


def group_variants(variants: list[Variant], make: str, model: str) -> list[dict]:
    """Group variants by generation (header when >1 gen), hybrids sorted last.

    Returns [{"header": str|None, "items": [(original_index, name), ...]}, ...].
    """
    indexed = list(enumerate(variants))
    multi_gen = len({v.gen_label for _, v in indexed if v.gen_label}) > 1

    groups: dict[str, list[tuple[int, Variant]]] = {}
    order: list[str] = []
    for i, v in indexed:
        key = v.gen_label or ""
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append((i, v))

    out: list[dict] = []
    for key in order:
        items = sorted(groups[key], key=lambda iv: (_is_hybrid(iv[1].name), iv[1].name))
        header = short_gen(key, make, model) if (multi_gen and key) else None
        out.append({"header": header, "items": [(i, v.name) for i, v in items]})
    return out


def build_variant_keyboard(variants: list[Variant], make: str, model: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for group in group_variants(variants, make, model):
        if group["header"]:
            rows.append([InlineKeyboardButton(f"— {group['header']} —", callback_data="noop")])
        for i, name in group["items"]:
            rows.append([InlineKeyboardButton(name[:62], callback_data=f"var:{i}")])
    rows.append([
        InlineKeyboardButton("✖ Cancel", callback_data="var:cancel"),
        InlineKeyboardButton("✍ Enter manually", callback_data="var:manual"),
    ])
    return InlineKeyboardMarkup(rows)
