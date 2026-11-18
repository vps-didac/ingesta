"""Prueba sin red de la ingesta: que los datos de Hevy y Garmin acaban bien en la BD.

No llama a ninguna API: alimenta las funciones de ingest/ con respuestas con la forma
que documentan Hevy (OpenAPI de api.hevyapp.com) y garminconnect, sobre una BD temporal
creada con el esquema VIEJO, para comprobar también la migración.

Uso:  python scripts/test_ingest.py
"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

tmp = Path(tempfile.mkdtemp(prefix="ingest-test-"))
os.environ["DB_PATH"] = str(tmp / "personal_data.db")
os.environ["TZ"] = "Europe/Madrid"

ESQUEMA_VIEJO = """
CREATE TABLE hevy_sets (id INTEGER PRIMARY KEY AUTOINCREMENT, workout_id TEXT, date TEXT,
    workout_title TEXT, exercise_title TEXT, set_index INTEGER, weight_kg REAL, reps INTEGER,
    rpe REAL, UNIQUE(workout_id, exercise_title, set_index));
CREATE TABLE garmin_daily (date TEXT PRIMARY KEY, sleep_score INTEGER, sleep_hours REAL,
    hrv_avg REAL, resting_hr INTEGER, body_battery_max INTEGER, steps INTEGER, stress_avg INTEGER);
INSERT INTO garmin_daily (date, steps) VALUES ('2020-01-01', 1234);
INSERT INTO garmin_daily (date) VALUES ('2020-01-02');
CREATE TABLE cardio_activities (activity_id TEXT PRIMARY KEY, date TEXT, sport_type TEXT);
INSERT INTO cardio_activities VALUES ('999', '2026-10-01', 'running');
"""


def workout(wid, title, start, ejercicios, end=None, warmup=0):
    """Entreno con la forma de la API de Hevy. Las `warmup` primeras series de cada
    ejercicio son de calentamiento."""
    return {"id": wid, "title": title, "start_time": start, "end_time": end or start, "exercises": [
        {"index": i, "title": t, "exercise_template_id": f"T{i}", "sets": [
            {"index": j, "type": "warmup" if j < warmup else "normal", "weight_kg": kg, "reps": reps, "rpe": None,
             "distance_meters": None, "duration_seconds": None}
            for j, (kg, reps) in enumerate(series)
        ]} for i, (t, series) in enumerate(ejercicios)
    ]}


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok  {msg}")


def main() -> int:
    with sqlite3.connect(os.environ["DB_PATH"]) as c:
        c.executescript(ESQUEMA_VIEJO)

    from ingest import db
    from ingest import garmin, hevy

    db.init_schema()
    cols = [r[1] for r in sqlite3.connect(os.environ["DB_PATH"]).execute("PRAGMA table_info(hevy_sets)")]
    check("exercise_index" in cols, "hevy_sets vacía se recrea con el esquema nuevo")
    viejo = db.fetch_all("SELECT steps, body_battery_min FROM garmin_daily WHERE date = '2020-01-01'")
    check(viejo == [{"steps": 1234, "body_battery_min": None}], "garmin_daily con datos conserva filas y gana columnas")
    tablas = {r["name"] for r in db.fetch_all("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}
    check("cardio_activities" not in tablas and {"garmin_activities", "hevy_workouts", "sesiones_fuerza"} <= tablas,
          "cardio_activities desaparece; llegan garmin_activities, hevy_workouts y la vista")

    # --- Hevy: el mismo ejercicio dos veces en un entreno no debe chocar en el UNIQUE
    w1 = workout("w1", "Pierna", "2026-11-15T23:30:00Z", [
        ("Squat (Barbell)", [(100, 5), (100, 5)]),
        ("Leg Press", [(200, 10)]),
        ("Squat (Barbell)", [(80, 8)]),
    ])
    w2 = workout("w2", "Empuje", "2026-11-16T08:00:00Z", [("Bench Press (Barbell)", [(80, 5)])])
    # La API los da del más reciente al más antiguo; fetch_events les da la vuelta
    r = hevy.apply_events([{"type": "updated", "workout": w1}, {"type": "updated", "workout": w2}])
    check(r == {"workouts": 2, "deleted": 0, "sets": 5}, f"Hevy inserta 5 series ({r})")
    fecha = db.fetch_all("SELECT DISTINCT date FROM hevy_sets WHERE workout_id = 'w1'")
    check(fecha == [{"date": "2026-11-16"}], "23:30 UTC del 15 cuenta como día 16 en Madrid")

    # Edición en la app: se quita una serie → debe desaparecer también aquí
    w1["exercises"][0]["sets"].pop()
    hevy.apply_events([{"type": "updated", "workout": w1}])
    n = db.fetch_all("SELECT COUNT(*) n FROM hevy_sets WHERE workout_id = 'w1'")[0]["n"]
    check(n == 3, "editar un entreno lo reescribe entero (3 series)")

    hevy.apply_events([{"type": "deleted", "id": "w2", "deleted_at": "2026-11-16T10:00:00Z"}])
    n = db.fetch_all("SELECT COUNT(*) n FROM hevy_sets WHERE workout_id = 'w2'")[0]["n"]
    check(n == 0, "un entreno borrado en Hevy se borra de la BD")
    ids = [r["workout_id"] for r in db.fetch_all("SELECT workout_id FROM hevy_workouts")]
    check(ids == ["w1"], "hevy_workouts sigue las altas y los borrados")

    # Entreno de gimnasio que también se grabó en Garmin (ver FakeGarmin más abajo).
    # Hevy da "+00:00"; se guarda normalizado a Z para compararlo con Garmin.
    w3 = workout("w3", "Push", "2026-11-01T17:02:00+00:00", [
        ("Bench Press (Barbell)", [(40, 10), (80, 5), (80, 5)]),
    ], end="2026-11-01T18:05:00+00:00", warmup=1)
    hevy.apply_events([{"type": "updated", "workout": w3}])
    fila = db.fetch_all("SELECT start_time, end_time, duration_min FROM hevy_workouts WHERE workout_id = 'w3'")[0]
    check(fila == {"start_time": "2026-11-01T17:02:00Z", "end_time": "2026-11-01T18:05:00Z", "duration_min": 63.0},
          "hevy_workouts: inicio y fin en UTC con Z, y duración")

    with db.connect(readonly=False) as conn:
        conn.execute("DELETE FROM hevy_workouts")
    check(hevy._faltan_workouts(), "series sin hevy_workouts → la próxima pasada rehace el historial")
    hevy.apply_events([{"type": "updated", "workout": w1}, {"type": "updated", "workout": w3}])

    # --- Garmin: cliente falso con la forma de las respuestas de garminconnect
    class FakeGarmin:
        def get_user_summary(self, d):
            return {"restingHeartRate": 48, "totalSteps": 9876, "averageStressLevel": -1,
                    "bodyBatteryHighestValue": 88, "bodyBatteryLowestValue": 12}

        def get_sleep_data(self, d):
            return {"dailySleepDTO": {"sleepTimeSeconds": 27000, "sleepScores": {"overall": {"value": 81}}}}

        def get_hrv_data(self, d):
            if d.endswith("-02"):
                raise RuntimeError("sin datos de HRV")
            return {"hrvSummary": {"lastNightAvg": 62}}

        def get_activities_by_date(self, a, b):
            self.desde_actividades = a
            return [
                {"activityId": 111, "activityName": "Rodaje", "startTimeLocal": "2026-11-01 07:10:00",
                 "startTimeGMT": "2026-11-01 06:10:00",
                 "activityType": {"typeKey": "running"}, "duration": 2700.0, "elapsedDuration": 2700.0,
                 "distance": 8123.4, "averageHR": 148.6, "maxHR": 171.0, "calories": 600.0, "elevationGain": 55.0},
                # Calentamiento en cinta que acaba justo cuando empieza la fuerza: está dentro
                # del margen, pero se solapa menos que la actividad de fuerza.
                {"activityId": 333, "activityName": "Cinta", "startTimeLocal": "2026-11-01 17:45:00",
                 "startTimeGMT": "2026-11-01 16:45:00",
                 "activityType": {"typeKey": "treadmill_running"}, "duration": 900.0, "elapsedDuration": 900.0},
                # El reloj se arrancó 2 min antes que Hevy y se paró 5 min después.
                {"activityId": 222, "activityName": "Fuerza", "startTimeLocal": "2026-11-01 18:00:00",
                 "startTimeGMT": "2026-11-01 17:00:00",
                 "activityType": {"typeKey": "strength_training"}, "duration": 3500.0, "elapsedDuration": 4200.0,
                 "averageHR": 112.4, "maxHR": 158.0, "calories": 450.0},
            ]

    fake = FakeGarmin()
    garmin.client = lambda interactive=False: fake
    garmin.PAUSE_S = 0
    for vuelta in range(2):  # dos veces: el upsert no debe duplicar
        r = garmin.sync(since="2026-11-01")
        if vuelta == 0:
            check(fake.desde_actividades == "2020-01-01",
                  "con garmin_activities vacía, las actividades se bajan desde el primer día de garmin_daily")
    check(not db.fetch_all("SELECT 1 FROM garmin_daily WHERE date = '2020-01-02'"),
          "los días sin ningún dato (antes de tener reloj) se limpian")
    dias = db.fetch_all("SELECT * FROM garmin_daily WHERE date >= '2026-11-01' ORDER BY date")
    check(len(dias) == r["days"] and r["days"] >= 2, f"Garmin: una fila por día, sin duplicados ({r['days']})")
    d1 = dias[0]
    check((d1["sleep_score"], d1["sleep_hours"], d1["hrv_avg"], d1["resting_hr"], d1["steps"])
          == (81, 7.5, 62, 48, 9876), "Garmin: campos del día bien mapeados")
    check(d1["stress_avg"] is None, "estrés -1 (sin datos) se guarda como NULL")
    check(dias[1]["hrv_avg"] is None and dias[1]["sleep_score"] == 81,
          "si falla una métrica, el resto del día se guarda")
    act = {a["activity_id"]: a for a in db.fetch_all("SELECT * FROM garmin_activities")}
    check(set(act) == {"111", "222", "333"}, "se guardan todas las actividades, también la fuerza")
    run = act["111"]
    check((run["duration_min"], run["distance_km"], run["avg_hr"]) == (45.0, 8.123, 149),
          "actividad: minutos, km y pulso bien convertidos")
    check((act["222"]["start_time_utc"], act["222"]["end_time_utc"]) == ("2026-11-01T17:00:00Z", "2026-11-01T18:10:00Z"),
          "actividad: inicio UTC y fin con la duración total (pausas incluidas)")

    # --- Cruce Hevy-Garmin
    pareja = db.fetch_all("SELECT workout_id, garmin_activity_id FROM hevy_workouts ORDER BY workout_id")
    check(pareja == [{"workout_id": "w1", "garmin_activity_id": None},
                     {"workout_id": "w3", "garmin_activity_id": "222"}],
          "el entreno de Hevy se empareja con la fuerza de Garmin, no con la cinta; sin solape, NULL")
    sesion = db.fetch_all("SELECT * FROM sesiones_fuerza WHERE workout_id = 'w3'")[0]
    check((sesion["series"], sesion["volumen_kg"], sesion["avg_hr"], sesion["calories"]) == (2, 800.0, 112, 450.0),
          "sesiones_fuerza: series y volumen de Hevy (sin calentamiento) con pulso y calorías de Garmin")

    # Si se borra el entreno en Hevy, la actividad de Garmin queda libre
    hevy.apply_events([{"type": "deleted", "id": "w3"}])
    from ingest import link
    link.link()
    check(db.fetch_all("SELECT COUNT(*) n FROM sesiones_fuerza")[0]["n"] == 1, "borrar en Hevy quita la sesión")

    # Caso real (31-08-2026): dos entrenos seguidos en Hevy y una sola actividad en
    # Garmin. Se la queda el que más se solapa; si no, las kcal contarían doble.
    from datetime import datetime as _dt
    t = lambda h: _dt.fromisoformat(f"2026-08-31T{h}:00+00:00")
    pares = link.emparejar(
        [("iso", t("13:21"), t("13:33")), ("pull", t("13:33"), t("15:21"))],
        [("g", t("13:34"), t("15:24"))],
    )
    check(pares == {"iso": None, "pull": "g"}, "una actividad de Garmin va a un solo entreno de Hevy")

    r = garmin.sync(since="2026-11-10", solo_actividades=True)
    check(r["days"] == 0 and r["activities"] == 3, "--solo-actividades no pide el resumen diario")

    estado = {r["source"]: r for r in db.fetch_all("SELECT * FROM sync_state")}
    check(estado["garmin"]["last_error"] is None and estado["garmin"]["cursor"], "sync_state apunta la pasada de Garmin")

    # --- Agenda: franjas y reintentos de la mañana
    from datetime import datetime as _dt2, time as _t, timedelta as _td
    from ingest import __main__ as cli
    from ingest.agenda import Agenda
    from ingest.common import TZ
    a = Agenda([_t(9), _t(12), _t(17), _t(22)])
    en = lambda h, m=0: _dt2(2026, 11, 18, h, m, tzinfo=TZ)  # noqa: E731
    check(a.franja_pendiente(en(8, 59)) is None and a.franja_pendiente(en(9, 0)) == _t(9),
          "la primera franja es a las 9:00")
    check(not a.toca_reintento(en(8, 30), True), "antes de la franja de las 9 no se reintenta")
    a.marcar(en(9, 0), _t(9))
    check(a.franja_pendiente(en(9, 1)) is None, "una franja hecha no se repite")
    check(a.toca_reintento(en(9, 1), True) and not a.toca_reintento(en(9, 1), False),
          "tras las 9, se reintenta solo si falta el sueño")
    check(a.programar_reintento(en(9, 1), 300) == en(9, 16), "sin uso de madrugada: cada 15 min")
    check(not a.toca_reintento(en(9, 10), True) and a.toca_reintento(en(9, 16), True), "respeta el intervalo")
    check(a.programar_reintento(en(9, 16), 4200) == en(10, 16), "con pasos de madrugada: cada hora")
    check(not a.toca_reintento(en(22, 0), True), "en la última franja dejan de reintentarse")
    check(a.franja_pendiente(en(13, 30)) == _t(12) and a.franja_pendiente(en(14, 30)) is None,
          "una franja se recupera hasta 2 h tarde, no más")
    b = Agenda([_t(9), _t(12)])
    b.marcar_vigentes(en(10, 0))
    check(b.franja_pendiente(en(10, 1)) is None, "la pasada al arrancar cuenta como la franja en curso")

    ahora = _dt2.now(TZ)
    falta, _ = cli._sueno_y_pasos_hoy(ahora)
    check(not falta, "con el sueño de hoy en la BD no se reintenta")
    with db.connect(readonly=False) as conn:
        conn.execute("UPDATE garmin_daily SET sleep_score = NULL, sleep_hours = NULL WHERE date = ?",
                     (ahora.date().isoformat(),))
    check(cli._sueno_y_pasos_hoy(ahora) == (True, 9876), "sin sueño de hoy: falta, con sus pasos")
    check(cli._sueno_y_pasos_hoy(ahora + _td(days=30)) == (True, None), "un día sin fila cuenta como sin sueño")

    print("OK: la ingesta escribe lo que debe.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
