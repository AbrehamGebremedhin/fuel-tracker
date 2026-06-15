"""SQLite storage layer."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .config import DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cars (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    make        TEXT    NOT NULL,
    model       TEXT    NOT NULL,
    year        INTEGER NOT NULL,
    rated_l100  REAL,
    rated_kmpl  REAL,
    source_url  TEXT,
    rated_note  TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS fillups (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    car_id     INTEGER NOT NULL REFERENCES cars(id) ON DELETE CASCADE,
    odometer   INTEGER NOT NULL,
    liters     REAL    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS user_state (
    user_id        INTEGER PRIMARY KEY,
    active_car_id  INTEGER REFERENCES cars(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_fillups_car ON fillups(car_id, odometer);
CREATE INDEX IF NOT EXISTS idx_cars_user ON cars(user_id);
"""


@dataclass
class Car:
    id: int
    user_id: int
    make: str
    model: str
    year: int
    rated_l100: float | None
    rated_kmpl: float | None
    source_url: str | None
    rated_note: str | None

    @property
    def label(self) -> str:
        return f"{self.make} {self.model} {self.year}"


def _connect(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path: Path | None = None) -> None:
    with _connect(path) as conn:
        conn.executescript(_SCHEMA)


def _row_to_car(row: sqlite3.Row) -> Car:
    return Car(
        id=row["id"],
        user_id=row["user_id"],
        make=row["make"],
        model=row["model"],
        year=row["year"],
        rated_l100=row["rated_l100"],
        rated_kmpl=row["rated_kmpl"],
        source_url=row["source_url"],
        rated_note=row["rated_note"],
    )


# --- cars -------------------------------------------------------------------

def add_car(
    user_id: int,
    make: str,
    model: str,
    year: int,
    *,
    rated_l100: float | None = None,
    rated_kmpl: float | None = None,
    source_url: str | None = None,
    rated_note: str | None = None,
) -> int:
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO cars (user_id, make, model, year, rated_l100, rated_kmpl,
                                 source_url, rated_note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, make, model, year, rated_l100, rated_kmpl, source_url, rated_note),
        )
        return int(cur.lastrowid)


def get_car(car_id: int, user_id: int | None = None) -> Car | None:
    """Fetch a car. Pass ``user_id`` to enforce ownership (returns None if it differs)."""
    with _connect() as conn:
        if user_id is None:
            row = conn.execute("SELECT * FROM cars WHERE id = ?", (car_id,)).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM cars WHERE id = ? AND user_id = ?", (car_id, user_id)
            ).fetchone()
        return _row_to_car(row) if row else None


def list_cars(user_id: int) -> list[Car]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM cars WHERE user_id = ? ORDER BY id", (user_id,)
        ).fetchall()
        return [_row_to_car(r) for r in rows]


def delete_car(car_id: int, user_id: int) -> Car | None:
    """Delete a car (and, via cascade, its fill-ups) if owned by the user.

    Returns the deleted car, or None if it doesn't exist / isn't theirs.
    The active-car pointer is cleared automatically (ON DELETE SET NULL).
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM cars WHERE id = ? AND user_id = ?", (car_id, user_id)
        ).fetchone()
        if not row:
            return None
        car = _row_to_car(row)
        conn.execute("DELETE FROM cars WHERE id = ?", (car_id,))
        return car


def find_car(user_id: int, make: str, model: str, year: int) -> Car | None:
    with _connect() as conn:
        row = conn.execute(
            """SELECT * FROM cars
               WHERE user_id = ? AND lower(make) = lower(?)
                 AND lower(model) = lower(?) AND year = ?""",
            (user_id, make, model, year),
        ).fetchone()
        return _row_to_car(row) if row else None


def set_rated(car_id: int, *, l100: float | None, kmpl: float | None,
              source_url: str | None = None, note: str | None = None) -> None:
    with _connect() as conn:
        conn.execute(
            """UPDATE cars
               SET rated_l100 = ?, rated_kmpl = ?, source_url = ?, rated_note = ?
               WHERE id = ?""",
            (l100, kmpl, source_url, note, car_id),
        )


# --- active car -------------------------------------------------------------

def set_active(user_id: int, car_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO user_state (user_id, active_car_id) VALUES (?, ?)
               ON CONFLICT(user_id) DO UPDATE SET active_car_id = excluded.active_car_id""",
            (user_id, car_id),
        )


def get_active_car(user_id: int) -> Car | None:
    with _connect() as conn:
        row = conn.execute(
            """SELECT c.* FROM cars c
               JOIN user_state s ON s.active_car_id = c.id
               WHERE s.user_id = ?""",
            (user_id,),
        ).fetchone()
        return _row_to_car(row) if row else None


# --- fillups ----------------------------------------------------------------

def add_fillup(car_id: int, odometer: int, liters: float) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO fillups (car_id, odometer, liters) VALUES (?, ?, ?)",
            (car_id, odometer, liters),
        )
        return int(cur.lastrowid)


def get_fillups(car_id: int) -> list[tuple[int, float]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT odometer, liters FROM fillups WHERE car_id = ? ORDER BY odometer",
            (car_id,),
        ).fetchall()
        return [(r["odometer"], r["liters"]) for r in rows]


def delete_last_fillup(car_id: int) -> tuple[int, float] | None:
    """Delete the most recently *added* fill-up for a car. Returns it, or None."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, odometer, liters FROM fillups WHERE car_id = ? ORDER BY id DESC LIMIT 1",
            (car_id,),
        ).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM fillups WHERE id = ?", (row["id"],))
        return (row["odometer"], row["liters"])
