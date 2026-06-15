"""Entry point: `uv run fuel-tracker` or `python -m fuel_tracker`."""

from __future__ import annotations

from .bot import run


def main() -> None:
    run()


if __name__ == "__main__":
    main()
