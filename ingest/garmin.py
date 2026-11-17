"""Ingesta de Garmin Connect → garmin_daily y cardio_activities.

Garmin no tiene API pública para particulares: se usa la librería `garminconnect`
(0.3.x, que se autentica como la app de Android). Puede romperse si Garmin cambia el
login; si pasa, el error queda en sync_state y el resto del sistema sigue.

Autenticación:
  - La primera vez, a mano y con terminal (por si hay MFA):
        docker compose run --rm ingesta python -m ingest garmin-login
    Guarda los tokens en GARMIN_TOKENSTORE (por defecto /data/garmin_tokens, 0600).
  - Después, cada pasada carga esos tokens y la librería los refresca y los vuelve a
    guardar sola. Solo si el refresh token caduca vuelve a usar email y contraseña; si
    entonces Garmin pide MFA, la pasada falla con un mensaje y hay que repetir el login.

Cada pasada repasa los últimos días (el sueño y el HRV de hoy se completan horas
después) y las actividades del mismo rango. Todo es upsert: repetir no duplica nada.
"""
import os
import time
from datetime import date, timedelta

from ingest import db
from ingest.common import IngestError, get_cursor, iso, log, now_utc, record_run

SOURCE = "garmin"
TOKENSTORE = os.environ.get("GARMIN_TOKENSTORE", "/data/garmin_tokens")
FIRST_RUN_DAYS = int(os.environ.get("GARMIN_FIRST_RUN_DAYS", "30"))
OVERLAP_DAYS = 3          # días hacia atrás que se repasan aunque ya estén
PAUSE_S = 0.5             # entre días, para no parecer un scraper con prisa
# Lo que ya cubre Hevy no se duplica en cardio_activities.
SKIP_SPORTS = {"strength_training"}


def client(interactive: bool = False):
    """Cliente autenticado. interactive=True permite pedir el código MFA por teclado."""
    try:
        from garminconnect import Garmin
    except ImportError as exc:  # pragma: no cover - solo si falta la dependencia
        raise IngestError(f"garminconnect no está instalado: {exc}") from exc

    email = os.environ.get("GARMIN_EMAIL") or None
    password = os.environ.get("GARMIN_PASSWORD") or None
    prompt = (lambda: input("Código MFA de Garmin: ").strip()) if interactive else None
    g = Garmin(email, password, prompt_mfa=prompt)
    try:
        g.login(TOKENSTORE)
    except Exception as exc:
        msg = str(exc)
        if "MFA" in msg and not interactive:
            raise IngestError(
                "Garmin pide MFA: ejecuta 'docker compose run --rm ingesta python -m ingest garmin-login'"
            ) from exc
        raise IngestError(f"Login de Garmin fallido: {type(exc).__name__}: {msg}") from exc
    return g


def _get(d: dict | None, *path):
    for k in path:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def _int(v):
    return int(round(v)) if isinstance(v, (int, float)) else None


def daily_row(dia: str, summary: dict | None, sleep: dict | None, hrv: dict | None) -> tuple:
    segundos = _get(sleep, "dailySleepDTO", "sleepTimeSeconds")
    return (
        dia,
        _int(_get(sleep, "dailySleepDTO", "sleepScores", "overall", "value")),
        round(segundos / 3600, 2) if segundos else None,
        _get(hrv, "hrvSummary", "lastNightAvg"),
        _int(_get(summary, "restingHeartRate")),
        _int(_get(summary, "bodyBatteryHighestValue")),
        _int(_get(summary, "bodyBatteryLowestValue")),
        _int(_get(summary, "totalSteps")),
        # -1/-2 significan "sin datos" en Garmin
        v if (v := _int(_get(summary, "averageStressLevel"))) is not None and v >= 0 else None,
        iso(now_utc()),
    )


def activity_row(a: dict) -> tuple | None:
    sport = _get(a, "activityType", "typeKey")
    if sport in SKIP_SPORTS or a.get("activityId") is None:
        return None
    inicio = a.get("startTimeLocal")  # "YYYY-MM-DD HH:MM:SS"
    dur = a.get("duration")
    dist = a.get("distance")
    return (
        str(a["activityId"]),
        inicio[:10] if inicio else None,
        inicio,
        a.get("activityName"),
        sport,
        round(dur / 60, 2) if dur else None,
        round(dist / 1000, 3) if dist else None,
        _int(a.get("averageHR")),
        _int(a.get("maxHR")),
        a.get("calories"),
        a.get("elevationGain"),
        a.get("trainingStressScore"),
    )


UPSERT_DAILY = """
INSERT INTO garmin_daily (date, sleep_score, sleep_hours, hrv_avg, resting_hr, body_battery_max,
                          body_battery_min, steps, stress_avg, synced_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(date) DO UPDATE SET
    sleep_score = excluded.sleep_score, sleep_hours = excluded.sleep_hours, hrv_avg = excluded.hrv_avg,
    resting_hr = excluded.resting_hr, body_battery_max = excluded.body_battery_max,
    body_battery_min = excluded.body_battery_min, steps = excluded.steps,
    stress_avg = excluded.stress_avg, synced_at = excluded.synced_at
"""

UPSERT_ACTIVITY = """
INSERT INTO cardio_activities (activity_id, date, start_time, name, sport_type, duration_min, distance_km,
                               avg_hr, max_hr, calories, elevation_gain_m, tss)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(activity_id) DO UPDATE SET
    date = excluded.date, start_time = excluded.start_time, name = excluded.name,
    sport_type = excluded.sport_type, duration_min = excluded.duration_min,
    distance_km = excluded.distance_km, avg_hr = excluded.avg_hr, max_hr = excluded.max_hr,
    calories = excluded.calories, elevation_gain_m = excluded.elevation_gain_m, tss = excluded.tss
"""


def _safe(fn, *args):
    """Una métrica que falla (p. ej. sin reloj puesto esa noche) no tumba el día entero."""
    try:
        return fn(*args)
    except Exception as exc:
        name = getattr(fn, "__name__", "?")
        if "TooManyRequests" in type(exc).__name__:
            raise
        log.warning("Garmin %s%s: %s", name, args, exc)
        return None


def _rango(days: int | None, since: str | None) -> tuple[date, date]:
    hoy = date.today()
    if since:
        return date.fromisoformat(since), hoy
    if days:
        return hoy - timedelta(days=days - 1), hoy
    cursor = get_cursor(SOURCE)
    if cursor:
        return min(date.fromisoformat(cursor), hoy) - timedelta(days=OVERLAP_DAYS), hoy
    return hoy - timedelta(days=FIRST_RUN_DAYS - 1), hoy


def sync(days: int | None = None, since: str | None = None) -> dict:
    """Sincroniza garmin_daily y cardio_activities.

    Sin argumentos: desde la última pasada (menos OVERLAP_DAYS), o FIRST_RUN_DAYS la
    primera vez. days=N repasa los últimos N días; since=YYYY-MM-DD, desde esa fecha.
    """
    desde, hasta = _rango(days, since)
    resumen = {"days": 0, "activities": 0, "desde": desde.isoformat(), "hasta": hasta.isoformat()}
    try:
        g = client()
        dia = desde
        while dia <= hasta:
            d = dia.isoformat()
            fila = daily_row(
                d,
                _safe(g.get_user_summary, d),
                _safe(g.get_sleep_data, d),
                _safe(g.get_hrv_data, d),
            )
            with db.connect(readonly=False) as conn:
                conn.execute(UPSERT_DAILY, fila)
            resumen["days"] += 1
            dia += timedelta(days=1)
            time.sleep(PAUSE_S)

        actividades = g.get_activities_by_date(desde.isoformat(), hasta.isoformat()) or []
        filas = [f for a in actividades if (f := activity_row(a))]
        with db.connect(readonly=False) as conn:
            conn.executemany(UPSERT_ACTIVITY, filas)
        resumen["activities"] = len(filas)
    except Exception as exc:
        record_run(SOURCE, error=f"{type(exc).__name__}: {exc}")
        raise
    record_run(SOURCE, cursor=hasta.isoformat(), rows=resumen["days"] + resumen["activities"])
    log.info("Garmin: %s", resumen)
    return resumen
