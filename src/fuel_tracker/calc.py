"""Fuel-economy calculations (fill-to-full method)."""

from __future__ import annotations

from dataclasses import dataclass


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
