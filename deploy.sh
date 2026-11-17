#!/usr/bin/env bash
#
# Despliegue de ESTE proyecto en el VPS.
#
# Lo ejecuta /home/ubuntu/bin/deploy.sh (el despachador) DESPUÉS de haber traído
# el código nuevo, así que aquí no se hace git: solo levantar y comprobar.
# También se puede ejecutar a mano desde el directorio del proyecto en el VPS.

set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

# La ingesta no expone HTTP: en vez de un healthcheck por URL, más abajo se
# comprueba que el contenedor sigue en pie unos segundos después de arrancar.
HEALTH_URL=""

if [ -f secrets.env.example ] && [ ! -f secrets.env ]; then
  echo "ERROR: falta secrets.env. Cópialo de secrets.env.example y rellénalo." >&2
  exit 1
fi

echo "==> docker compose up -d --build"
docker compose up -d --build --remove-orphans
docker compose ps

if [ -n "$HEALTH_URL" ]; then
  echo "==> Comprobando $HEALTH_URL"
  code=000
  for _ in $(seq 1 20); do
    code="$(curl -s -o /dev/null -m 5 -w '%{http_code}' "$HEALTH_URL" || true)"
    [ "$code" != "000" ] && break
    sleep 2
  done
fi

echo "==> Logs del arranque"
docker compose logs --tail 25

if [ -n "$HEALTH_URL" ] && [ "$code" = "000" ]; then
  echo "ERROR: nadie responde en $HEALTH_URL tras el despliegue." >&2
  exit 1
fi
[ -n "$HEALTH_URL" ] && echo "OK: el servicio responde (HTTP $code)."

# Si el proceso muere al arrancar (import roto, BD ilegible...), Docker lo
# reinicia en bucle y "up" habría dado verde igual. Un fallo de Hevy o Garmin
# NO tumba el contenedor: queda en sync_state y en los logs.
antes="$(docker inspect -f '{{.RestartCount}}' ingesta)"
sleep 15
estado="$(docker inspect -f '{{.State.Status}}' ingesta)"
despues="$(docker inspect -f '{{.RestartCount}}' ingesta)"
echo "==> Contenedor: $estado, reinicios $antes -> $despues"
if [ "$estado" != "running" ] || [ "$despues" != "$antes" ]; then
  echo "ERROR: el contenedor ingesta no sigue en pie tras arrancar." >&2
  docker compose logs --tail 50
  exit 1
fi
echo "OK: la ingesta está en marcha."
exit 0
