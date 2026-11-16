#!/usr/bin/env bash
#
# Despachador de despliegues del VPS. Va en /home/ubuntu/bin/deploy.sh y es el
# comando forzado de la clave de CI en ~/.ssh/authorized_keys:
#
#   from="100.64.0.0/10",command="/home/ubuntu/bin/deploy.sh",no-port-forwarding,\
#   no-X11-forwarding,no-agent-forwarding,no-pty ssh-ed25519 AAAA... github-ci
#
# GitHub Actions hace  ssh -p 2222 ubuntu@<host> "<nombre-del-repo>"  y lo único
# que llega aquí es ese nombre, en SSH_ORIGINAL_COMMAND. La clave no da shell.

set -euo pipefail
PROJECT="${SSH_ORIGINAL_COMMAND:-${1:-}}"

# Solo nombres simples: nada de /, .. ni espacios
if [[ ! "$PROJECT" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]*$ ]]; then
  echo "ERROR: nombre de proyecto inválido: '$PROJECT'"; exit 1
fi

DIR="/home/ubuntu/apps/projects/$PROJECT"
if [[ ! -d "$DIR/.git" ]]; then
  echo "ERROR: '$PROJECT' no está habilitado en el VPS (clónalo primero en $DIR)"; exit 1
fi

cd "$DIR"
exec 9>"/tmp/deploy-$PROJECT.lock"; flock 9   # evita dos despliegues a la vez

echo ">> Actualizando código..."
git fetch --prune origin
git reset --hard origin/main                  # nunca 'git clean': borraría secrets.env

# El repo manda si trae su propio script: guardias de secretos, comprobación de
# salud, lo que necesite. Como el código ya está actualizado, el que se ejecuta
# es el deploy.sh recién traído, no el de antes.
if [[ -x ./deploy.sh ]]; then
  echo ">> Ejecutando el deploy.sh del repo..."
  ./deploy.sh
elif [[ -f docker-compose.yml || -f compose.yaml ]]; then
  echo ">> Sin deploy.sh: levantando contenedores sin más..."
  docker compose up -d --build --remove-orphans
fi

docker image prune -f >/dev/null
MSG="$(date -Is) $PROJECT @ $(git rev-parse --short HEAD)"
echo "$MSG" >> ~/deploy.log
echo ">> OK: $MSG"
