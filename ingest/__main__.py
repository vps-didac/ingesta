"""CLI de ingesta.

    python -m ingest hevy [--full]                 # incremental; --full rehace todo el historial
    python -m ingest garmin [--days N | --since F] [--solo-actividades]
                                                   # incremental; o repasa un rango
    python -m ingest link                          # recalcula el cruce Hevy-Garmin
    python -m ingest all                           # las dos, una vez
    python -m ingest loop                          # el servicio del compose: franjas de ingest/agenda.py
    python -m ingest garmin-login                  # login interactivo (MFA) y guarda tokens
    python -m ingest status                        # qué tal fue la última pasada de cada una

En el VPS se lanzan dentro del contenedor:
    docker compose run --rm ingesta python -m ingest garmin --days 365
"""
import argparse
import logging
import os
import sys
import time
from datetime import datetime

from ingest import db
from ingest.db import DB_PATH
from ingest.common import TZ, IngestError, log

ESPERA_S = 60   # cada cuánto mira el bucle si toca algo


def _run(nombre: str, fn) -> bool:
    try:
        fn()
        return True
    except IngestError as exc:
        log.error("%s: %s", nombre, exc)
    except Exception:
        log.exception("%s: error inesperado", nombre)
    return False


def _hevy(full=False):
    from ingest import hevy
    return hevy.sync(full=full)


def _garmin(days=None, since=None, solo_actividades=False):
    from ingest import garmin
    return garmin.sync(days=days, since=since, solo_actividades=solo_actividades)


def _sueno_y_pasos_hoy(ahora: datetime) -> tuple[bool, int | None]:
    """(falta el sueño de hoy, pasos de hoy)."""
    filas = db.fetch_all("SELECT sleep_score, sleep_hours, steps FROM garmin_daily WHERE date = ?",
                         (ahora.date().isoformat(),))
    if not filas:
        return True, None
    f = filas[0]
    return f["sleep_score"] is None and f["sleep_hours"] is None, f["steps"]


def _completa() -> None:
    _run("hevy", _hevy)
    _run("garmin", _garmin)


def cmd_loop() -> int:
    from ingest.agenda import Agenda

    agenda = Agenda()
    log.info("Ingesta: franjas %s; reintentos de sueño cada %s / %s si ya hay uso (BD %s)",
             ", ".join(h.strftime("%H:%M") for h in agenda.horas),
             agenda.intervalo_reintento(0), agenda.intervalo_reintento(10**9), DB_PATH)

    def _tras_completa() -> None:
        """Si tras una pasada completa sigue faltando el sueño, el primer reintento va
        a un intervalo de distancia (no un minuto después)."""
        ahora = datetime.now(TZ)
        falta, pasos = _sueno_y_pasos_hoy(ahora)
        if falta:
            agenda.programar_reintento(ahora, pasos)

    # Pasada al arrancar (o tras un despliegue): deja la BD al día y cuenta como la franja en curso
    _completa()
    agenda.marcar_vigentes(datetime.now(TZ))
    _tras_completa()

    while True:
        time.sleep(ESPERA_S)
        ahora = datetime.now(TZ)
        if (franja := agenda.franja_pendiente(ahora)) is not None:
            log.info("Franja de las %s", franja.strftime("%H:%M"))
            _completa()
            agenda.marcar(ahora, franja)
            _tras_completa()
            continue

        falta_sueno, pasos = _sueno_y_pasos_hoy(ahora)
        if agenda.toca_reintento(ahora, falta_sueno):
            _run("garmin", lambda: _garmin(days=1))
            falta_sueno, pasos = _sueno_y_pasos_hoy(datetime.now(TZ))
            if falta_sueno:
                siguiente = agenda.programar_reintento(datetime.now(TZ), pasos)
                log.info("Sin sueño de hoy todavía (pasos: %s). Siguiente intento a las %s",
                         pasos, siguiente.strftime("%H:%M"))
            else:
                log.info("Sueño de hoy recibido")


def cmd_status() -> int:
    for fila in db.fetch_all("SELECT * FROM sync_state ORDER BY source"):
        print(fila)
    return 0


def cmd_garmin_login() -> int:
    from ingest import garmin
    g = garmin.client(interactive=True)
    print(f"OK: sesión de Garmin iniciada como {g.get_full_name()}. Tokens en {garmin.TOKENSTORE}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m ingest")
    sub = p.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("hevy")
    h.add_argument("--full", action="store_true", help="ignorar el cursor y traer todo el historial")
    g = sub.add_parser("garmin")
    rango = g.add_mutually_exclusive_group()
    rango.add_argument("--days", type=int, help="repasar los últimos N días")
    rango.add_argument("--since", help="repasar desde YYYY-MM-DD")
    g.add_argument("--solo-actividades", action="store_true",
                   help="solo actividades (una consulta), sin el resumen diario")
    sub.add_parser("link")
    sub.add_parser("all")
    sub.add_parser("loop")
    sub.add_parser("garmin-login")
    sub.add_parser("status")
    args = p.parse_args(argv)

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    db.init_schema()

    if args.cmd == "hevy":
        return 0 if _run("hevy", lambda: _hevy(full=args.full)) else 1
    if args.cmd == "garmin":
        return 0 if _run("garmin", lambda: _garmin(days=args.days, since=args.since,
                                                    solo_actividades=args.solo_actividades)) else 1
    if args.cmd == "link":
        from ingest import link
        return 0 if _run("link", link.link) else 1
    if args.cmd == "all":
        ok = [_run("hevy", _hevy), _run("garmin", _garmin)]
        return 0 if all(ok) else 1
    if args.cmd == "loop":
        return cmd_loop()
    if args.cmd == "status":
        return cmd_status()
    if args.cmd == "garmin-login":
        try:
            return cmd_garmin_login()
        except IngestError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
