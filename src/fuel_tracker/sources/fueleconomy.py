"""fueleconomy.gov (US EPA) source — make/model/year -> combined economy.

US-market vehicles only, but it's an official API with a clean JSON mode, so it makes
a good second source / cross-check alongside auto-data.net.
"""

from __future__ import annotations

import httpx

from .base import Economy, mpg_to_economy

BASE = "https://www.fueleconomy.gov/ws/rest/vehicle"
HEADERS = {"User-Agent": "fuel-tracker-bot/0.1", "Accept": "application/json"}
SOURCE_NAME = "fueleconomy.gov (US EPA)"
MAX_TRIMS = 5


def _as_list(menu) -> list[dict]:
    if menu is None:
        return []
    return menu if isinstance(menu, list) else [menu]


async def lookup(make: str, model: str, year: int, *, timeout: float = 20.0) -> Economy | None:
    """Return the combined economy averaged across matching US trims, or None."""
    try:
        async with httpx.AsyncClient(headers=HEADERS, timeout=timeout) as client:
            opts = await client.get(
                f"{BASE}/menu/options",
                params={"year": str(year), "make": make, "model": model},
            )
            opts.raise_for_status()
            data = opts.json()
            if not isinstance(data, dict):  # unknown model -> JSON null
                return None
            items = _as_list(data.get("menuItem"))
            if not items:
                return None

            mpgs: list[float] = []
            for item in items[:MAX_TRIMS]:
                vid = item.get("value")
                if not vid:
                    continue
                v = await client.get(f"{BASE}/{vid}")
                v.raise_for_status()
                comb = v.json().get("comb08")
                try:
                    mpg = float(comb)
                except (TypeError, ValueError):
                    continue
                if mpg > 0:
                    mpgs.append(mpg)

            if not mpgs:
                return None
            avg = sum(mpgs) / len(mpgs)
            detail = f"combined, avg of {len(mpgs)} US trim(s)"
            return mpg_to_economy(avg, SOURCE_NAME, detail)
    except (httpx.HTTPError, ValueError, KeyError):
        return None
