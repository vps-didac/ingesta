"""CLI de ingesta.

    python -m ingest hevy [--full]                 # incremental; --full rehace todo el historial
    python -m ingest garmin [--days N | --since F] # incremental; o repasa un rango
    python -m ingest all                           # las dos, una vez
    python -m ingest loop                          # las dos cada INGEST_INTERVAL_MIN (el servicio del compose)
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

from ingest import db
from ingest.db import DB_PATH
from ingest.common import IngestError, log

INTERVAL_MIN = int(os.environ.get("INGEST_INTERVAL_MIN", "360"))


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


def _garmin(days=None, since=None):
    from ingest import garmin
    return garmin.sync(days=days, since=since)


def cmd_loop() -> int:
    log.info("Ingesta en bucle cada %s min (BD %s)", INTERVAL_MIN, DB_PATH)
    while True:
        _run("hevy", _hevy)
        _run("garmin", _garmin)
        time.sleep(INTERVAL_MIN * 60)


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
        return 0 if _run("garmin", lambda: _garmin(days=args.days, since=args.since)) else 1
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
