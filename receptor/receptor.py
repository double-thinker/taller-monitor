#!/usr/bin/env python3
"""Receptor del taller: recibe trazas OTLP en JSON de los alumnos y las reenvía a Phoenix en protobuf.

Es lo único que se expone a internet. Phoenix (interfaz, API y MCP) sigue escuchando solo en 127.0.0.1.

  POST /v1/traces   Authorization: Bearer <token>   cuerpo OTLP JSON   -> Phoenix
  GET  /salud       sin token                                            -> "ok"
  cualquier otra ruta                                                    -> 404

Variables:
  TALLER_MONITOR_TOKEN   token compartido (obligatorio)
  RECEPTOR_PUERTO        puerto local (por defecto 6010)
  PHOENIX_URL            Phoenix local (por defecto http://127.0.0.1:6006)
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import sys
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from google.protobuf import json_format
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2

TOKEN = os.environ.get("TALLER_MONITOR_TOKEN", "")
PUERTO = int(os.environ.get("RECEPTOR_PUERTO", "6010"))
PHOENIX = os.environ.get("PHOENIX_URL", "http://127.0.0.1:6006").rstrip("/")
MAX_CUERPO = 10 * 1024 * 1024


def hex_a_b64(v: str) -> str:
    return base64.b64encode(bytes.fromhex(v)).decode()


def a_protobuf(datos: dict) -> tuple[bytes, int, set[str]]:
    """OTLP JSON (ids en hex, como manda la especificación) -> ExportTraceServiceRequest serializado."""
    alumnos: set[str] = set()
    n = 0
    for rs in datos.get("resourceSpans", []):
        for a in rs.get("resource", {}).get("attributes", []):
            if a.get("key") == "taller.alumno":
                alumnos.add(a.get("value", {}).get("stringValue", ""))
        for ss in rs.get("scopeSpans", []):
            for s in ss.get("spans", []):
                n += 1
                for k in ("traceId", "spanId", "parentSpanId"):
                    if s.get(k):
                        s[k] = hex_a_b64(s[k])
    msg = json_format.ParseDict(datos, trace_service_pb2.ExportTraceServiceRequest())
    return msg.SerializeToString(), n, alumnos


def log(*partes) -> None:
    print(time.strftime("%H:%M:%S"), *partes, flush=True)


class Manejador(BaseHTTPRequestHandler):
    server_version = "taller-receptor/1.0"

    def log_message(self, *args):  # silencia el log por defecto
        pass

    def responder(self, codigo: int, texto: str = "") -> None:
        cuerpo = texto.encode()
        self.send_response(codigo)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def do_GET(self):
        if self.path == "/salud":
            self.responder(200, "ok")
        else:
            self.responder(404, "no encontrado")

    def do_POST(self):
        origen = self.headers.get("X-Forwarded-For", self.client_address[0])
        if self.path != "/v1/traces":
            return self.responder(404, "no encontrado")
        auth = self.headers.get("Authorization", "")
        if not TOKEN or not hmac.compare_digest(auth.encode(), f"Bearer {TOKEN}".encode()):
            log("401", origen)
            return self.responder(401, "token incorrecto")
        largo = int(self.headers.get("Content-Length", "0") or 0)
        if largo <= 0 or largo > MAX_CUERPO:
            return self.responder(413, "cuerpo vacío o demasiado grande")
        try:
            datos = json.loads(self.rfile.read(largo))
            pb, n, alumnos = a_protobuf(datos)
        except Exception as e:
            log("400", origen, type(e).__name__, str(e)[:200])
            return self.responder(400, "OTLP JSON no válido")
        try:
            req = urllib.request.Request(
                PHOENIX + "/v1/traces", data=pb, method="POST",
                headers={"Content-Type": "application/x-protobuf"},
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                codigo = r.status
        except Exception as e:
            log("502", origen, "Phoenix no responde:", str(e)[:200])
            return self.responder(502, "Phoenix no disponible")
        log(codigo, ",".join(sorted(alumnos)) or "?", f"{n} spans", origen)
        self.responder(200 if codigo < 300 else 502, "ok")


def main() -> int:
    if not TOKEN:
        print("Falta TALLER_MONITOR_TOKEN", file=sys.stderr)
        return 1
    srv = ThreadingHTTPServer(("127.0.0.1", PUERTO), Manejador)
    log(f"receptor en http://127.0.0.1:{PUERTO} -> {PHOENIX}")
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
