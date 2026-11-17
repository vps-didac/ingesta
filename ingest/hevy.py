"""Ingesta de Hevy → hevy_sets, con la API pública oficial (requiere Hevy Pro).

La clave se crea en https://hevy.com/settings?developer y va en HEVY_API_KEY.

Sincronización incremental con /v1/workouts/events: devuelve los entrenos creados o
editados ("updated", con el entreno completo) y los borrados ("deleted") desde una fecha.
Cada entreno editado se reescribe entero (se borran sus series y se insertan de nuevo),
así una serie quitada en la app también desaparece de la BD. La primera vez el cursor
es 1970 y llega todo el historial.
"""
import os
import time
from datetime import datetime, timedelta

import requests

from ingest import db
from ingest.common import TZ, IngestError, get_cursor, iso, log, now_utc, record_run

SOURCE = "hevy"
BASE_URL = os.environ.get("HEVY_BASE_URL", "https://api.hevyapp.com")
PAGE_SIZE = 10  # máximo que acepta la API
# Margen al reanudar: un entreno guardado justo mientras corría la pasada anterior
# podría quedar fuera. Reprocesar unos minutos de más es inocuo (es idempotente).
OVERLAP = timedelta(minutes=10)


def _api_key() -> str:
    key = os.environ.get("HEVY_API_KEY", "").strip()
    if not key:
        raise IngestError("HEVY_API_KEY no configurada (secrets.env)")
    return key


def _get(session: requests.Session, path: str, params: dict) -> dict:
    for intento in range(5):
        resp = session.get(f"{BASE_URL}{path}", params=params, timeout=30)
        if resp.status_code == 429 or resp.status_code >= 500:
            espera = int(resp.headers.get("Retry-After", 0)) or 2 ** (intento + 1)
            log.warning("Hevy %s → HTTP %s, reintento en %ss", path, resp.status_code, espera)
            time.sleep(espera)
            continue
        if resp.status_code in (401, 403):
            raise IngestError(f"Hevy rechaza la API key (HTTP {resp.status_code}). ¿Sigue activo Hevy Pro?")
        if resp.status_code == 404:
            # La API contesta 404 cuando la página pedida no existe (p. ej. sin eventos)
            log.info("Hevy %s %s → 404, se toma como página vacía", path, params)
            return {}
        resp.raise_for_status()
        return resp.json()
    raise IngestError(f"Hevy {path}: demasiados reintentos")


def fetch_events(session: requests.Session, since: str) -> list[dict]:
    """Todos los eventos desde `since`, del más antiguo al más reciente."""
    eventos, page = [], 1
    while True:
        data = _get(session, "/v1/workouts/events", {"page": page, "pageSize": PAGE_SIZE, "since": since})
        eventos.extend(data.get("events") or [])
        if page >= (data.get("page_count") or 0):
            break
        page += 1
    # La API los da del más reciente al más antiguo; al aplicar, el último gana.
    eventos.reverse()
    return eventos


def _local_date(start_time: str | None) -> str | None:
    if not start_time:
        return None
    dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
    return dt.astimezone(TZ).date().isoformat()


def workout_rows(w: dict) -> list[tuple]:
    """Aplana un entreno de Hevy en filas de hevy_sets."""
    filas = []
    fecha = _local_date(w.get("start_time"))
    for ex in w.get("exercises") or []:
        for s in ex.get("sets") or []:
            reps = s.get("reps")
            filas.append((
                w["id"], fecha, w.get("start_time"), w.get("title"),
                int(ex.get("index", 0)), ex.get("title"), ex.get("exercise_template_id"),
                int(s.get("index", 0)), s.get("type"),
                s.get("weight_kg"), int(reps) if reps is not None else None, s.get("rpe"),
                s.get("distance_meters"), s.get("duration_seconds"),
            ))
    return filas


INSERT_SET = """
INSERT INTO hevy_sets (workout_id, date, start_time, workout_title, exercise_index, exercise_title,
                       exercise_template_id, set_index, set_type, weight_kg, reps, rpe, distance_m, duration_s)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def apply_events(eventos: list[dict]) -> dict:
    """Aplica los eventos en una sola transacción. Devuelve un resumen."""
    resumen = {"workouts": 0, "deleted": 0, "sets": 0}
    with db.connect(readonly=False) as conn:
        for ev in eventos:
            if ev.get("type") == "deleted":
                conn.execute("DELETE FROM hevy_sets WHERE workout_id = ?", (ev["id"],))
                resumen["deleted"] += 1
            elif ev.get("type") == "updated" and ev.get("workout"):
                w = ev["workout"]
                conn.execute("DELETE FROM hevy_sets WHERE workout_id = ?", (w["id"],))
                filas = workout_rows(w)
                conn.executemany(INSERT_SET, filas)
                resumen["workouts"] += 1
                resumen["sets"] += len(filas)
    return resumen


def sync(full: bool = False) -> dict:
    """Trae de Hevy lo nuevo desde la última pasada (o todo con full=True)."""
    inicio = now_utc()
    cursor = None if full else get_cursor(SOURCE)
    since = cursor or "1970-01-01T00:00:00Z"
    try:
        with requests.Session() as s:
            s.headers.update({"api-key": _api_key(), "accept": "application/json"})
            eventos = fetch_events(s, since)
        resumen = apply_events(eventos)
    except Exception as exc:
        record_run(SOURCE, error=f"{type(exc).__name__}: {exc}")
        raise
    record_run(SOURCE, cursor=iso(inicio - OVERLAP), rows=resumen["sets"])
    log.info("Hevy desde %s: %s", since, resumen)
    return resumen
