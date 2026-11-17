# ingesta

Trae los datos de **Hevy** y **Garmin Connect** a `personal_data.db`, la SQLite que el
MCP del VPS ([vps-didac/mcp-server](https://github.com/vps-didac/mcp-server)) consulta
con `query_personal_db`. Cada push a `main` pasa los tests y se despliega solo, como el
resto de proyectos del VPS (creado desde `plantilla-vps`).

```
ingest/
├── db.py          # esquema de la BD + migración. Este repo es su dueño.
├── hevy.py        # API oficial de Hevy → hevy_sets
├── garmin.py      # garminconnect → garmin_daily + cardio_activities
├── common.py      # zona horaria, sync_state
└── __main__.py    # CLI: python -m ingest ...
scripts/test_ingest.py   # CI: la ingesta escribe bien, sin red
```

## Reparto con el MCP

| | ingesta (este repo) | mcp-server |
|---|---|---|
| BD | crea las tablas, migra y escribe | la abre en solo lectura (`mode=ro`) |
| Volumen | `/home/ubuntu/apps/lab-ai/data` → `/data` | el mismo |
| Python | 3.12 (lo pide `garminconnect`) | el suyo |

Si cambias una tabla en `ingest/db.py`, actualiza la docstring de `query_personal_db` en
`tools/health_db.py` del MCP: es lo que le dice al modelo qué columnas hay.

## Fuentes

- **Hevy**: API pública oficial, **requiere Hevy Pro**. Incremental por
  `/v1/workouts/events`: recoge entrenos nuevos, editados y borrados. Un entreno editado
  se reescribe entero. La primera pasada trae todo el historial.
- **Garmin**: no hay API pública para particulares; se usa
  [`garminconnect`](https://github.com/cyberjunky/python-garminconnect) 0.3.x, que se
  autentica como la app de Android. Cada pasada repasa los últimos días (el sueño y el HRV
  se completan con retraso) y las actividades del mismo rango, salvo las de fuerza, que ya
  vienen de Hevy. **Es la pieza frágil**: si Garmin cambia el login, se rompe hasta que
  salga una versión nueva de la librería. Se arregla subiéndola en `requirements.txt`.

El servicio sincroniza las dos cada `INGEST_INTERVAL_MIN` (360 por defecto). Son
independientes: si una falla, la otra sigue, y el error queda en la tabla `sync_state`
(visible desde el MCP con la herramienta `ingest_status`). Todo es idempotente: repetir
una pasada no duplica nada.

## Puesta en marcha en el VPS (una vez)

```bash
cd /home/ubuntu/apps/projects
git clone https://github.com/vps-didac/ingesta.git
cd ingesta
cp secrets.env.example secrets.env && chmod 600 secrets.env
nano secrets.env                                            # HEVY_API_KEY, GARMIN_EMAIL, GARMIN_PASSWORD
docker compose build
docker compose run --rm ingesta python -m ingest garmin-login     # pide el código MFA si lo hay
docker compose run --rm ingesta python -m ingest garmin --days 365  # histórico (opcional)
./deploy.sh
```

Los tokens de Garmin quedan en `/data/garmin_tokens` (0600) y se refrescan solos. Solo
hay que repetir `garmin-login` si `sync_state` muestra un error de MFA.

## CLI

```bash
docker compose run --rm ingesta python -m ingest status            # última pasada de cada fuente
docker compose run --rm ingesta python -m ingest hevy [--full]
docker compose run --rm ingesta python -m ingest garmin [--days N | --since YYYY-MM-DD]
docker compose logs --tail 50 ingesta
```

En local: `pip install -r requirements.txt && python scripts/test_ingest.py`.

## Trampas

- **Secretos en `secrets.env`, nunca `.env`** y nunca en el código: el repo es público.
- **uid 1000 en el contenedor, 1001 en el VPS.** La carpeta de datos ya la escribe el MCP
  con el 1000, por eso este servicio usa el mismo.
- **Versiones fijadas con `==`.** Con más motivo aquí: una versión nueva de
  `garminconnect` puede arreglar o romper el login.
