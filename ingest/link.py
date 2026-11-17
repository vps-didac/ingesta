"""Empareja cada entreno de Hevy con su actividad de Garmin por fecha y hora.

En Garmin se graban los entrenos de gimnasio solo por el pulso y las calorías; las
series están en Hevy. No hay ningún identificador común, así que se cruzan por tiempo:
para cada entreno de Hevy se elige la actividad de Garmin que más se solapa con él,
con un margen de MARGIN_MIN minutos por si un reloj se arrancó antes o se paró
después que el otro. Si ninguna se acerca, el entreno queda sin pareja (NULL).

Se recalcula entero en cada pasada (son unos cientos de filas): así un entreno
editado, una actividad nueva o un cambio en el criterio se aplican solos.
"""
from datetime import datetime, timedelta

from ingest import db
from ingest.common import log

MARGIN = timedelta(minutes=10)


def _dt(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def emparejar(workouts: list[tuple], actividades: list[tuple]) -> dict[str, str | None]:
    """workouts y actividades: (id, inicio, fin) con datetimes. Devuelve workout → actividad.

    Candidatas: las que se solapan con el entreno ampliado MARGIN por cada lado. Gana
    la de mayor solapamiento real (que puede ser negativo, si solo entra por el margen).

    Una actividad va a un solo entreno: si dos entrenos seguidos de Hevy caen en la
    misma actividad de Garmin (p. ej. un bloque corto de isométricos y luego el
    entreno principal), se la queda el que más se solapa y el otro queda sin pareja.
    Si no, sus calorías se contarían dos veces.
    """
    pares, solapes = {}, {}
    for wid, w_ini, w_fin in workouts:
        mejor, mejor_solape = None, None
        for aid, a_ini, a_fin in actividades:
            if a_ini >= w_fin + MARGIN or a_fin <= w_ini - MARGIN:
                continue
            solape = min(a_fin, w_fin) - max(a_ini, w_ini)
            if mejor_solape is None or solape > mejor_solape:
                mejor, mejor_solape = aid, solape
        pares[wid] = mejor
        solapes[wid] = mejor_solape

    duenyo = {}  # actividad → entreno que más se solapa con ella
    for wid, aid in pares.items():
        if aid and (aid not in duenyo or solapes[wid] > solapes[duenyo[aid]]):
            duenyo[aid] = wid
    return {wid: (aid if aid and duenyo[aid] == wid else None) for wid, aid in pares.items()}


def link() -> int:
    """Recalcula los emparejamientos. Devuelve cuántos entrenos tienen pareja."""
    with db.connect(readonly=False) as conn:
        workouts = [
            (r["workout_id"], _dt(r["start_time"]), _dt(r["end_time"]))
            for r in conn.execute("SELECT workout_id, start_time, end_time FROM hevy_workouts"
                                  " WHERE start_time IS NOT NULL AND end_time IS NOT NULL")
        ]
        actividades = [
            (r["activity_id"], _dt(r["start_time_utc"]), _dt(r["end_time_utc"]))
            for r in conn.execute("SELECT activity_id, start_time_utc, end_time_utc FROM garmin_activities"
                                  " WHERE start_time_utc IS NOT NULL AND end_time_utc IS NOT NULL")
        ]
        pares = emparejar(workouts, actividades)
        conn.execute("UPDATE hevy_workouts SET garmin_activity_id = NULL")
        conn.executemany("UPDATE hevy_workouts SET garmin_activity_id = ? WHERE workout_id = ?",
                         [(aid, wid) for wid, aid in pares.items() if aid])
    n = sum(1 for a in pares.values() if a)
    log.info("Emparejados con Garmin: %s de %s entrenos de Hevy", n, len(pares))
    return n


def safe_link() -> None:
    """Para el final de cada sync: si el cruce falla, la ingesta ya está guardada igual."""
    try:
        link()
    except Exception:
        log.exception("Error emparejando Hevy con Garmin")
