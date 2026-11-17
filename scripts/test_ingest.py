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
"""


def workout(wid, title, start, ejercicios):
    return {"id": wid, "title": title, "start_time": start, "end_time": start, "exercises": [
        {"index": i, "title": t, "exercise_template_id": f"T{i}", "sets": [
            {"index": j, "type": "normal", "weight_kg": kg, "reps": reps, "rpe": None,
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
            return [
                {"activityId": 111, "activityName": "Rodaje", "startTimeLocal": "2026-11-01 07:10:00",
                 "activityType": {"typeKey": "running"}, "duration": 2700.0, "distance": 8123.4,
                 "averageHR": 148.6, "maxHR": 171.0, "calories": 600.0, "elevationGain": 55.0},
                {"activityId": 222, "activityName": "Fuerza", "startTimeLocal": "2026-11-01 18:00:00",
                 "activityType": {"typeKey": "strength_training"}, "duration": 3600.0},
            ]

    garmin.client = lambda interactive=False: FakeGarmin()
    garmin.PAUSE_S = 0
    for _ in range(2):  # dos veces: el upsert no debe duplicar
        r = garmin.sync(since="2026-11-01")
    dias = db.fetch_all("SELECT * FROM garmin_daily WHERE date >= '2026-11-01' ORDER BY date")
    check(len(dias) == r["days"] and r["days"] >= 2, f"Garmin: una fila por día, sin duplicados ({r['days']})")
    d1 = dias[0]
    check((d1["sleep_score"], d1["sleep_hours"], d1["hrv_avg"], d1["resting_hr"], d1["steps"])
          == (81, 7.5, 62, 48, 9876), "Garmin: campos del día bien mapeados")
    check(d1["stress_avg"] is None, "estrés -1 (sin datos) se guarda como NULL")
    check(dias[1]["hrv_avg"] is None and dias[1]["sleep_score"] == 81,
          "si falla una métrica, el resto del día se guarda")
    act = db.fetch_all("SELECT * FROM cardio_activities")
    check(len(act) == 1 and act[0]["sport_type"] == "running", "la fuerza de Garmin no entra en cardio")
    check((act[0]["duration_min"], act[0]["distance_km"], act[0]["avg_hr"]) == (45.0, 8.123, 149),
          "actividad: minutos, km y pulso bien convertidos")

    estado = {r["source"]: r for r in db.fetch_all("SELECT * FROM sync_state")}
    check(estado["garmin"]["last_error"] is None and estado["garmin"]["cursor"], "sync_state apunta la pasada de Garmin")

    print("OK: la ingesta escribe lo que debe.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
