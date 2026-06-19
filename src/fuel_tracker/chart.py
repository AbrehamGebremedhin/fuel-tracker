"""Render a dashboard-style km/L chart as PNG bytes."""

from __future__ import annotations

import io

import matplotlib

matplotlib.use("Agg")  # headless backend; must be set before pyplot import

import matplotlib.font_manager as fm  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import MaxNLocator  # noqa: E402

from .calc import Stats, TimeStats  # noqa: E402
from .db import Car  # noqa: E402

# Palette
BLUE = "#378ADD"    # per-tank series
AMBER = "#E8833A"   # rolling-average trend
GREEN = "#2BA84A"   # rated / best
RED = "#E24B4A"     # worst / large fill
TEAL = "#7FD6BB"    # liters bars
GREY = "#9AA0A6"    # average / muted lines
INK = "#1A1A1A"
MUTE = "#8A9099"
PANEL = "#F6F7F9"


def _moving_average(values: list[float], window: int) -> list[float]:
    """Trailing moving average that still produces a value for every point."""
    out: list[float] = []
    for i in range(len(values)):
        seg = values[max(0, i - window + 1): i + 1]
        out.append(sum(seg) / len(seg))
    return out


def _metric(ax, x: float, value: str, label: str, color: str) -> None:
    ax.text(x, 0.66, value, ha="center", va="center", transform=ax.transAxes,
            fontsize=17, fontweight="bold", color=color)
    ax.text(x, 0.18, label, ha="center", va="center", transform=ax.transAxes,
            fontsize=9.5, color=MUTE)


def render_chart(car: Car, stats: Stats, ts: TimeStats | None = None) -> bytes:
    plt.rcParams["font.family"] = fm.FontProperties().get_name()
    legs = stats.legs
    n = len(legs)
    idx = list(range(n))
    xs = [leg.odo_to for leg in legs]   # plot against real odometer, not index
    kmpl = [leg.km_per_l for leg in legs]
    liters = [leg.liters for leg in legs]
    labels = [f"{leg.odo_to:,}" for leg in legs]
    rated = car.rated_kmpl
    avg = stats.overall_km_per_l
    best_i = max(idx, key=lambda i: kmpl[i])
    worst_i = min(idx, key=lambda i: kmpl[i])
    has_cost = stats.has_cost

    # Bars/labels are spaced by odometer now, so derive widths/strides from the data.
    gaps = [xs[i + 1] - xs[i] for i in range(n - 1)]
    bar_w = (min(gaps) if gaps else 1000) * 0.7
    label_step = max(1, round(n / 8))    # thin per-point labels so they don't collide
    tick_step = max(1, round(n / 9))

    fig = plt.figure(figsize=(9.2, 9.0 if has_cost else 7.6), dpi=150)
    fig.patch.set_facecolor("white")
    ratios = [0.5, 3.0, 1.0] + ([1.0] if has_cost else [])
    gs = fig.add_gridspec(len(ratios), 1, height_ratios=ratios, hspace=0.30,
                          left=0.085, right=0.965, top=0.885 if ts else 0.9, bottom=0.1)

    # --- title + summary metric strip ------------------------------------
    fig.text(0.085, 0.965, f"{car.label}", fontsize=15, fontweight="bold", color=INK)
    sub = (f"{legs[0].odo_from:,}–{legs[-1].odo_to:,} km   ·   "
           f"{stats.total_distance:,} km   ·   {stats.total_fuel:g} L   ·   {n} tanks")
    if has_cost:
        sub += (f"   ·   spent {stats.total_cost:,.0f}   ·   "
                f"{stats.avg_cost_per_100:,.0f}/100km   ·   {stats.avg_price_per_l:g}/L")
    fig.text(0.085, 0.925, sub, fontsize=9.5, color=MUTE)
    if ts:
        tline = (f"{ts.km_per_day:g} km/day  ·  fill every ~{ts.days_between_fills:g} d  ·  "
                 f"~{ts.monthly_distance:,} km/mo  ·  next fill ~{ts.next_fill_date:%b %d}")
        fig.text(0.085, 0.902, tline, fontsize=9, color=AMBER)

    ax_m = fig.add_subplot(gs[0])
    ax_m.axis("off")
    _metric(ax_m, 0.125, f"{avg:g}", "Overall km/L", INK)
    _metric(ax_m, 0.375, f"{stats.best_km_per_l:g}", "Best", GREEN)
    _metric(ax_m, 0.625, f"{stats.worst_km_per_l:g}", "Worst", RED)
    _metric(ax_m, 0.875, f"{kmpl[-1]:g}", "Latest", BLUE)

    # --- main km/L panel --------------------------------------------------
    ax1 = fig.add_subplot(gs[1])

    # Rated-vs-actual band. Only worth shading when the rated line actually crosses
    # the data — otherwise (rated far above every tank) it's a solid wash, so just
    # draw the reference line.
    if rated:
        ax1.axhline(rated, color=GREEN, linestyle=(0, (5, 4)), linewidth=1.3,
                    label=f"Rated {rated:g}", zorder=2)
        above = sum(v >= rated for v in kmpl)
        if 0 < above < n:
            ax1.fill_between(xs, kmpl, rated, where=[v < rated for v in kmpl],
                             color=RED, alpha=0.07, interpolate=True, zorder=1)
            ax1.fill_between(xs, kmpl, rated, where=[v >= rated for v in kmpl],
                             color=GREEN, alpha=0.12, interpolate=True, zorder=1)

    ax1.axhline(avg, color=GREY, linestyle=(0, (6, 4)), linewidth=1.2,
                label=f"Average {avg:g}", zorder=2)

    # Per-tank line + rolling-average trend.
    window = max(2, round(n / 5))
    trend = _moving_average(kmpl, window)
    ax1.plot(xs, kmpl, color=BLUE, linewidth=1.4, alpha=0.45, zorder=3)
    ax1.scatter(xs, kmpl, s=34, color=BLUE, zorder=4, edgecolors="white",
                linewidths=0.8, label="Per tank")
    ax1.plot(xs, trend, color=AMBER, linewidth=2.6, zorder=5,
             label=f"Trend ({window}-tank avg)")

    # Per-point value labels — thinned so they don't collide; best/worst get their own.
    for i in idx:
        if i in (best_i, worst_i) or i % label_step:
            continue
        ax1.annotate(f"{kmpl[i]:.1f}", (xs[i], kmpl[i]), textcoords="offset points",
                     xytext=(0, 7), ha="center", fontsize=7, color=MUTE, zorder=6)

    # Best / worst markers.
    ax1.scatter([xs[best_i]], [kmpl[best_i]], marker="*", s=240, color=GREEN,
                edgecolors="white", linewidths=1, zorder=7)
    ax1.annotate(f"best {kmpl[best_i]:.2f}", (xs[best_i], kmpl[best_i]),
                 textcoords="offset points", xytext=(0, 13), ha="center",
                 fontsize=8.5, fontweight="bold", color=GREEN, zorder=8)
    ax1.scatter([xs[worst_i]], [kmpl[worst_i]], marker="X", s=130, color=RED,
                edgecolors="white", linewidths=1, zorder=7)
    ax1.annotate(f"worst {kmpl[worst_i]:.2f}", (xs[worst_i], kmpl[worst_i]),
                 textcoords="offset points", xytext=(0, -16), ha="center",
                 fontsize=8.5, fontweight="bold", color=RED, zorder=8)

    lo = min(min(kmpl), rated or kmpl[0])
    hi = max(max(kmpl), rated or 0)
    pad = (hi - lo) * 0.22 or 1
    ax1.set_ylim(lo - pad * 0.6, hi + pad)
    ax1.set_ylabel("km / L", fontsize=11, color=INK)
    ax1.grid(axis="y", color="#ECEEF1", linewidth=1)
    ax1.set_axisbelow(True)
    ax1.margins(x=0.02)
    ax1.legend(loc="upper center", ncol=4, fontsize=8.5, frameon=False,
               bbox_to_anchor=(0.5, 1.11), handletextpad=0.4, columnspacing=1.4)

    # --- liters panel -----------------------------------------------------
    ax2 = fig.add_subplot(gs[2], sharex=ax1)
    big = max(liters)
    bar_colors = [RED if v == big else TEAL for v in liters]
    ax2.bar(xs, liters, color=bar_colors, width=bar_w, zorder=3)
    ax2.set_ylabel("Liters", fontsize=10, color=INK)
    ax2.grid(axis="y", color="#ECEEF1", linewidth=1)
    ax2.set_axisbelow(True)
    ax2.yaxis.set_major_locator(MaxNLocator(nbins=4))

    panels = [ax1, ax2]

    # --- cost panel (only when fill-ups have a recorded cost) -------------
    if has_cost:
        ax3 = fig.add_subplot(gs[3], sharex=ax1)
        cpk = [leg.cost_per_100 for leg in legs]            # None where unknown
        bars = [c if c is not None else 0 for c in cpk]
        ax3.bar(xs, bars, color=AMBER, width=bar_w, zorder=3)
        ax3.axhline(stats.avg_cost_per_100, color=GREY, linestyle=(0, (6, 4)),
                    linewidth=1.2, zorder=4)
        ax3.set_ylabel("Cost / 100 km", fontsize=10, color=INK)
        ax3.grid(axis="y", color="#ECEEF1", linewidth=1)
        ax3.set_axisbelow(True)
        ax3.yaxis.set_major_locator(MaxNLocator(nbins=4))
        panels.append(ax3)

    # The bottom-most panel carries the odometer labels; hide them on the rest.
    # Show every tick_step-th odometer (always include the last) to avoid crowding.
    tick_idx = sorted(set(range(0, n, tick_step)) | {n - 1})
    bottom = panels[-1]
    bottom.set_xticks([xs[i] for i in tick_idx])
    bottom.set_xticklabels([labels[i] for i in tick_idx], rotation=45, ha="right",
                           fontsize=8, color=MUTE)
    bottom.set_xlabel("Odometer (km)", fontsize=10, color=MUTE)
    for ax in panels[:-1]:
        plt.setp(ax.get_xticklabels(), visible=False)

    for ax in panels:
        for spine in ("top", "right", "left"):
            ax.spines[spine].set_visible(False)
        ax.tick_params(colors=MUTE, length=0)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor="white", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()
