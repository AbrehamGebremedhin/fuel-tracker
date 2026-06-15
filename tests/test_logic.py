"""Logic tests that run without a Telegram token or network.

Run with:  uv run python tests/test_logic.py
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

# Point the DB at a throwaway file BEFORE importing the package.
_tmp = Path(tempfile.mkdtemp()) / "test.db"
os.environ["FUEL_TRACKER_DB"] = str(_tmp)

from fuel_tracker import db                      # noqa: E402
from fuel_tracker.calc import compute_stats       # noqa: E402
from fuel_tracker.parsing import (                # noqa: E402
    parse_addcar,
    parse_fillup_line,
    parse_fillups,
)
from fuel_tracker.sources import autodata, goonet  # noqa: E402
from fuel_tracker.sources.base import (           # noqa: E402
    clean_variant_name,
    l100_to_economy,
    mpg_to_economy,
)

# The exact lines from the user's notes (mixed 'liter'/'Liter', commas, units).
NOTES = """
14.01 liter @ 92184 km
15.14 liter @ 92374 km
13.61 liter @ 92476 km
14.95 liter @ 92644 km
13.62 liter @ 92779 km
12.6 liter @ 92932 km
9.6 liter @ 93043 km
10.4 liter @ 93127 km
9.39 liter @ 93258 km
9.93 liter @ 93373 km
14.45 liter @ 93543 km
24.68 liter @ 93810 km
15.52 Liter @ 93979 km
13.64 Liter @ 94133 km
10.78 liter @ 94286 km
16.4 liter @ 94442 km
13.75 Liter @ 94615 km
"""


def almost(a: float, b: float, tol: float = 0.01) -> bool:
    return abs(a - b) <= tol


def test_parsing_variants():
    assert parse_fillup_line("14.01 @ 92184").liters == 14.01
    assert parse_fillup_line("14.01 liter @ 92184 km").odometer == 92184
    assert parse_fillup_line("13.75 Liter @ 94615 km").liters == 13.75
    assert parse_fillup_line("16.4L@94442").odometer == 94442
    assert parse_fillup_line("12,6 litres @ 92,932 km").liters == 12.6
    assert parse_fillup_line("12,6 litres @ 92,932 km").odometer == 92932
    assert parse_fillup_line("just a title") is None
    print("ok: parsing variants")


def test_cost_parsing():
    assert parse_fillup_line("14.01 @ 92184").cost is None
    assert parse_fillup_line("14.01 @ 92184 = 1200").cost == 1200
    assert parse_fillup_line("14.01 @ 92184 km = 1,200").cost == 1200
    # Price per litre -> total = price * liters.
    assert almost(parse_fillup_line("14.01 @ 92184 @ 85/L").cost, 14.01 * 85)
    assert almost(parse_fillup_line("20 @ 100000 @ 50 per L").cost, 1000.0)
    # Decimal comma in the amount.
    assert parse_fillup_line("10 @ 5000 = 85,5").cost == 85.5
    # Odometer is still parsed correctly alongside a cost.
    p = parse_fillup_line("14.01 @ 92184 = 1200")
    assert p.liters == 14.01 and p.odometer == 92184
    print("ok: cost parsing")


def test_cost_calc():
    # First fill-up is the baseline (no leg); the next two carry cost.
    fillups = [(1000, 10.0, None), (1200, 20.0, 1000.0), (1500, 15.0, 750.0)]
    s = compute_stats(fillups)
    assert s.has_cost
    assert s.total_cost == 1750
    assert almost(s.avg_cost_per_100, 350.0)   # 1750 / 500 km * 100
    assert almost(s.avg_price_per_l, 50.0)      # 1750 / 35 L
    assert almost(s.legs[0].cost_per_100, 500.0)
    assert almost(s.legs[0].price_per_l, 50.0)

    # No costs at all -> cost aggregates are absent.
    s2 = compute_stats([(0, 10.0, None), (200, 12.0, None)])
    assert not s2.has_cost and s2.total_cost is None

    # Partial costs: only legs that have a cost are aggregated.
    s3 = compute_stats([(0, 10.0, None), (200, 20.0, 1000.0), (500, 15.0, None)])
    assert s3.has_cost and s3.total_cost == 1000
    print("ok: cost calc")


def test_addcar_parse():
    c = parse_addcar("Toyota Corolla 2018")
    assert (c.make, c.model, c.year) == ("Toyota", "Corolla", 2018)
    c2 = parse_addcar("Mercedes Benz C200 2019")
    assert c2.make == "Mercedes" and c2.year == 2019 and "Benz" in c2.model
    assert parse_addcar("Toyota 2018") is None  # no model word
    assert parse_addcar("Toyota Corolla") is None  # no year
    print("ok: addcar parse")


def test_full_flow_matches_notes():
    db.init_db()
    parsed, bad = parse_fillups(NOTES)
    assert not bad, f"unexpected unparsed lines: {bad}"
    assert len(parsed) == 17

    car_id = db.add_car(1, "Toyota", "Corolla", 2018)
    db.set_active(1, car_id)
    for p in parsed:
        db.add_fillup(car_id, p.odometer, p.liters)

    s = compute_stats(db.get_fillups(car_id))
    assert s is not None
    assert s.fillup_count == 17
    assert s.total_distance == 2431                # 94615 - 92184, matches the HTML

    # Latest tank must match the dashboard exactly (7.95 L/100, 12.58 km/L).
    latest = s.latest_leg
    assert almost(latest.l_per_100, 7.95), latest.l_per_100
    assert almost(latest.km_per_l, 12.58), latest.km_per_l

    # Fill-to-full overall (fuel used = sum of all fills except the baseline = 218.46 L).
    # This reproduces the dashboard's headline 8.99 L/100 / 11.13 km/L exactly.
    assert almost(s.total_fuel, 218.46), s.total_fuel
    assert almost(s.overall_km_per_l, 11.13, tol=0.02), s.overall_km_per_l
    assert almost(s.overall_l_per_100, 8.99, tol=0.02), s.overall_l_per_100

    # Worst leg is 92374 -> 92476: only 102 km on a 13.61 L fill.
    assert almost(s.worst_km_per_l, 102 / 13.61, tol=0.02), s.worst_km_per_l
    print(
        f"ok: full flow — overall {s.overall_km_per_l} km/L, "
        f"latest {latest.km_per_l} km/L, best {s.best_km_per_l}, worst {s.worst_km_per_l}"
    )


def test_user_isolation():
    db.init_db()
    # Two different Telegram users.
    a_car = db.add_car(101, "Toyota", "Corolla", 2018)
    b_car = db.add_car(202, "Honda", "Civic", 2016)
    db.set_active(101, a_car)
    db.set_active(202, b_car)
    db.add_fillup(a_car, 1000, 10.0)
    db.add_fillup(a_car, 1200, 12.0)
    db.add_fillup(b_car, 5000, 30.0)

    # Each user sees only their own car(s).
    assert [c.id for c in db.list_cars(101)] == [a_car]
    assert [c.id for c in db.list_cars(202)] == [b_car]

    # Active car is per-user.
    assert db.get_active_car(101).id == a_car
    assert db.get_active_car(202).id == b_car

    # User B cannot fetch user A's car by id (ownership enforced).
    assert db.get_car(a_car, user_id=202) is None
    assert db.get_car(a_car, user_id=101).id == a_car

    # History does not bleed across users.
    assert len(db.get_fillups(a_car)) == 2
    assert len(db.get_fillups(b_car)) == 1
    print("ok: per-user isolation")


def test_economy_conversions():
    # 31 US mpg combined -> ~13.18 km/L (matches fueleconomy.gov for the Corolla).
    e = mpg_to_economy(31, "epa")
    assert almost(e.km_per_l, 13.18), e.km_per_l
    # 6.9 L/100 km -> ~14.49 km/L (auto-data combined for the 1.8 VVT-i).
    e2 = l100_to_economy(6.9, "auto-data")
    assert almost(e2.km_per_l, 14.49), e2.km_per_l
    assert clean_variant_name("1.5i 16V (110 Hp) Automatic 1999 - 2005") == "1.5i 16V (110 Hp) Automatic"
    assert clean_variant_name("1.8 VVT-i (140 Hp) CVTi-S 2018 -") == "1.8 VVT-i (140 Hp) CVTi-S"
    print("ok: economy conversions")


def test_variant_economy_parser():
    # Mirrors the real auto-data variant spec table; combined row must win.
    html = """
    <table>
      <tr><th>Fuel consumption (economy) - urban</th><td>7.8 l/100 km 30.2 US mpg 12.8 km/l</td></tr>
      <tr><th>Fuel consumption (economy) - extra urban</th><td>5.9 l/100 km 39.9 US mpg 16.9 km/l</td></tr>
      <tr><th>Fuel consumption (economy) - combined</th><td>6.9 l/100 km 34.1 US mpg 14.5 km/l</td></tr>
    </table>
    """
    eco = autodata._parse_variant_economy(html, "1.8 VVT-i (140 Hp)")
    assert eco is not None
    assert almost(eco.l_per_100, 6.9), eco.l_per_100
    assert almost(eco.km_per_l, 14.49), eco.km_per_l

    # Falls back to (urban + extra)/2 when there's no combined row.
    html2 = """
    <table>
      <tr><th>Fuel consumption (economy) - urban</th><td>8.0 l/100 km</td></tr>
      <tr><th>Fuel consumption (economy) - extra urban</th><td>6.0 l/100 km</td></tr>
    </table>
    """
    eco2 = autodata._parse_variant_economy(html2, "x")
    assert almost(eco2.l_per_100, 7.0), eco2.l_per_100

    # No consumption rows at all (e.g. old Platz) -> None.
    assert autodata._parse_variant_economy("<table></table>", "x") is None
    print("ok: variant economy parser")


def test_jdm_variant_hints():
    assert goonet.variant_hints("1.0i 16V (70 Hp) Automatic") == (1000, "AT")
    assert goonet.variant_hints("1.5i 16V (110 Hp)") == (1500, "MT")
    assert goonet.variant_hints("1.3 (88 Hp) CVT") == (1300, "AT")
    print("ok: jdm variant hints")


def test_jdm_grade_matching():
    # Mirrors goo-net's grade table: 1.0L FF in MT (21.5) and AT (19.6).
    html = """
    <table class="tbl_type03">
      <tr><th>g</th><td>CBA-SCP11</td><td>997cc</td><td>5MT</td><td>FF</td><td>21.5km/l</td></tr>
      <tr><th>g</th><td>CBA-SCP11</td><td>997cc</td><td>4AT</td><td>FF</td><td>19.6km/l</td></tr>
      <tr><th>g</th><td>CBA-NCP12</td><td>1496cc</td><td>4AT</td><td>FF</td><td>18.0km/l</td></tr>
    </table>
    """
    grades = goonet._parse_grades(html)
    assert len(grades) == 3
    # 1.0L automatic must resolve to the AT row, not the MT or 1.5L rows.
    assert goonet._select(grades, 1000, "AT") == [19.6]
    assert goonet._select(grades, 1000, "MT") == [21.5]
    assert goonet._select(grades, 1500, "AT") == [18.0]
    print("ok: jdm grade matching")


def test_undo():
    car_id = db.add_car(2, "Honda", "Civic", 2016)
    db.add_fillup(car_id, 1000, 10.0)
    db.add_fillup(car_id, 1200, 12.0)
    removed = db.delete_last_fillup(car_id)
    assert removed == (1200, 12.0)
    assert len(db.get_fillups(car_id)) == 1
    print("ok: undo")


def test_short_gen_tag():
    from fuel_tracker.keyboards import short_gen
    assert short_gen("Toyota Corolla XII (E210, facelift 2022)", "Toyota", "Corolla") == "XII (E210, facelift 2022)"
    assert short_gen("Toyota Corolla Axio", "Toyota", "Corolla") == "Axio"
    assert short_gen("Toyota Corolla iM", "Toyota", "Corolla") == "iM"
    assert short_gen("Toyota Corolla XI (E160, E170)", "Toyota", "Corolla") == "XI (E160, E170)"
    # Never ends with a dangling open paren (the old bug).
    assert not short_gen("Toyota Corolla XII (E210)", "Toyota", "Corolla").endswith("(")
    print("ok: short generation tag")


def test_group_variants():
    from fuel_tracker.keyboards import group_variants
    from fuel_tracker.sources.base import Variant
    variants = [
        Variant("1.8i (122 Hp) Hybrid e-CVT", "u1", "Toyota Corolla XII (E210)"),
        Variant("1.8 VVT-i (140 Hp)", "u2", "Toyota Corolla XII (E210)"),
        Variant("1.5 (109 Hp)", "u3", "Toyota Corolla Axio"),
    ]
    groups = group_variants(variants, "Toyota", "Corolla")
    # Two generations -> two groups, each with a header.
    assert [g["header"] for g in groups] == ["XII (E210)", "Axio"]
    # Within the E210 group the hybrid is sorted last; indices map back to originals.
    e210 = groups[0]["items"]
    assert e210[-1] == (0, "1.8i (122 Hp) Hybrid e-CVT")
    assert (1, "1.8 VVT-i (140 Hp)") in e210

    # Single generation -> no headers.
    single = group_variants(
        [Variant("A", "u", "Gen X"), Variant("B", "u", "Gen X")], "Mk", "Md"
    )
    assert single[0]["header"] is None
    print("ok: group variants (gen headers + hybrid-last)")


def test_turso_codec():
    from fuel_tracker import turso
    # Argument encoding (integers go over the wire as strings in Hrana).
    assert turso._enc_arg(None) == {"type": "null"}
    assert turso._enc_arg(42) == {"type": "integer", "value": "42"}
    assert turso._enc_arg(1.5) == {"type": "float", "value": 1.5}
    assert turso._enc_arg("hi") == {"type": "text", "value": "hi"}
    # Cell decoding.
    assert turso._dec_cell({"type": "integer", "value": "7"}) == 7
    assert turso._dec_cell({"type": "float", "value": "1.5"}) == 1.5
    assert turso._dec_cell({"type": "null"}) is None
    assert turso._dec_cell({"type": "text", "value": "x"}) == "x"
    # Parsing a Hrana execute result into dict rows + lastrowid.
    entry = {"response": {"result": {
        "cols": [{"name": "id"}, {"name": "liters"}, {"name": "cost"}],
        "rows": [[{"type": "integer", "value": "1"},
                  {"type": "float", "value": "14.01"},
                  {"type": "null"}]],
        "last_insert_rowid": "1",
    }}}
    cur = turso.TursoConnection._to_cursor(entry)
    row = cur.fetchone()
    assert row == {"id": 1, "liters": 14.01, "cost": None}
    assert cur.lastrowid == 1
    assert turso.http_url("libsql://db-org.turso.io") == "https://db-org.turso.io"
    assert turso.http_url("db-org.turso.io") == "https://db-org.turso.io"        # bare host
    assert turso.http_url('"libsql://db-org.turso.io"') == "https://db-org.turso.io"  # quoted
    assert turso.http_url("https://db-org.turso.io/") == "https://db-org.turso.io"
    print("ok: turso codec")


def test_delete_car():
    db.init_db()
    car_id = db.add_car(303, "Mazda", "Demio", 2014)
    db.set_active(303, car_id)
    db.add_fillup(car_id, 100, 5.0)
    db.add_fillup(car_id, 300, 10.0)

    # Another user cannot delete it.
    assert db.delete_car(car_id, user_id=999) is None
    assert db.get_car(car_id) is not None

    # Owner deletes: car gone, fill-ups cascade, active pointer cleared.
    deleted = db.delete_car(car_id, user_id=303)
    assert deleted is not None and deleted.id == car_id
    assert db.get_car(car_id) is None
    assert db.get_fillups(car_id) == []
    assert db.get_active_car(303) is None
    print("ok: delete car (cascade + ownership)")


if __name__ == "__main__":
    test_parsing_variants()
    test_cost_parsing()
    test_cost_calc()
    test_addcar_parse()
    test_full_flow_matches_notes()
    test_user_isolation()
    test_economy_conversions()
    test_variant_economy_parser()
    test_jdm_variant_hints()
    test_jdm_grade_matching()
    test_undo()
    test_short_gen_tag()
    test_group_variants()
    test_turso_codec()
    test_delete_car()
    print("\nAll logic tests passed.")
