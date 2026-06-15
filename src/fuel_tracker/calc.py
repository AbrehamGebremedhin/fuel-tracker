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

    @property
    def latest_leg(self) -> Leg | None:
        return self.legs[-1] if self.legs else None


def compute_leg(odo_from: int, odo_to: int, liters: float) -> Leg | None:
    """A leg's economy: liters added at the *current* stop covered the distance just driven."""
    distance = odo_to - odo_from
    if distance <= 0 or liters <= 0:
        return None
    return Leg(
        odo_from=odo_from,
        odo_to=odo_to,
        distance=distance,
        liters=liters,
        l_per_100=round(liters / distance * 100, 2),
        km_per_l=round(distance / liters, 2),
    )


def compute_stats(fillups: list[tuple[int, float]]) -> Stats | None:
    """`fillups` is a list of (odometer, liters); order doesn't matter, we sort by odometer.

    Returns None if there aren't enough points to measure a leg.
    """
    pts = sorted(fillups, key=lambda x: x[0])
    if len(pts) < 2:
        return None

    legs: list[Leg] = []
    for (odo_a, _la), (odo_b, lb) in zip(pts, pts[1:]):
        leg = compute_leg(odo_a, odo_b, lb)
        if leg:
            legs.append(leg)

    if not legs:
        return None

    total_distance = pts[-1][0] - pts[0][0]
    total_fuel = sum(leg.liters for leg in legs)
    kmpls = [leg.km_per_l for leg in legs]

    return Stats(
        fillup_count=len(pts),
        total_distance=total_distance,
        total_fuel=round(total_fuel, 2),
        overall_l_per_100=round(total_fuel / total_distance * 100, 2),
        overall_km_per_l=round(total_distance / total_fuel, 2),
        best_km_per_l=max(kmpls),
        worst_km_per_l=min(kmpls),
        legs=legs,
    )
