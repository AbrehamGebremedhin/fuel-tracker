"""One-off dump of the live Turso database to a local JSON file.

Run before any schema migration:  uv run python scripts/backup_turso.py
ponytail: manual pre-migration snapshot, not a backup schedule.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from fuel_tracker import config
from fuel_tracker.turso import TursoConnection

TABLES = ["cars", "fillups", "user_state"]


def main() -> None:
    if not (config.TURSO_DATABASE_URL and config.TURSO_AUTH_TOKEN):
        raise SystemExit("TURSO_DATABASE_URL/TURSO_AUTH_TOKEN not set in .env")

    conn = TursoConnection(config.TURSO_DATABASE_URL, config.TURSO_AUTH_TOKEN)
    dump = {}
    for table in TABLES:
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        dump[table] = rows
        print(f"{table}: {len(rows)} rows")

    out_dir = Path(__file__).resolve().parents[1] / "backups"
    out_dir.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = out_dir / f"turso_backup_{stamp}.json"
    out_path.write_text(json.dumps(dump, indent=2, default=str))
    print(f"backup written to {out_path}")


if __name__ == "__main__":
    main()
