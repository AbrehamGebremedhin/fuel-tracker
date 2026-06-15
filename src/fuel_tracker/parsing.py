"""Parsing of user text: fill-up lines and the /addcar argument."""

from __future__ import annotations

import re
from dataclasses import dataclass

# Matches "14.01 @ 92184", "14.01 liter @ 92184 km", "14.01L@92184", and an optional
# trailing cost: "= 1200" (total paid) or "@ 85.7/L" (price per litre). The unit words,
# "km", and the cost are all optional.
_FILLUP_RE = re.compile(
    r"""
    ^\s*
    (?P<liters>\d+(?:[.,]\d+)?)        # liters, dot or comma decimal
    \s*(?:l|liter|liters|litre|litres)?\s*
    @
    \s*(?P<odo>\d[\d,\.]*)\s*          # odometer, may have thousands separators
    (?:km|kms|kilometers?)?            # optional unit
    \s*
    (?P<rest>.*?)                      # optional trailing cost segment
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

_PER_LITRE_RE = re.compile(r"/\s*l|per\s*l", re.IGNORECASE)
_AMOUNT_RE = re.compile(r"\d+(?:[.,]\d+)?")


@dataclass
class ParsedFillup:
    liters: float
    odometer: int
    cost: float | None = None  # total cost for this fill-up, in the user's own currency


def _to_float(s: str) -> float:
    return float(s.replace(",", "."))


def _to_int_odo(s: str) -> int:
    # Odometers use thousands separators (commas or dots); strip them all.
    return int(re.sub(r"[,\.\s]", "", s))


def _to_amount(s: str) -> float:
    # Treat a single trailing "<comma><1-2 digits>" as a decimal comma; else commas
    # are thousands separators.
    if re.fullmatch(r"\d+,\d{1,2}", s):
        s = s.replace(",", ".")
    return float(s.replace(",", ""))


def _parse_cost(rest: str, liters: float) -> float | None:
    """Parse the trailing cost segment. Returns total cost or None."""
    rest = rest.strip()
    if not rest:
        return None
    m = _AMOUNT_RE.search(rest)
    if not m:
        return None
    amount = _to_amount(m.group(0))
    if amount <= 0:
        return None
    if _PER_LITRE_RE.search(rest):     # price per litre -> total = price * liters
        return round(amount * liters, 2)
    return round(amount, 2)             # otherwise it's the total paid


def parse_fillup_line(line: str) -> ParsedFillup | None:
    """Parse a single 'liters @ km [= cost | @ price/L]' line, or None."""
    m = _FILLUP_RE.match(line.strip())
    if not m:
        return None
    try:
        liters = _to_float(m.group("liters"))
        odo = _to_int_odo(m.group("odo"))
    except ValueError:
        return None
    if liters <= 0 or odo <= 0:
        return None
    cost = _parse_cost(m.group("rest"), liters)
    return ParsedFillup(liters=liters, odometer=odo, cost=cost)


def parse_fillups(text: str) -> tuple[list[ParsedFillup], list[str]]:
    """Parse possibly many lines. Returns (parsed, unparsed_lines)."""
    parsed: list[ParsedFillup] = []
    bad: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        p = parse_fillup_line(line)
        if p:
            parsed.append(p)
        else:
            bad.append(line)
    return parsed, bad


@dataclass
class ParsedCar:
    make: str
    model: str
    year: int


def parse_addcar(arg: str) -> ParsedCar | None:
    """Parse 'Make Model... Year' where Year is a trailing 4-digit number."""
    tokens = arg.split()
    if len(tokens) < 2:
        return None
    year_tok = tokens[-1]
    if not re.fullmatch(r"(19|20)\d{2}", year_tok):
        return None
    year = int(year_tok)
    rest = tokens[:-1]
    if len(rest) < 2:
        # Need at least a make and a model word.
        return None
    make = rest[0]
    model = " ".join(rest[1:])
    return ParsedCar(make=make, model=model, year=year)
