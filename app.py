"""Servicio mínimo de ejemplo: solo existe para que la plantilla se pueda
desplegar y comprobar de punta a punta antes de escribir nada propio.

Bórralo en cuanto tengas tu aplicación, pero deja algo respondiendo en
/healthz o ajusta HEALTH_URL en deploy.sh y el paso de healthcheck del
workflow, que es lo que decide si un despliegue ha salido bien.
"""
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PUERTO = int(os.environ.get("PORT", "8000"))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        cuerpo = b'{"estado":"ok"}' if self.path == "/healthz" else b'{"hola":"vps"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def log_message(self, formato, *args):  # a stdout, que es donde mira docker logs
        print(f"{self.address_string()} {formato % args}", flush=True)


if __name__ == "__main__":
    print(f"escuchando en 0.0.0.0:{PUERTO}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PUERTO), Handler).serve_forever()
