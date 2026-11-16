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


# --------------------------------------------------------------------------- telemetría nativa de Claude Code

TIPO_NATIVO = {
    "claude_code.interaction": "AGENT",
    "claude_code.llm_request": "LLM",
    "claude_code.tool": "TOOL",
}


def _attrs(lista: list) -> dict:
    out = {}
    for a in lista or []:
        v = a.get("value", {})
        out[a.get("key")] = next(iter(v.values()), None) if v else None
    return out


def _attr(clave, valor) -> dict:
    if isinstance(valor, int) and not isinstance(valor, bool):
        return {"key": clave, "value": {"intValue": str(valor)}}
    return {"key": clave, "value": {"stringValue": str(valor)}}


def enriquecer_nativo(datos: dict, proyecto_defecto: str = "taller-otel") -> None:
    """Añade atributos OpenInference a los spans de la telemetría nativa de Claude Code (claude_code.*).

    Así Phoenix los muestra como turnos (AGENT), llamadas al modelo (LLM) y herramientas (TOOL), con
    entrada, salida, tokens, alumno y sesión. Los spans del plugin no se tocan.
    """
    for rs in datos.get("resourceSpans", []):
        res = rs.setdefault("resource", {}).setdefault("attributes", [])
        ra = _attrs(res)
        if "openinference.project.name" not in ra:
            res.append(_attr("openinference.project.name", proyecto_defecto))
        alumno = ra.get("taller.alumno") or ra.get("user.email") or ""
        for ss in rs.get("scopeSpans", []):
            for s in ss.get("spans", []):
                tipo = TIPO_NATIVO.get(s.get("name", ""))
                a = _attrs(s.get("attributes"))
                if not s.get("name", "").startswith("claude_code.") or "openinference.span.kind" in a:
                    continue
                nuevos = [_attr("openinference.span.kind", tipo or "CHAIN")]
                if alumno:
                    nuevos.append(_attr("user.id", alumno))
                if s["name"] == "claude_code.interaction":
                    if a.get("user_prompt"):
                        nuevos.append(_attr("input.value", a["user_prompt"]))
                elif s["name"] == "claude_code.llm_request":
                    ent = sum(int(a.get(k) or 0) for k in ("input_tokens", "cache_read_tokens", "cache_creation_tokens"))
                    sal = int(a.get("output_tokens") or 0)
                    nuevos += [_attr("llm.model_name", a.get("model", "")), _attr("llm.provider", "anthropic"),
                               _attr("llm.token_count.prompt", ent), _attr("llm.token_count.completion", sal),
                               _attr("llm.token_count.total", ent + sal)]
                elif s["name"] == "claude_code.tool":
                    nuevos.append(_attr("tool.name", a.get("tool_name", "")))
                    entrada = a.get("full_command") or a.get("file_path") or a.get("subagent_type") or ""
                    if entrada:
                        nuevos.append(_attr("input.value", entrada))
                    for e in s.get("events", []):
                        if e.get("name") == "tool.output":
                            ea = _attrs(e.get("attributes"))
                            salida = ea.get("output") or ea.get("content") or ea.get("diff")
                            if salida:
                                nuevos.append(_attr("output.value", str(salida)[:30000]))
                if tipo is None:
                    nuevos[0] = _attr("openinference.span.kind", "CHAIN")
                s.setdefault("attributes", []).extend(nuevos)


def guardar_jsonl(nombre: str, datos: dict) -> None:
    ruta = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", nombre)
    with open(ruta, "a", encoding="utf-8") as f:
        f.write(json.dumps(datos, ensure_ascii=False) + "\n")


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
        if self.path not in ("/v1/traces", "/v1/logs", "/v1/metrics"):
            return self.responder(404, "no encontrado")
        auth = self.headers.get("Authorization", "")
        if not TOKEN or not hmac.compare_digest(auth.encode(), f"Bearer {TOKEN}".encode()):
            log("401", origen)
            return self.responder(401, "token incorrecto")
        largo = int(self.headers.get("Content-Length", "0") or 0)
        if largo <= 0 or largo > MAX_CUERPO:
            return self.responder(413, "cuerpo vacío o demasiado grande")
        if self.path in ("/v1/logs", "/v1/metrics"):
            # Telemetría nativa: eventos y métricas se guardan tal cual para consultarlos después.
            try:
                datos = json.loads(self.rfile.read(largo))
                guardar_jsonl("otel-logs.jsonl" if self.path == "/v1/logs" else "otel-metrics.jsonl", datos)
            except Exception as e:
                log("400", origen, self.path, type(e).__name__)
                return self.responder(400, "OTLP JSON no válido")
            log(200, self.path, origen)
            return self.responder(200, "{}")
        try:
            datos = json.loads(self.rfile.read(largo))
            enriquecer_nativo(datos)
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
