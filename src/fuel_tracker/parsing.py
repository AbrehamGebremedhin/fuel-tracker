"""Parsing of user text: fill-up lines and the /addcar argument."""

from __future__ import annotations

import re
from dataclasses import dataclass

# Matches "14.01 @ 92184", "14.01 liter @ 92184 km", "14.01L@92184",
# "14.01 litres @ 92,184 km", etc. The unit words and "km" are optional.
_FILLUP_RE = re.compile(
    r"""
    (?P<liters>\d+(?:[.,]\d+)?)        # liters, dot or comma decimal
    \s*(?:l|liter|liters|litre|litres)?\s*
    @
    \s*(?P<odo>[\d,\.]+?)\s*           # odometer, may have thousands separators
    (?:km|kms|kilometers?)?            # optional unit
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)


@dataclass
class ParsedFillup:
    liters: float
    odometer: int


def _to_float(s: str) -> float:
    return float(s.replace(",", "."))


def _to_int_odo(s: str) -> int:
    # Odometers use thousands separators (commas or dots); strip them all.
    return int(re.sub(r"[,\.\s]", "", s))


def parse_fillup_line(line: str) -> ParsedFillup | None:
    """Parse a single 'liters @ km' line. Returns None if it doesn't match."""
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
    return ParsedFillup(liters=liters, odometer=odo)


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
