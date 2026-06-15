"""goo-net.com (JDM) source — Japanese-market catalog fuel economy.

Covers domestic models (Platz, Vitz, Fit, Note, …) that auto-data.net and the US EPA
don't list. The catalog grade table exposes engine displacement, transmission, drivetrain
and the rated km/L, so we match the user's selected variant by displacement + transmission.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass

import httpx
from bs4 import BeautifulSoup

from .base import Economy

BASE = "https://www.goo-net.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en;q=0.8",
}
SOURCE_NAME = "goo-net (JDM, JC08/10·15)"

_CC_RE = re.compile(r"(\d{3,4})\s*cc", re.IGNORECASE)
_KMPL_RE = re.compile(r"(\d{1,2}(?:\.\d)?)\s*km/?l", re.IGNORECASE)
_DISP_RE = re.compile(r"(\d)\.(\d)")
_AUTO_WORDS = ("automatic", "auto", "cvt", "dsg", "tiptronic", "multidrive", "e-cvt", "s tronic")


@dataclass
class _Grade:
    cc: int
    transmission: str | None  # "AT" | "MT" | None
    kmpl: float


def variant_hints(name: str) -> tuple[int | None, str | None]:
    """Pull (engine_cc, transmission) from an auto-data variant label.

    e.g. "1.0i 16V (70 Hp) Automatic" -> (1000, "AT"); "1.3 VVT-i (88 Hp)" -> (1300, "MT").
    """
    cc = None
    m = _DISP_RE.search(name)
    if m:
        cc = int(round(float(m.group(0)) * 1000))  # "1.0" -> 1000, "1.3" -> 1300
    low = name.lower()
    trans = "AT" if any(w in low for w in _AUTO_WORDS) else "MT"
    return cc, trans


def _row_transmission(cell: str) -> str | None:
    s = cell.upper()
    has_mt = "MT" in s
    has_at = "AT" in s or "CVT" in s
    if has_mt and not has_at:
        return "MT"
    if has_at and not has_mt:
        return "AT"
    return None


def _parse_grades(html: str) -> list[_Grade]:
    soup = BeautifulSoup(html, "html.parser")
    grades: list[_Grade] = []
    for table in soup.select("table.tbl_type03"):
        for tr in table.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if not cells:
                continue
            row = " ".join(cells)
            cc_m = _CC_RE.search(row)
            kmpl_m = _KMPL_RE.search(row)
            if not cc_m or not kmpl_m:
                continue
            kmpl = float(kmpl_m.group(1))
            if kmpl <= 0:
                continue
            trans = next((_row_transmission(c) for c in cells if _row_transmission(c)), None)
            grades.append(_Grade(cc=int(cc_m.group(1)), transmission=trans, kmpl=kmpl))
    return grades


def _slug(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", s.upper())


async def _find_model_url(client: httpx.AsyncClient, make: str, model: str) -> str | None:
    direct = f"{BASE}/catalog/{_slug(make)}/{_slug(model)}/"
    r = await client.get(direct)
    if r.status_code == 200 and "tbl_type03" in r.text:
        return direct
    # Fallback: scan the maker catalog for a model link matching the name.
    maker = await client.get(f"{BASE}/catalog/{_slug(make)}/")
    if maker.status_code != 200:
        return None
    soup = BeautifulSoup(maker.text, "html.parser")
    target = _slug(model)
    prefix = f"/catalog/{_slug(make)}/"
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith(prefix):
            tail = href[len(prefix):].strip("/")
            if tail and _slug(tail) == target:
                return BASE + href
    return None


def _select(grades: list[_Grade], cc: int | None, trans: str | None) -> list[float]:
    """Pick km/L values matching the variant, relaxing filters if needed."""
    def by_cc(g: _Grade) -> bool:
        return cc is None or abs(g.cc - cc) <= 120

    def by_trans(g: _Grade) -> bool:
        return trans is None or g.transmission is None or g.transmission == trans

    for predicate in (
        lambda g: by_cc(g) and (trans is None or g.transmission == trans),
        lambda g: by_cc(g),                       # relax transmission
        lambda g: True,                            # relax everything
    ):
        vals = [g.kmpl for g in grades if predicate(g)]
        if vals:
            return vals
    return []


async def lookup(
    make: str, model: str, year: int, *, variant: str | None = None, timeout: float = 25.0
) -> Economy | None:
    cc, trans = variant_hints(variant) if variant else (None, None)
    try:
        async with httpx.AsyncClient(headers=HEADERS, timeout=timeout,
                                     follow_redirects=True) as client:
            url = await _find_model_url(client, make, model)
            if not url:
                return None
            r = await client.get(url)
            r.raise_for_status()
            grades = _parse_grades(r.text)
            if not grades:
                return None
            vals = _select(grades, cc, trans)
            if not vals:
                return None
            kmpl = round(statistics.median(vals), 2)
            bits = []
            if cc:
                bits.append(f"~{cc / 1000:.1f}L")
            if trans:
                bits.append(trans)
            scope = " ".join(bits) or "all grades"
            # JDM figures are Japan's JC08 / 10·15 cycle — optimistic vs EU/EPA combined.
            spread = f", range {min(vals):g}-{max(vals):g}" if len(vals) > 1 else ""
            detail = f"{scope}, median of {len(vals)} grade(s){spread}"
            return Economy(l_per_100=round(100 / kmpl, 2), km_per_l=kmpl,
                           source=SOURCE_NAME, detail=detail)
    except (httpx.HTTPError, ValueError):
        return None
