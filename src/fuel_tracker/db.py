"""SQLite storage layer."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import config
from .config import DB_PATH
from .turso import TursoConnection

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
    goal_kmpl   REAL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS fillups (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    car_id     INTEGER NOT NULL REFERENCES cars(id) ON DELETE CASCADE,
    odometer   INTEGER NOT NULL,
    liters     REAL    NOT NULL,
    cost       REAL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    is_full    INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS user_state (
    user_id             INTEGER PRIMARY KEY,
    active_car_id       INTEGER REFERENCES cars(id) ON DELETE SET NULL,
    units               TEXT    NOT NULL DEFAULT 'metric',
    last_reminder_sent  TEXT
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
    goal_kmpl: float | None = None

    @property
    def label(self) -> str:
        return f"{self.make} {self.model} {self.year}"


def _connect(path: Path | None = None):
    if config.TURSO_DATABASE_URL and config.TURSO_AUTH_TOKEN:
        return TursoConnection(config.TURSO_DATABASE_URL, config.TURSO_AUTH_TOKEN)
    conn = sqlite3.connect(path or DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path: Path | None = None) -> None:
    with _connect(path) as conn:
        conn.executescript(_SCHEMA)
        # Migrate older databases that predate the cost / is_full columns.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(fillups)")}
        if "cost" not in cols:
            conn.execute("ALTER TABLE fillups ADD COLUMN cost REAL")
        if "is_full" not in cols:
            conn.execute("ALTER TABLE fillups ADD COLUMN is_full INTEGER NOT NULL DEFAULT 1")
            # Every fill-up logged before this column existed was assumed full tank
            # (the fill-to-full method had no other option) — backfill explicitly.
            conn.execute("UPDATE fillups SET is_full = 1 WHERE is_full IS NULL")

        car_cols = {r["name"] for r in conn.execute("PRAGMA table_info(cars)")}
        if "goal_kmpl" not in car_cols:
            conn.execute("ALTER TABLE cars ADD COLUMN goal_kmpl REAL")

        state_cols = {r["name"] for r in conn.execute("PRAGMA table_info(user_state)")}
        if "units" not in state_cols:
            conn.execute("ALTER TABLE user_state ADD COLUMN units TEXT NOT NULL DEFAULT 'metric'")
        if "last_reminder_sent" not in state_cols:
            conn.execute("ALTER TABLE user_state ADD COLUMN last_reminder_sent TEXT")


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
        goal_kmpl=row["goal_kmpl"],
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


def _owns_car(conn, car_id: int, user_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM cars WHERE id = ? AND user_id = ?", (car_id, user_id)
    ).fetchone() is not None


def update_car_info(car_id: int, user_id: int, make: str, model: str, year: int) -> bool:
    """Fix a car's make/model/year in place (keeps its fill-up history and rated data)."""
    with _connect() as conn:
        if not _owns_car(conn, car_id, user_id):
            return False
        conn.execute(
            "UPDATE cars SET make = ?, model = ?, year = ? WHERE id = ?",
            (make, model, year, car_id),
        )
        return True


def set_goal(car_id: int, user_id: int, goal_kmpl: float | None) -> bool:
    with _connect() as conn:
        if not _owns_car(conn, car_id, user_id):
            return False
        conn.execute("UPDATE cars SET goal_kmpl = ? WHERE id = ?", (goal_kmpl, car_id))
        return True


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

def add_fillup(car_id: int, odometer: int, liters: float, cost: float | None = None,
               is_full: bool = True) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO fillups (car_id, odometer, liters, cost, is_full) VALUES (?, ?, ?, ?, ?)",
            (car_id, odometer, liters, cost, int(is_full)),
        )
        return int(cur.lastrowid)


def get_fillups(car_id: int) -> list[tuple[int, float, float | None, str, bool]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT odometer, liters, cost, created_at, is_full FROM fillups "
            "WHERE car_id = ? ORDER BY odometer",
            (car_id,),
        ).fetchall()
        return [
            (r["odometer"], r["liters"], r["cost"], r["created_at"], bool(r["is_full"]))
            for r in rows
        ]


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


def list_fillups_with_id(car_id: int, limit: int | None = 20) -> list[tuple]:
    """Recent raw fill-up rows as (id, odometer, liters, cost, created_at, is_full),
    newest first — for showing a user which id to /delfill. ``limit=None`` for all rows
    (used by /export)."""
    with _connect() as conn:
        sql = "SELECT id, odometer, liters, cost, created_at, is_full FROM fillups WHERE car_id = ? ORDER BY id DESC"
        params: tuple = (car_id,)
        if limit is not None:
            sql += " LIMIT ?"
            params = (car_id, limit)
        rows = conn.execute(sql, params).fetchall()
        return [
            (r["id"], r["odometer"], r["liters"], r["cost"], r["created_at"], bool(r["is_full"]))
            for r in rows
        ]


def delete_fillup(fillup_id: int, car_id: int) -> tuple[int, float] | None:
    """Delete one fill-up by id, scoped to a car the caller already owns. Returns
    (odometer, liters), or None if it doesn't exist / belongs to another car."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT odometer, liters FROM fillups WHERE id = ? AND car_id = ?",
            (fillup_id, car_id),
        ).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM fillups WHERE id = ?", (fillup_id,))
        return (row["odometer"], row["liters"])


# --- per-user preferences -----------------------------------------------------

def get_units(user_id: int) -> str:
    with _connect() as conn:
        row = conn.execute(
            "SELECT units FROM user_state WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row["units"] if row and row["units"] else "metric"


def set_units(user_id: int, units: str) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO user_state (user_id, units) VALUES (?, ?)
               ON CONFLICT(user_id) DO UPDATE SET units = excluded.units""",
            (user_id, units),
        )


def get_last_reminder(user_id: int) -> str | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT last_reminder_sent FROM user_state WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row["last_reminder_sent"] if row else None


def set_last_reminder(user_id: int, day: str) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO user_state (user_id, last_reminder_sent) VALUES (?, ?)
               ON CONFLICT(user_id) DO UPDATE SET last_reminder_sent = excluded.last_reminder_sent""",
            (user_id, day),
        )
