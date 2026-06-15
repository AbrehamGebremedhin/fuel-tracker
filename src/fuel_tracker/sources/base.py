"""Shared data types and helpers for economy sources."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# US mpg -> km/L  (1 mile = 1.609344 km, 1 US gal = 3.785411784 L)
MPG_TO_KMPL = 1.609344 / 3.785411784  # ~0.4251437


@dataclass
class Economy:
    """A resolved rated economy figure from one source."""

    l_per_100: float
    km_per_l: float
    source: str          # human-readable source name
    detail: str = ""     # e.g. "combined, 1.8 VVT-i (140 Hp)"


@dataclass
class Variant:
    """A selectable engine/trim option for a car."""

    name: str            # e.g. "1.5i 16V (110 Hp) Automatic"
    url: str             # source page with this variant's specs
    gen_label: str = ""  # generation name, used to disambiguate when several match


@dataclass
class VariantResult:
    model_name: str
    variants: list[Variant] = field(default_factory=list)


def mpg_to_economy(mpg: float, source: str, detail: str = "") -> Economy:
    kmpl = mpg * MPG_TO_KMPL
    return Economy(
        l_per_100=round(100 / kmpl, 2),
        km_per_l=round(kmpl, 2),
        source=source,
        detail=detail,
    )


def l100_to_economy(l_per_100: float, source: str, detail: str = "") -> Economy:
    return Economy(
        l_per_100=round(l_per_100, 2),
        km_per_l=round(100 / l_per_100, 2),
        source=source,
        detail=detail,
    )


_YEAR_TAIL_RE = re.compile(r"\s*\d{4}\s*-\s*\d{0,4}\s*$")


def clean_variant_name(text: str) -> str:
    """Strip a trailing production-year range from a variant label."""
    return _YEAR_TAIL_RE.sub("", re.sub(r"\s+", " ", text).strip()).strip()
