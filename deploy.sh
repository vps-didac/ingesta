#!/usr/bin/env bash
#
# Despliegue de ESTE proyecto en el VPS.
#
# Lo ejecuta /home/ubuntu/bin/deploy.sh (el despachador) DESPUÉS de haber traído
# el código nuevo, así que aquí no se hace git: solo levantar y comprobar.
# También se puede ejecutar a mano desde el directorio del proyecto en el VPS.

set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

# URL que debe responder para dar el despliegue por bueno. Déjala vacía si este
# proyecto no expone HTTP. Cualquier respuesta vale: un 401 o un 406 significan
# que el servicio está vivo, y es 000 lo que indica que no hay nadie escuchando.
HEALTH_URL="http://127.0.0.1:8000/healthz"

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
exit 0
