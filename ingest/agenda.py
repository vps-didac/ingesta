"""Cuándo se sincroniza: franjas fijas del día y reintentos de la mañana.

Sin red ni BD: solo decide. El bucle de ingest/__main__.py le pregunta qué toca.

  - Franjas (INGEST_HORAS, por defecto 09:00, 12:00, 17:00 y 22:00): Hevy + Garmin.
    Si el servicio estaba parado a la hora justa, la franja se hace al arrancar,
    siempre que no hayan pasado más de TOLERANCIA.
  - Después de la primera franja, mientras el sueño de hoy no esté en la BD, se repasa
    solo Garmin de hoy:
        cada INGEST_REINTENTO_MIN (15) si el reloj aún no ha subido nada de hoy;
        cada INGEST_REINTENTO_USO_MIN (60) si ya hay uso de esa madrugada (pasos de hoy
        >= INGEST_PASOS_USO): el reloj sincroniza pero no trae la noche, lo más probable
        es que no durmieras con él, y no tiene sentido insistir cada cuarto de hora.
    Los reintentos paran al llegar el sueño o en la última franja del día.

Hevy entra solo en las franjas: un entreno de la tarde aparece en la nota a las 17 o a las 22.
"""
import os
from datetime import date, datetime, time, timedelta


def _horas(texto: str) -> list[time]:
    horas = sorted(time.fromisoformat(h.strip()) for h in texto.split(",") if h.strip())
    if not horas:
        raise ValueError("INGEST_HORAS no puede estar vacío")
    return horas


HORAS = _horas(os.environ.get("INGEST_HORAS", "09:00,12:00,17:00,22:00"))
REINTENTO = timedelta(minutes=int(os.environ.get("INGEST_REINTENTO_MIN", "15")))
REINTENTO_USO = timedelta(minutes=int(os.environ.get("INGEST_REINTENTO_USO_MIN", "60")))
PASOS_USO = int(os.environ.get("INGEST_PASOS_USO", "1500"))
TOLERANCIA = timedelta(hours=2)


class Agenda:
    def __init__(self, horas: list[time] | None = None):
        self.horas = horas or HORAS
        self.hechas: set[tuple[date, time]] = set()
        self.proximo_reintento: datetime | None = None

    def _en(self, dia: date, h: time, ahora: datetime) -> datetime:
        return datetime.combine(dia, h, tzinfo=ahora.tzinfo)

    def franja_pendiente(self, ahora: datetime) -> time | None:
        for h in self.horas:
            inicio = self._en(ahora.date(), h, ahora)
            if inicio <= ahora < inicio + TOLERANCIA and (ahora.date(), h) not in self.hechas:
                return h
        return None

    def marcar(self, ahora: datetime, h: time) -> None:
        self.hechas = {(d, x) for d, x in self.hechas if d >= ahora.date() - timedelta(days=1)}
        self.hechas.add((ahora.date(), h))
        # Una pasada completa ya repasa Garmin: el siguiente reintento cuenta desde aquí
        self.proximo_reintento = None

    def marcar_vigentes(self, ahora: datetime) -> None:
        """Al arrancar se hace una pasada completa: cuenta como la franja en curso."""
        if (h := self.franja_pendiente(ahora)) is not None:
            self.marcar(ahora, h)

    def toca_reintento(self, ahora: datetime, falta_sueno: bool) -> bool:
        primera = self._en(ahora.date(), self.horas[0], ahora)
        ultima = self._en(ahora.date(), self.horas[-1], ahora)
        if not falta_sueno or not (primera <= ahora < ultima):
            return False
        if (ahora.date(), self.horas[0]) not in self.hechas:
            return False
        return self.proximo_reintento is None or ahora >= self.proximo_reintento

    def intervalo_reintento(self, pasos_hoy: int | None) -> timedelta:
        return REINTENTO_USO if (pasos_hoy or 0) >= PASOS_USO else REINTENTO

    def programar_reintento(self, ahora: datetime, pasos_hoy: int | None) -> datetime:
        self.proximo_reintento = ahora + self.intervalo_reintento(pasos_hoy)
        return self.proximo_reintento
