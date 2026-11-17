"""personal_data.db: esquema, migración y conexiones.

Este repo es el DUEÑO del esquema: es el único que escribe en la BD. El MCP
(vps-didac/mcp-server) la monta y la lee en modo solo lectura; si cambias una tabla
aquí, actualiza también la docstring de query_personal_db en tools/health_db.py de allí.
"""
import logging
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(os.environ.get("DB_PATH", "/data/personal_data.db"))

log = logging.getLogger("db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS hevy_sets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workout_id TEXT NOT NULL,
    date TEXT,                    -- fecha local (TZ) del inicio del entreno, YYYY-MM-DD
    start_time TEXT,              -- ISO 8601 UTC tal cual lo da Hevy
    workout_title TEXT,
    exercise_index INTEGER NOT NULL,
    exercise_title TEXT,
    exercise_template_id TEXT,
    set_index INTEGER NOT NULL,
    set_type TEXT,                -- normal, warmup, failure, dropset
    weight_kg REAL,
    reps INTEGER,
    rpe REAL,
    distance_m REAL,
    duration_s REAL,
    UNIQUE(workout_id, exercise_index, set_index)
);
CREATE INDEX IF NOT EXISTS idx_hevy_sets_date ON hevy_sets(date);

CREATE TABLE IF NOT EXISTS garmin_daily (
    date TEXT PRIMARY KEY,
    sleep_score INTEGER,
    sleep_hours REAL,
    hrv_avg REAL,
    resting_hr INTEGER,
    body_battery_max INTEGER,
    body_battery_min INTEGER,
    steps INTEGER,
    stress_avg INTEGER,
    synced_at TEXT
);

CREATE TABLE IF NOT EXISTS cardio_activities (
    activity_id TEXT PRIMARY KEY,
    date TEXT,
    start_time TEXT,              -- hora local de Garmin
    name TEXT,
    sport_type TEXT,
    duration_min REAL,
    distance_km REAL,
    avg_hr INTEGER,
    max_hr INTEGER,
    calories REAL,
    elevation_gain_m REAL,
    tss REAL
);
CREATE INDEX IF NOT EXISTS idx_cardio_date ON cardio_activities(date);

-- Estado de cada ingesta: cursor para la siguiente pasada y último resultado.
CREATE TABLE IF NOT EXISTS sync_state (
    source TEXT PRIMARY KEY,      -- hevy, garmin
    cursor TEXT,
    last_run TEXT,
    last_ok TEXT,
    last_error TEXT,
    last_rows INTEGER
);
"""


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _migrate(conn: sqlite3.Connection) -> None:
    """Pone las tablas existentes al día con SCHEMA.

    CREATE TABLE IF NOT EXISTS no toca una tabla que ya existe, así que aquí se compara
    contra el esquema esperado. Tabla vacía y distinta → se recrea (cambia restricciones
    como el UNIQUE). Tabla con datos → solo se añaden las columnas que falten; nunca se
    borran datos.
    """
    ref = sqlite3.connect(":memory:")
    ref.executescript(SCHEMA)
    tablas = [r[0] for r in ref.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    for tabla in tablas:
        actuales = _columns(conn, tabla)
        if not actuales:
            continue  # no existe: la crea el executescript
        esperadas = _columns(ref, tabla)
        if actuales == esperadas:
            continue
        vacia = conn.execute(f"SELECT 1 FROM {tabla} LIMIT 1").fetchone() is None
        if vacia:
            log.warning("Recreando la tabla vacía %s con el esquema nuevo", tabla)
            conn.execute(f"DROP TABLE {tabla}")
            continue
        for fila in ref.execute(f"PRAGMA table_info({tabla})"):
            nombre, tipo = fila[1], fila[2]
            if nombre not in actuales:
                log.warning("Añadiendo la columna %s.%s", tabla, nombre)
                conn.execute(f"ALTER TABLE {tabla} ADD COLUMN {nombre} {tipo}")
    ref.close()


def init_schema() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        _migrate(conn)
        conn.executescript(SCHEMA)


@contextmanager
def connect(readonly: bool = True):
    """Conexión con filas tipo dict. readonly=True abre la BD en modo solo lectura a nivel de SQLite."""
    if readonly:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    else:
        # timeout: el MCP lee el mismo archivo; si una lectura larga coincide, se espera
        conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        if not readonly:
            conn.commit()
    finally:
        conn.close()


def fetch_all(sql: str, params: tuple = (), limit: int | None = None) -> list[dict]:
    with connect(readonly=True) as conn:
        rows = conn.execute(sql, params).fetchmany(limit) if limit else conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
