# plantilla-vps

Plantilla para proyectos que viven en el VPS: cada push a `main` pasa los tests y,
si están en verde, se despliega solo. Pulsa **Use this template** y sigue los cinco
pasos de abajo.

Lo que trae:

```
.github/workflows/deploy.yml   # tests + Tailscale + SSH al VPS. No hay que tocarlo.
deploy.sh                      # cómo se levanta ESTE proyecto. Lo ejecuta el VPS.
Dockerfile                     # dependencias primero, para que el rebuild sea rápido
docker-compose.yml             # esqueleto del servicio
app.py                         # servicio de ejemplo con /healthz: bórralo
secrets.env.example            # plantilla del archivo de secretos del VPS
vps/dispatcher.sh              # el /home/ubuntu/bin/deploy.sh del VPS (ver abajo)
```

## Estrenar un proyecto

**1. Crea el repo** desde esta plantilla. Tiene que ser **público**: la organización
está en plan Free y los secretos de organización solo los leen repos públicos. No
escribas credenciales en el código; para eso está `secrets.env`.

**2. Ajusta tres cosas** y empuja a `main`:

- `docker-compose.yml`: el `container_name` (vale el nombre del repo) y lo que necesite
  el servicio.
- `deploy.sh`: la variable `HEALTH_URL`, o vacíala si el proyecto no expone HTTP.
- `.github/workflows/deploy.yml`: el paso del healthcheck y, si hay tests, el hueco
  que ya está comentado.

**3. Clónalo en el VPS**, en `apps/projects/` y con el nombre exacto del repositorio:
es así como lo encuentra el despachador.

```bash
cd /home/ubuntu/apps/projects
git clone https://github.com/vps-didac/<nombre-del-repo>.git
cd <nombre-del-repo>
cp secrets.env.example secrets.env && chmod 600 secrets.env   # aunque quede vacío
```

**4. Pruébalo a mano** antes de fiarte de Actions:

```bash
./deploy.sh
```

**5. Empuja algo a `main`** y mira la pestaña Actions. A partir de ahí, solo.

## Cómo funciona

```
push a main
  └─ job test: construye la imagen, la arranca y comprueba el healthcheck
      └─ job deploy: entra en el tailnet (tag:ci) y hace
           ssh -p 2222 $VPS_USER@$VPS_HOST "<nombre-del-repo>"
            └─ la clave tiene comando forzado: no da shell, solo acepta ese nombre
                └─ /home/ubuntu/bin/deploy.sh: git reset --hard origin/main
                    └─ ./deploy.sh del repo: compose up --build + healthcheck
```

El despachador actualiza el código **antes** de ejecutar el `deploy.sh` del repo, así
que el script que corre siempre es el recién traído. Y como el `deploy.sh` de cada
proyecto decide cómo se levanta, cambiar el despliegue es un commit, no entrar al VPS.

Secretos (de organización, visibilidad "Public repositories", ya configurados):

| Secreto | Para qué |
|---|---|
| `TS_OAUTH_CLIENT_ID` / `TS_OAUTH_SECRET` | Cliente OAuth de Tailscale con `tag:ci` |
| `VPS_HOST` / `VPS_USER` | Destino del SSH |
| `VPS_SSH_KEY` | Clave privada del deploy, con comando forzado en el VPS |
| `VPS_KNOWN_HOSTS` | Huella del host, para no desactivar la verificación |

## Puesta a punto del VPS (una sola vez, no por proyecto)

`vps/dispatcher.sh` es la versión que espera esta plantilla: igual que la que ya hay,
pero ejecutando el `deploy.sh` del repo cuando existe. Instalarla:

```bash
install -m 755 vps/dispatcher.sh /home/ubuntu/bin/deploy.sh
```

Y la clave de CI tiene que apuntar ahí. Si en `~/.ssh/authorized_keys` hay una entrada
con una ruta concreta en vez del despachador, el repo que la use desplegará siempre ese
proyecto, pase el nombre que pase:

```
# mal: ignora el nombre del repo
command="bash /home/ubuntu/apps/lab-ai/mcp-server/deploy.sh" ... ssh-ed25519 AAAA...

# bien: enruta por nombre
from="100.64.0.0/10",command="/home/ubuntu/bin/deploy.sh",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty ssh-ed25519 AAAA...
```

## Cosas que duelen una vez

- **El archivo de secretos no puede llamarse `.env`.** Si el compose monta el directorio
  dentro del contenedor, hay librerías (pydantic-settings, y por tanto fastmcp) que
  auto-parsean cualquier `.env` del directorio de trabajo. Y si el contenedor corre con
  `user:` distinto del dueño del archivo, un `.env` con permisos 600 lo mata al arrancar.
- **Fija las versiones con `==`.** Cada despliegue reconstruye la imagen: con rangos
  abiertos, el servicio cambia de versión el día menos pensado y sin avisar.
- **El contenedor corre como uid 1000 si lo pides, pero el usuario del VPS es el 1001.**
  Cuidado al montar volúmenes y escribir en ellos.
- **`docker compose up -d --build` vale para todo.** Si solo cambió código, reutiliza las
  capas y reinicia en segundos; si cambiaron las dependencias, reconstruye.

## Diagnóstico

| Síntoma en Actions | Causa probable |
|---|---|
| `no está habilitado en el VPS` | Falta el clon en `/home/ubuntu/apps/projects/<nombre>` |
| `deploy.sh: No such file or directory` | La clave apunta a una ruta fija en vez del despachador |
| `env file ... not found` | Falta `secrets.env` junto al `docker-compose.yml` |
| El paso de Tailscale falla | El cliente OAuth no tiene permiso sobre `tag:ci` en la ACL |
| `Host key verification failed` | `VPS_KNOWN_HOSTS` no cubre el 2222 (`ssh-keyscan -p 2222 <host>`) |
| `Permission denied (publickey)` | La pública de `VPS_SSH_KEY` no está en `authorized_keys` |

El historial de despliegues del VPS está en `~/deploy.log`.
