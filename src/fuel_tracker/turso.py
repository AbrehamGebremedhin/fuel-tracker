"""Minimal Turso/libSQL backend over the HTTP (Hrana v2) API.

Talks to a Turso database via plain HTTPS + httpx — no native libsql dependency, so it
builds anywhere. It exposes just enough of the sqlite3 connection/cursor interface for
``db.py`` to use it interchangeably with the local sqlite3 backend.

Enabled when TURSO_DATABASE_URL (libsql://… or https://…) and TURSO_AUTH_TOKEN are set.
"""

from __future__ import annotations

import base64
import re
from typing import Any, Sequence

import httpx

# One shared, keep-alive client (TLS handshake reuse) for the whole process.
_client = httpx.Client(timeout=30.0)


def http_url(database_url: str) -> str:
    """Normalise a Turso URL to an https endpoint.

    Tolerant of a bare host, surrounding quotes/backslashes, and stray whitespace or
    newlines from a mangled copy-paste or shell-escaped env var (a URL never legitimately
    contains whitespace, quotes or backslashes). Strips wrapping junk from both ends —
    e.g. a value like `"https://db.turso.io"\\` becomes `https://db.turso.io`.
    """
    url = re.sub(r"\s+", "", database_url).strip("'\"\\`")
    if url.startswith("libsql://"):
        url = "https://" + url[len("libsql://"):]
    elif not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url.rstrip("/")


def _enc_arg(value: Any) -> dict:
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "integer", "value": str(int(value))}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    if isinstance(value, (bytes, bytearray)):
        return {"type": "blob", "base64": base64.b64encode(bytes(value)).decode()}
    return {"type": "text", "value": str(value)}


def _dec_cell(cell: dict) -> Any:
    t = cell.get("type")
    if t == "null":
        return None
    if t == "integer":
        return int(cell["value"])
    if t == "float":
        return float(cell["value"])
    if t == "blob":
        return base64.b64decode(cell["base64"])
    return cell.get("value")


class TursoError(RuntimeError):
    pass


class _Cursor:
    """Mimics the slice of sqlite3.Cursor that db.py relies on."""

    def __init__(self, rows: list[dict], last_insert_rowid: int | None):
        self._rows = rows
        self.lastrowid = last_insert_rowid

    def fetchone(self) -> dict | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[dict]:
        return self._rows

    def __iter__(self):
        return iter(self._rows)


class TursoConnection:
    """sqlite3-like connection backed by the Turso HTTP pipeline endpoint."""

    def __init__(self, database_url: str, auth_token: str):
        self._endpoint = f"{http_url(database_url)}/v2/pipeline"
        self._headers = {"Authorization": f"Bearer {auth_token}"}

    # context-manager parity with `with sqlite3.connect(...) as conn:`
    def __enter__(self) -> "TursoConnection":
        return self

    def __exit__(self, *exc) -> None:
        return None

    def _pipeline(self, statements: list[tuple[str, Sequence]]) -> list[dict]:
        # foreign_keys must be enabled per-connection; prepend it every time so
        # ON DELETE CASCADE works for the statement(s) in this pipeline.
        reqs: list[dict] = [{"type": "execute", "stmt": {"sql": "PRAGMA foreign_keys=ON"}}]
        for sql, params in statements:
            reqs.append({"type": "execute", "stmt": {
                "sql": sql, "args": [_enc_arg(a) for a in params]}})
        reqs.append({"type": "close"})

        resp = _client.post(self._endpoint, headers=self._headers, json={"requests": reqs})
        resp.raise_for_status()
        results = resp.json().get("results", [])
        for r in results:
            if r.get("type") == "error":
                raise TursoError(r.get("error", {}).get("message", "unknown Turso error"))
        return results[1:]  # drop the PRAGMA result

    @staticmethod
    def _to_cursor(result_entry: dict) -> _Cursor:
        result = result_entry["response"]["result"]
        cols = [c["name"] for c in result["cols"]]
        rows = [
            {cols[i]: _dec_cell(cell) for i, cell in enumerate(row)}
            for row in result["rows"]
        ]
        lri = result.get("last_insert_rowid")
        return _Cursor(rows, int(lri) if lri not in (None, "") else None)

    def execute(self, sql: str, params: Sequence = ()) -> _Cursor:
        return self._to_cursor(self._pipeline([(sql, params)])[0])

    def executescript(self, script: str) -> None:
        statements = [s.strip() for s in script.split(";") if s.strip()]
        self._pipeline([(s, ()) for s in statements])
