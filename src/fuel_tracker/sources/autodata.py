"""auto-data.net source: model search, variant listing, per-variant economy.

Flow:  autocomplete search  ->  model page (generations by year)
       ->  generation page(s) (engine variants)  ->  variant page (combined l/100 km).
"""

from __future__ import annotations

import re

import httpx
from bs4 import BeautifulSoup

from .base import Economy, Variant, VariantResult, clean_variant_name, l100_to_economy

BASE = "https://www.auto-data.net"
AUTOCOMPLETE = f"{BASE}/ajax/get-words.php"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Referer": f"{BASE}/en/search",
    "X-Requested-With": "XMLHttpRequest",
}
SOURCE_NAME = "auto-data.net"

_YEAR_RANGE_RE = re.compile(r"(\d{4})\s*-\s*(\d{4})?")
_HP_RE = re.compile(r"\(\s*\d+\s*Hp\s*\)", re.IGNORECASE)
_L100_RE = re.compile(r"([\d.]+)\s*l/100", re.IGNORECASE)

MAX_GENERATIONS = 4
MAX_VARIANTS = 24


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _abs_url(href: str) -> str:
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return BASE + href
    return f"{BASE}/en/{href}"


def _year_range(years_text: str) -> tuple[int, int | None] | None:
    m = _YEAR_RANGE_RE.search(years_text)
    if not m:
        return None
    return int(m.group(1)), (int(m.group(2)) if m.group(2) else None)


# --- search -----------------------------------------------------------------

async def _pick_model_slug(client: httpx.AsyncClient, make: str, model: str) -> str | None:
    query = f"{make} {model}".strip()
    r = await client.get(AUTOCOMPLETE, params={"SEARCH_MORE_RESULTS": "0", "search": query})
    r.raise_for_status()
    candidates: list[tuple[str, str]] = []  # (label, slug)
    for entry in r.text.split("|"):
        if "###" not in entry:
            continue
        slug, label_html = entry.split("###", 1)
        slug = slug.strip()
        if "-model-" not in slug:
            continue
        label = _norm(BeautifulSoup(label_html, "html.parser").get_text(" "))
        if label:
            candidates.append((label, slug))
    if not candidates:
        return None
    target = _norm(query)
    exact = [s for lbl, s in candidates if lbl == target]
    if exact:
        return exact[0]
    pool = [(lbl, s) for lbl, s in candidates if lbl.startswith(target)] or candidates
    pool.sort(key=lambda ls: (target not in ls[0], len(ls[0])))
    return pool[0][1]


def _parse_generations(html: str) -> list[tuple[str, str, str]]:
    """Return (title, years_text, url) per generation on a model page."""
    soup = BeautifulSoup(html, "html.parser")
    by_url: dict[str, dict[str, str]] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "generation" not in href:
            continue
        text = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
        if not text:
            continue
        slot = by_url.setdefault(href, {})
        if re.match(r"^\d{4}", text):
            slot.setdefault("years", text)
        else:
            slot.setdefault("title", text)
    return [
        (slot.get("title", ""), slot.get("years", ""), _abs_url(href))
        for href, slot in by_url.items()
    ]


def _choose_generations(
    gens: list[tuple[str, str, str]], year: int
) -> list[tuple[str, str, str]]:
    def matches(g):
        rng = _year_range(g[1])
        return bool(rng) and rng[0] <= year <= (rng[1] if rng[1] is not None else 9999)

    matching = [g for g in gens if matches(g)]
    if matching:
        # Prefer plain (non body/regional variant) titles first.
        markers = ("(usa)", "(us)", "touring", "hatchback", "estate", "coupe",
                   "cabriolet", "wagon", "alltrack", "cross", "sportback")
        matching.sort(key=lambda g: (any(m in g[0].lower() for m in markers), len(g[0])))
        return matching[:MAX_GENERATIONS]
    # No exact year match: take generations closest by start year.
    with_year = [(g, _year_range(g[1])) for g in gens]
    with_year = [(g, r) for g, r in with_year if r]
    with_year.sort(key=lambda gr: abs(gr[1][0] - year))
    return [g for g, _ in with_year[:1]]


def _parse_variants(html: str, gen_label: str) -> list[Variant]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[Variant] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.startswith("/en/") or "generation" in href or "-model-" in href:
            continue
        text = a.get_text(" ", strip=True)
        if not _HP_RE.search(text) or href in seen:
            continue
        seen.add(href)
        out.append(Variant(name=clean_variant_name(text), url=_abs_url(href),
                            gen_label=gen_label))
    return out


async def search_variants(
    make: str, model: str, year: int, *, timeout: float = 20.0
) -> VariantResult | None:
    """Find selectable engine variants for the given make/model/year."""
    try:
        async with httpx.AsyncClient(headers=HEADERS, timeout=timeout,
                                     follow_redirects=True) as client:
            slug = await _pick_model_slug(client, make, model)
            if not slug:
                return None
            model_resp = await client.get(_abs_url(slug))
            model_resp.raise_for_status()
            gens = _choose_generations(_parse_generations(model_resp.text), year)
            if not gens:
                return None

            variants: list[Variant] = []
            multi_gen = len(gens) > 1
            for title, _years, url in gens:
                gen_resp = await client.get(url)
                gen_resp.raise_for_status()
                label = title if multi_gen else ""
                variants.extend(_parse_variants(gen_resp.text, label))
                if len(variants) >= MAX_VARIANTS:
                    break
            return VariantResult(model_name=f"{make} {model}", variants=variants[:MAX_VARIANTS])
    except (httpx.HTTPError, ValueError):
        return None


# --- per-variant economy ----------------------------------------------------

def _l100_from_td(td_text: str) -> float | None:
    m = _L100_RE.search(td_text)
    return float(m.group(1)) if m else None


def _parse_variant_economy(html: str, detail: str) -> Economy | None:
    soup = BeautifulSoup(html, "html.parser")
    combined = urban = extra = summary = None
    for tr in soup.find_all("tr"):
        th, td = tr.find("th"), tr.find("td")
        if not th or not td:
            continue
        label = th.get_text(" ", strip=True).lower()
        val = _l100_from_td(td.get_text(" ", strip=True))
        if val is None:
            continue
        if "consumption" in label and "combined" in label:
            combined = val
        elif "consumption" in label and "extra" in label:
            extra = val
        elif "consumption" in label and "urban" in label:
            urban = val
        elif "fuel economy" in label:
            summary = val

    l100 = combined or summary
    if l100 is None and urban is not None and extra is not None:
        l100 = round((urban + extra) / 2, 2)
    if not l100 or l100 <= 0:
        return None
    return l100_to_economy(l100, SOURCE_NAME, detail)


async def variant_economy(url: str, *, detail: str = "", timeout: float = 20.0) -> Economy | None:
    try:
        async with httpx.AsyncClient(headers=HEADERS, timeout=timeout,
                                     follow_redirects=True) as client:
            r = await client.get(url)
            r.raise_for_status()
            return _parse_variant_economy(r.text, detail)
    except (httpx.HTTPError, ValueError):
        return None
