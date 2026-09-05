"""Configuration loaded from environment / .env."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

# SQLite database location. Override with FUEL_TRACKER_DB.
DB_PATH: Path = Path(
    os.getenv("FUEL_TRACKER_DB", Path(__file__).resolve().parents[2] / "fuel_tracker.db")
)


def require_token() -> str:
    if not BOT_TOKEN:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN is not set. Copy .env.example to .env and add your "
            "token from @BotFather."
        )
    return BOT_TOKEN
