"""Fuel-economy calculations (fill-to-full method)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta


@dataclass
class Leg:
    odo_from: int
    odo_to: int
    distance: int
    liters: float
    l_per_100: float
    km_per_l: float
    cost: float | None = None
    cost_per_100: float | None = None   # cost per 100 km driven
    price_per_l: float | None = None


@dataclass
class Stats:
    fillup_count: int
    total_distance: int
    total_fuel: float          # fuel consumed over the measured distance (excludes baseline fill)
    overall_l_per_100: float
    overall_km_per_l: float
    best_km_per_l: float
    worst_km_per_l: float
    legs: list[Leg]
    # Cost aggregates (None when no fill-up has a recorded cost).
    total_cost: float | None = None
    avg_cost_per_100: float | None = None
    avg_price_per_l: float | None = None

    @property
    def latest_leg(self) -> Leg | None:
        return self.legs[-1] if self.legs else None

    @property
    def has_cost(self) -> bool:
        return self.total_cost is not None


def compute_leg(odo_from: int, odo_to: int, liters: float,
                cost: float | None = None) -> Leg | None:
    """A leg's economy: liters added at the *current* stop covered the distance just driven."""
    distance = odo_to - odo_from
    if distance <= 0 or liters <= 0:
        return None
    leg = Leg(
        odo_from=odo_from,
        odo_to=odo_to,
        distance=distance,
        liters=liters,
        l_per_100=round(liters / distance * 100, 2),
        km_per_l=round(distance / liters, 2),
    )
    if cost is not None and cost > 0:
        leg.cost = round(cost, 2)
        leg.cost_per_100 = round(cost / distance * 100, 2)
        leg.price_per_l = round(cost / liters, 2)
    return leg


@dataclass
class TimeStats:
    span_days: int               # first to last fill-up
    km_per_day: float
    liters_per_month: float
    days_between_fills: float     # average gap between fill-ups
    next_fill_date: date          # projected from km/day + average tank distance
    monthly_distance: float
    monthly_fuel: float
    monthly_cost: float | None = None   # None when no fill-up has a cost


def _parse_dt(s: str) -> datetime | None:
    # SQLite datetime('now') -> "YYYY-MM-DD HH:MM:SS"; fromisoformat handles it on 3.11+.
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def time_stats(fillups: list[tuple], stats: Stats) -> TimeStats | None:
    """Time-based cadence and a naive linear projection of the next fill-up.

    `fillups` rows are (odometer, liters, cost, created_at); `stats` is the matching
    odometer-based result. Returns None when there aren't ≥2 dated fill-ups spanning
    real time (e.g. a bulk import where every row shares one timestamp).
    """
    # A bulk import enters many fill-ups at one instant, so they share a created_at.
    # Those rows carry no real time signal — the distance they cover was driven before
    # the import, not in the seconds it took to insert them. Collapse each identical
    # timestamp to one point (the odometer as of that instant) so an import counts as a
    # single reading instead of inflating the daily rate. ponytail: dedupe by timestamp.
    by_dt: dict[datetime, float] = {}
    for p in fillups:
        if len(p) > 3 and (dt := _parse_dt(p[3])):
            by_dt[dt] = max(by_dt.get(dt, p[0]), p[0])
    dated = sorted(by_dt.items(), key=lambda x: x[0])
    if len(dated) < 2:
        return None
    first_dt, first_odo = dated[0]
    last_dt, last_odo = dated[-1]
    span = last_dt - first_dt
    span_days = span.days
    if span.total_seconds() <= 0:
        return None  # all logged at once — no time signal to analyse

    span_d = span.total_seconds() / 86400
    km_per_day = (last_odo - first_odo) / span_d
    # A bulk import inserts rows seconds apart, so created_at spans seconds — not days —
    # while the odometer jumps thousands of km, implying an absurd daily rate. No car
    # averages this; treat it as "no real time signal" rather than project nonsense.
    # ponytail: 2000 km/day floor; raise it only if someone genuinely road-trips that hard.
    if km_per_day > 2000:
        return None
    days_between = span_d / (len(dated) - 1)

    # Project the next fill: drive an average tank's distance at the recent daily rate.
    avg_tank_km = stats.total_distance / len(stats.legs)
    days_to_next = avg_tank_km / km_per_day if km_per_day > 0 else days_between
    next_fill_date = (last_dt + timedelta(days=days_to_next)).date()

    monthly_distance = km_per_day * 30
    monthly_fuel = stats.overall_l_per_100 / 100 * monthly_distance
    monthly_cost = (
        stats.avg_cost_per_100 / 100 * monthly_distance
        if stats.avg_cost_per_100 is not None else None
    )

    return TimeStats(
        span_days=span_days,
        km_per_day=round(km_per_day, 1),
        liters_per_month=round(monthly_fuel, 1),
        days_between_fills=round(days_between, 1),
        next_fill_date=next_fill_date,
        monthly_distance=round(monthly_distance),
        monthly_fuel=round(monthly_fuel, 1),
        monthly_cost=round(monthly_cost) if monthly_cost is not None else None,
    )


def compute_stats(fillups: list[tuple]) -> Stats | None:
    """`fillups` is a list of (odometer, liters[, cost]); order doesn't matter.

    Returns None if there aren't enough points to measure a leg.
    """
    pts = sorted(fillups, key=lambda x: x[0])
    if len(pts) < 2:
        return None

    legs: list[Leg] = []
    for prev, cur in zip(pts, pts[1:]):
        cost = cur[2] if len(cur) > 2 else None
        leg = compute_leg(prev[0], cur[0], cur[1], cost)
        if leg:
            legs.append(leg)

    if not legs:
        return None

    total_distance = pts[-1][0] - pts[0][0]
    total_fuel = sum(leg.liters for leg in legs)
    kmpls = [leg.km_per_l for leg in legs]

    cost_legs = [leg for leg in legs if leg.cost is not None]
    total_cost = avg_cost_per_100 = avg_price_per_l = None
    if cost_legs:
        total_cost = round(sum(leg.cost for leg in cost_legs), 2)
        cost_distance = sum(leg.distance for leg in cost_legs)
        cost_liters = sum(leg.liters for leg in cost_legs)
        avg_cost_per_100 = round(total_cost / cost_distance * 100, 2)
        avg_price_per_l = round(total_cost / cost_liters, 2)

    return Stats(
        fillup_count=len(pts),
        total_distance=total_distance,
        total_fuel=round(total_fuel, 2),
        overall_l_per_100=round(total_fuel / total_distance * 100, 2),
        overall_km_per_l=round(total_distance / total_fuel, 2),
        best_km_per_l=max(kmpls),
        worst_km_per_l=min(kmpls),
        legs=legs,
        total_cost=total_cost,
        avg_cost_per_100=avg_cost_per_100,
        avg_price_per_l=avg_price_per_l,
    )
