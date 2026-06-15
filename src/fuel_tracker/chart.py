"""Render a km/L trend chart as PNG bytes, styled after the HTML dashboard."""

from __future__ import annotations

import io

import matplotlib

matplotlib.use("Agg")  # headless backend; must be set before pyplot import

import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import MaxNLocator  # noqa: E402

from .calc import Stats  # noqa: E402
from .db import Car  # noqa: E402

BLUE = "#378ADD"
RED = "#E24B4A"
TEAL = "#9FE1CB"
GREEN = "#2BA84A"
INK = "#111111"
MUTE = "#888888"


def render_chart(car: Car, stats: Stats) -> bytes:
    """Top panel: km/L per tank (with overall avg + rated lines). Bottom: liters per fill."""
    legs = stats.legs
    x = list(range(len(legs)))
    kmpl = [leg.km_per_l for leg in legs]
    liters = [leg.liters for leg in legs]
    labels = [f"{leg.odo_to:,}" for leg in legs]

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(9, 6.4), dpi=140, sharex=True,
        gridspec_kw={"height_ratios": [3, 1.2], "hspace": 0.12},
    )
    fig.patch.set_facecolor("white")

    # --- km/L trend -------------------------------------------------------
    ax1.plot(x, kmpl, color=BLUE, linewidth=2, zorder=2)
    # Flag tanks that came in worse than your own running average.
    pt_colors = [RED if v < stats.overall_km_per_l else BLUE for v in kmpl]
    ax1.scatter(x, kmpl, c=pt_colors, s=42, zorder=3, edgecolors="white", linewidths=0.8)

    ax1.axhline(
        stats.overall_km_per_l, color=RED, linestyle=(0, (6, 4)), linewidth=1.4,
        label=f"Average {stats.overall_km_per_l} km/L",
    )
    if car.rated_kmpl:
        ax1.axhline(
            car.rated_kmpl, color=GREEN, linestyle=(0, (2, 3)), linewidth=1.4,
            label=f"Rated {car.rated_kmpl} km/L",
        )

    # Annotate the latest tank.
    ax1.annotate(
        f"{kmpl[-1]:.2f}", (x[-1], kmpl[-1]),
        textcoords="offset points", xytext=(6, 8),
        fontsize=10, fontweight="bold", color=INK,
    )

    ax1.set_ylabel("km / L", fontsize=11, color=INK)
    ax1.set_title(f"{car.label} — fuel economy", fontsize=14, fontweight="bold",
                  color=INK, loc="left", pad=10)
    ax1.legend(loc="lower left", fontsize=9, frameon=False)
    ax1.grid(axis="y", color="#eee", linewidth=1)
    ax1.set_axisbelow(True)
    ax1.margins(x=0.02)

    # --- liters per fill --------------------------------------------------
    bar_colors = [RED if v > 20 else TEAL for v in liters]
    ax2.bar(x, liters, color=bar_colors, width=0.62)
    ax2.set_ylabel("Liters", fontsize=10, color=INK)
    ax2.grid(axis="y", color="#eee", linewidth=1)
    ax2.set_axisbelow(True)
    ax2.yaxis.set_major_locator(MaxNLocator(nbins=4))

    ax2.set_xticks(x)
    ax2.set_xticklabels(labels, rotation=45, ha="right", fontsize=8, color=MUTE)
    ax2.set_xlabel("Odometer (km)", fontsize=10, color=MUTE)

    for ax in (ax1, ax2):
        for spine in ("top", "right"):
            ax.spines[spine].set_visible(False)
        ax.tick_params(colors=MUTE)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor="white", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()
