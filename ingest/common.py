"""Piezas compartidas por las ingestas: zona horaria, estado en sync_state y errores."""
import logging
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ingest import db

log = logging.getLogger("ingest")

# Zona horaria para decidir a qué día pertenece un entreno (Hevy da UTC).
TZ = ZoneInfo(os.environ.get("TZ", "Europe/Madrid"))


class IngestError(Exception):
    """Fallo esperado con un mensaje que se entiende sin traceback (credenciales, MFA...)."""


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_cursor(source: str) -> str | None:
    with db.connect(readonly=False) as conn:
        row = conn.execute("SELECT cursor FROM sync_state WHERE source = ?", (source,)).fetchone()
        return row["cursor"] if row else None


def record_run(source: str, *, cursor: str | None = None, rows: int = 0, error: str | None = None) -> None:
    """Apunta el resultado de una pasada. Si hay error, el cursor no se mueve."""
    ahora = iso(now_utc())
    with db.connect(readonly=False) as conn:
        conn.execute("INSERT OR IGNORE INTO sync_state(source) VALUES (?)", (source,))
        if error:
            conn.execute(
                "UPDATE sync_state SET last_run = ?, last_error = ? WHERE source = ?",
                (ahora, error[:500], source),
            )
        else:
            conn.execute(
                "UPDATE sync_state SET last_run = ?, last_ok = ?, last_error = NULL, last_rows = ?,"
                " cursor = COALESCE(?, cursor) WHERE source = ?",
                (ahora, ahora, rows, cursor, source),
            )
