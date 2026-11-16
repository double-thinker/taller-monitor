#!/usr/bin/env python3
"""Hook de taller-monitor: manda la actividad de la sesión de Claude Code al receptor del profesor.

Solo usa la biblioteca estándar. Nunca bloquea ni cambia el comportamiento de Claude Code:
ante cualquier error, termina con código 0 y sin salida.

Configuración. Gana la variable de entorno, después la opción del plugin y después el fichero
.claude/taller-monitor.json del repositorio ({"url": ..., "token": ..., "proyecto": ...}):
  TALLER_MONITOR_URL       o la opción "url"      URL base del receptor, p. ej. https://equipo.ts.net:10000
  TALLER_MONITOR_TOKEN     o la opción "token"    token que da el profesor
  TALLER_MONITOR_ALUMNO    o la opción "alumno"   nombre visible en el panel (por defecto, el correo de git sin dominio)
  TALLER_MONITOR_PROYECTO  o la opción "proyecto" proyecto de Phoenix (por defecto, "taller")
  TALLER_MONITOR_OFF=1     desactiva el envío sin desinstalar el plugin
"""

from __future__ import annotations

import getpass
import json
import os
import secrets
import sys
import time
import urllib.request
from pathlib import Path

MAX_TEXTO = 30_000  # caracteres por atributo
MAX_PENDIENTES = 5_000_000  # bytes de la cola de reintentos
TIMEOUT = 5  # segundos por envío


# --------------------------------------------------------------------------- configuración

def config_repo() -> dict:
    """Configuración del repositorio del taller: .claude/taller-monitor.json en la raíz del proyecto."""
    base = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    try:
        return json.loads((Path(base) / ".claude" / "taller-monitor.json").read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def opcion(nombre: str, defecto: str = "", repo: dict | None = None) -> str:
    """Prioridad: variable TALLER_MONITOR_*, opción del plugin, fichero del repositorio, valor por defecto."""
    v = os.environ.get(f"TALLER_MONITOR_{nombre.upper()}", "").strip()
    if not v:
        v = os.environ.get(f"CLAUDE_PLUGIN_OPTION_{nombre.upper()}", "").strip()
    if not v and repo:
        v = str(repo.get(nombre, "") or "").strip()
    return v or defecto


def alumno_por_defecto() -> str:
    """El correo o el nombre de git del alumno; si no hay, el usuario del sistema."""
    import subprocess

    for clave in ("user.email", "user.name"):
        try:
            v = subprocess.run(["git", "config", clave], capture_output=True, text=True, timeout=2).stdout.strip()
            if v:
                return v.split("@")[0] if clave == "user.email" else v
        except Exception:
            pass
    try:
        return getpass.getuser()
    except Exception:
        return "anonimo"


def config() -> dict | None:
    if os.environ.get("TALLER_MONITOR_OFF", "") in ("1", "true", "si", "sí"):
        return None
    repo = config_repo()
    url, token = opcion("url", repo=repo), opcion("token", repo=repo)
    if not url or not token:
        return None
    return {
        "url": url.rstrip("/") + "/v1/traces",
        "token": token,
        "alumno": opcion("alumno", repo=repo) or alumno_por_defecto(),
        "proyecto": opcion("proyecto", "taller", repo=repo),
    }


def dir_datos() -> Path:
    base = os.environ.get("CLAUDE_PLUGIN_DATA") or str(Path.home() / ".claude" / "taller-monitor")
    p = Path(base)
    p.mkdir(parents=True, exist_ok=True)
    return p


# --------------------------------------------------------------------------- estado por sesión

class Estado:
    """Estado de una sesión en un fichero JSON, protegido con un cerrojo (los hooks van en paralelo)."""

    def __init__(self, session_id: str):
        d = dir_datos() / "sesiones"
        d.mkdir(exist_ok=True)
        seguro = "".join(c for c in session_id if c.isalnum() or c in "-_") or "sin-sesion"
        self.ruta = d / f"{seguro}.json"
        self.cerrojo = None
        self.datos: dict = {}

    def __enter__(self):
        self.cerrojo = open(str(self.ruta) + ".lock", "a+", encoding="utf-8")
        try:
            import fcntl

            fcntl.flock(self.cerrojo, fcntl.LOCK_EX)
        except ImportError:  # Windows: sin cerrojo
            pass
        try:
            self.datos = json.loads(self.ruta.read_text(encoding="utf-8"))
        except Exception:
            self.datos = {}
        return self

    def __exit__(self, *exc):
        try:
            self.ruta.write_text(json.dumps(self.datos), encoding="utf-8")
        finally:
            self.cerrojo.close()


# --------------------------------------------------------------------------- spans OTLP (JSON)

def ahora_ns() -> int:
    return time.time_ns()


def nuevo_id(n_bytes: int) -> str:
    return secrets.token_hex(n_bytes)


def texto(v) -> str:
    if v is None:
        return ""
    if not isinstance(v, str):
        try:
            v = json.dumps(v, ensure_ascii=False)
        except Exception:
            v = str(v)
    if len(v) > MAX_TEXTO:
        v = v[:MAX_TEXTO] + f"\n[... recortado, {len(v)} caracteres en total]"
    return v


def atributo(clave: str, valor) -> dict:
    if isinstance(valor, bool):
        return {"key": clave, "value": {"boolValue": valor}}
    if isinstance(valor, int):
        return {"key": clave, "value": {"intValue": str(valor)}}
    if isinstance(valor, float):
        return {"key": clave, "value": {"doubleValue": valor}}
    return {"key": clave, "value": {"stringValue": texto(valor)}}


def span(cfg, sesion, *, trace_id, span_id, padre, nombre, tipo, inicio, fin, attrs, error=None) -> dict:
    base = {
        "openinference.span.kind": tipo,
        "session.id": sesion,
        "user.id": cfg["alumno"],
    }
    base.update({k: v for k, v in attrs.items() if v is not None and v != ""})
    s = {
        "traceId": trace_id,
        "spanId": span_id,
        "name": nombre,
        "kind": 1,
        "startTimeUnixNano": str(inicio),
        "endTimeUnixNano": str(max(fin, inicio)),
        "attributes": [atributo(k, v) for k, v in base.items()],
        "status": {"code": 2, "message": texto(error)[:500]} if error else {"code": 1},
    }
    if padre:
        s["parentSpanId"] = padre
    return s


def lote(cfg, spans: list[dict]) -> dict:
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        atributo("openinference.project.name", cfg["proyecto"]),
                        atributo("service.name", "claude-code"),
                        atributo("taller.alumno", cfg["alumno"]),
                    ]
                },
                "scopeSpans": [{"scope": {"name": "taller-monitor", "version": "1.0.0"}, "spans": spans}],
            }
        ]
    }


# --------------------------------------------------------------------------- envío con cola de reintentos

def enviar(cfg, spans: list[dict]) -> None:
    if not spans:
        return
    cola = dir_datos() / "pendientes.jsonl"
    cuerpos = []
    if cola.exists():
        try:
            cuerpos = [l for l in cola.read_text(encoding="utf-8").splitlines() if l.strip()]
            cola.unlink()
        except Exception:
            cuerpos = []
    cuerpos.append(json.dumps(lote(cfg, spans), ensure_ascii=False))
    fallidos = []
    for cuerpo in cuerpos:
        try:
            req = urllib.request.Request(
                cfg["url"],
                data=cuerpo.encode("utf-8"),
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {cfg['token']}",
                    # Cloudflare rechaza el agente por defecto de Python (error 1010).
                    "User-Agent": "taller-monitor/1.1.2",
                },
            )
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                if r.status >= 300:
                    fallidos.append(cuerpo)
        except urllib.error.HTTPError as e:
            if os.environ.get("TALLER_MONITOR_DEBUG"):
                print(f"taller-monitor: HTTP {e.code} {e.read()[:200]!r}", file=sys.stderr)
            if e.code >= 500 or e.code == 429:
                fallidos.append(cuerpo)
            # 401/403/413: no se reintenta
        except Exception:
            fallidos.append(cuerpo)
    if fallidos:
        datos = "\n".join(fallidos) + "\n"
        if len(datos) <= MAX_PENDIENTES:
            with open(cola, "a", encoding="utf-8") as f:
                f.write(datos)


# --------------------------------------------------------------------------- transcript: llamadas al modelo

def llamadas_modelo(transcript: str, desde: int) -> tuple[list[dict], int]:
    """Lee las respuestas del modelo añadidas al transcript desde el byte `desde`."""
    salida = []
    try:
        with open(transcript, "rb") as f:
            f.seek(desde)
            bruto = f.read()
            fin = desde + len(bruto)
    except Exception:
        return [], desde
    # Solo líneas completas
    corte = bruto.rfind(b"\n")
    if corte < 0:
        return [], desde
    fin = desde + corte + 1
    for linea in bruto[: corte + 1].splitlines():
        try:
            e = json.loads(linea)
        except Exception:
            continue
        if e.get("type") != "assistant":
            continue
        m = e.get("message") or {}
        uso = m.get("usage") or {}
        partes = m.get("content") or []
        textos = [p.get("text", "") for p in partes if isinstance(p, dict) and p.get("type") == "text"]
        herramientas = [p.get("name", "") for p in partes if isinstance(p, dict) and p.get("type") == "tool_use"]
        salida.append(
            {
                "modelo": m.get("model", ""),
                "id": m.get("id", ""),
                "entrada": int(uso.get("input_tokens", 0) or 0)
                + int(uso.get("cache_read_input_tokens", 0) or 0)
                + int(uso.get("cache_creation_input_tokens", 0) or 0),
                "salida": int(uso.get("output_tokens", 0) or 0),
                "texto": "\n".join(t for t in textos if t),
                "herramientas": herramientas,
                "ts": e.get("timestamp", ""),
            }
        )
    # Las respuestas en streaming se escriben en varias líneas con el mismo id: se quedan con la última.
    unicas: dict[str, dict] = {}
    for s in salida:
        clave = s["id"] or str(len(unicas))
        if clave in unicas:
            prev = unicas[clave]
            s["texto"] = "\n".join(t for t in (prev["texto"], s["texto"]) if t)
            s["herramientas"] = prev["herramientas"] + s["herramientas"]
        unicas[clave] = s
    return list(unicas.values()), fin


def ts_a_ns(ts: str, defecto: int) -> int:
    try:
        from datetime import datetime

        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1e9)
    except Exception:
        return defecto


# --------------------------------------------------------------------------- eventos

def turno_abierto(st: dict, cfg, sesion, t: int, ev: dict | None = None) -> dict:
    """Devuelve el turno en curso.

    Si no hay turno abierto y el evento viene de un subagente en segundo plano, se cuelga del último
    turno cerrado (el que lanzó el subagente). En otro caso (p. ej. herramienta tras /resume), abre
    un turno implícito.
    """
    if not st.get("turno") and ev and ev.get("agent_id") and st.get("ultimo"):
        return st["ultimo"]
    if not st.get("turno"):
        st["turno"] = {"trace": nuevo_id(16), "span": nuevo_id(8), "inicio": t, "prompt": "", "n_tools": 0, "errores": 0}
    return st["turno"]


def procesar(ev: dict, cfg: dict) -> list[dict]:
    nombre_ev = ev.get("hook_event_name", "")
    sesion = ev.get("session_id", "") or "sin-sesion"
    t = ahora_ns()
    spans: list[dict] = []
    with Estado(sesion) as e:
        st = e.datos
        st.setdefault("herr", {})
        if "offset" not in st:
            st["offset"] = 0
            # Al reanudar una sesión, no se reenvía el historial anterior.
            tp = ev.get("transcript_path")
            if tp and os.path.exists(tp) and nombre_ev != "SessionStart":
                st["offset"] = os.path.getsize(tp)
            elif tp and os.path.exists(tp) and ev.get("source") in ("resume", "compact", "fork"):
                st["offset"] = os.path.getsize(tp)

        comunes = {
            "taller.cwd": ev.get("cwd", ""),
            "taller.proyecto_local": os.path.basename(ev.get("cwd", "") or ""),
            "taller.permission_mode": ev.get("permission_mode", ""),
        }

        if nombre_ev == "SessionStart":
            spans.append(
                span(cfg, sesion, trace_id=nuevo_id(16), span_id=nuevo_id(8), padre=None,
                     nombre="inicio de sesión", tipo="CHAIN", inicio=t, fin=t,
                     attrs={**comunes, "input.value": ev.get("source", ""), "llm.model_name": ev.get("model", "")})
            )

        elif nombre_ev == "UserPromptSubmit":
            # Si quedó un turno sin cerrar (Ctrl+C), se cierra antes de abrir el nuevo.
            if st.get("turno"):
                spans += cerrar_turno(st, cfg, sesion, ev, t, comunes, cierre="interrumpido")
            st["turno"] = {"trace": nuevo_id(16), "span": nuevo_id(8), "inicio": t,
                           "prompt": ev.get("prompt", ""), "n_tools": 0, "errores": 0}

        elif nombre_ev == "PreToolUse":
            tu = ev.get("tool_use_id") or nuevo_id(8)
            st["herr"][tu] = t
            if len(st["herr"]) > 200:  # herramientas denegadas que nunca tendrán PostToolUse
                for k in sorted(st["herr"], key=st["herr"].get)[:100]:
                    st["herr"].pop(k, None)
            turno_abierto(st, cfg, sesion, t, ev)

        elif nombre_ev in ("PostToolUse", "PostToolUseFailure"):
            turno = turno_abierto(st, cfg, sesion, t, ev)
            tu = ev.get("tool_use_id", "")
            inicio = st["herr"].pop(tu, t) if tu else t
            error = None
            if nombre_ev == "PostToolUseFailure":
                error = ev.get("error") or ev.get("tool_response") or "fallo"
                turno["errores"] += 1
            turno["n_tools"] += 1
            herramienta = ev.get("tool_name", "herramienta")
            spans.append(
                span(cfg, sesion, trace_id=turno["trace"], span_id=nuevo_id(8), padre=turno["span"],
                     nombre=herramienta, tipo="TOOL", inicio=inicio, fin=t, error=error,
                     attrs={**comunes, "tool.name": herramienta,
                            "input.value": texto(ev.get("tool_input")), "input.mime_type": "application/json",
                            "output.value": texto(ev.get("tool_response") if error is None else error),
                            "taller.agente": ev.get("agent_type", "")})
            )

        elif nombre_ev == "SubagentStop":
            turno = turno_abierto(st, cfg, sesion, t, ev)
            spans.append(
                span(cfg, sesion, trace_id=turno["trace"], span_id=nuevo_id(8), padre=turno["span"],
                     nombre=f"subagente {ev.get('agent_type', '')}".strip(), tipo="AGENT", inicio=t, fin=t,
                     attrs={**comunes, "output.value": ev.get("last_assistant_message", ""),
                            "taller.agente": ev.get("agent_type", "")})
            )

        elif nombre_ev == "PreCompact":
            spans.append(
                span(cfg, sesion, trace_id=nuevo_id(16), span_id=nuevo_id(8), padre=None,
                     nombre="compactación de contexto", tipo="CHAIN", inicio=t, fin=t,
                     attrs={**comunes, "input.value": ev.get("trigger", "") or ev.get("custom_instructions", "")})
            )

        elif nombre_ev == "Stop":
            if st.get("turno"):
                spans += cerrar_turno(st, cfg, sesion, ev, t, comunes)

        elif nombre_ev == "SessionEnd":
            if st.get("turno"):
                spans += cerrar_turno(st, cfg, sesion, ev, t, comunes, cierre="fin de sesión")
            spans.append(
                span(cfg, sesion, trace_id=nuevo_id(16), span_id=nuevo_id(8), padre=None,
                     nombre="fin de sesión", tipo="CHAIN", inicio=t, fin=t,
                     attrs={**comunes, "input.value": ev.get("reason", "")})
            )
    return spans


def cerrar_turno(st, cfg, sesion, ev, t, comunes, cierre="normal") -> list[dict]:
    turno = st.pop("turno")
    st["ultimo"] = {"trace": turno["trace"], "span": turno["span"], "inicio": turno["inicio"],
                    "prompt": turno.get("prompt", ""), "n_tools": 0, "errores": 0}
    spans = []
    llamadas, st["offset"] = llamadas_modelo(ev.get("transcript_path", ""), st.get("offset", 0))
    tot_in = tot_out = 0
    modelo = ""
    prev = turno["inicio"]
    for ll in llamadas:
        fin = ts_a_ns(ll["ts"], t)
        fin = min(max(fin, turno["inicio"]), t)
        tot_in += ll["entrada"]
        tot_out += ll["salida"]
        modelo = ll["modelo"] or modelo
        spans.append(
            span(cfg, sesion, trace_id=turno["trace"], span_id=nuevo_id(8), padre=turno["span"],
                 nombre="modelo", tipo="LLM", inicio=prev, fin=fin,
                 attrs={"llm.model_name": ll["modelo"], "llm.provider": "anthropic",
                        "llm.token_count.prompt": ll["entrada"], "llm.token_count.completion": ll["salida"],
                        "llm.token_count.total": ll["entrada"] + ll["salida"],
                        "output.value": ll["texto"] or ("herramientas: " + ", ".join(ll["herramientas"]))})
        )
        prev = fin
    # Stop trae la respuesta final. Si el turno se cierra de otra forma (Ctrl+C, fin de sesión, modo -p),
    # se usa el último texto del modelo que haya en el transcript.
    respuesta = ev.get("last_assistant_message", "") if cierre == "normal" else ""
    if not respuesta:
        textos = [ll["texto"] for ll in llamadas if ll["texto"]]
        respuesta = textos[-1] if textos else ""
    if cierre == "interrumpido":
        respuesta = (respuesta + "\n\n" if respuesta else "") + "[turno interrumpido]"
    spans.append(
        span(cfg, sesion, trace_id=turno["trace"], span_id=turno["span"], padre=None,
             nombre="turno", tipo="AGENT", inicio=turno["inicio"], fin=t,
             attrs={**comunes, "input.value": turno["prompt"], "output.value": respuesta,
                    "llm.model_name": modelo,
                    "llm.token_count.prompt": tot_in, "llm.token_count.completion": tot_out,
                    "llm.token_count.total": tot_in + tot_out,
                    "taller.herramientas": turno["n_tools"], "taller.errores": turno["errores"],
                    "taller.cierre": cierre})
    )
    return spans


def main() -> int:
    try:
        cfg = config()
        if cfg is None:
            return 0
        ev = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
        if os.environ.get("TALLER_MONITOR_DEBUG"):
            with open(dir_datos() / "eventos-debug.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        enviar(cfg, procesar(ev, cfg))
    except Exception:
        if os.environ.get("TALLER_MONITOR_DEBUG"):
            raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
