"""Reconstrucción de trazas OpenInference a partir de los eventos de hooks de Claude Code.

El cliente (plugin o hook HTTP) manda cada evento tal cual. Aquí se guarda el estado de cada sesión y
se emiten spans:

- turno (AGENT): de UserPromptSubmit a Stop. Raíz de la traza.
- modelo (LLM): llamadas al modelo que el plugin lee del transcript en Stop y SubagentStop.
- herramienta (TOOL): PostToolUse o PostToolUseFailure. El inicio sale de duration_ms.
- subagente (AGENT): de SubagentStart a SubagentStop. Sus herramientas y llamadas cuelgan de él.
- herramienta sin resultado: PreToolUse sin PostToolUse pasados GRACIA segundos (denegada o
  interrumpida), o PermissionDenied.

Los eventos pueden llegar desordenados o tarde (hooks en paralelo, cola de reintentos): cada uno
lleva su hora (`ts`) y su padre se decide con lo que se sabía cuando empezó.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path

MAX_TEXTO = 30_000
GRACIA = 90  # segundos de espera antes de dar por perdida una herramienta sin resultado


def _id(n: int) -> str:
    return secrets.token_hex(n)


def _texto(v) -> str:
    if v is None:
        return ""
    if not isinstance(v, str):
        try:
            v = json.dumps(v, ensure_ascii=False)
        except Exception:
            v = str(v)
    return v if len(v) <= MAX_TEXTO else v[:MAX_TEXTO] + f"\n[... recortado, {len(v)} caracteres]"


def _atributo(clave: str, valor) -> dict:
    if isinstance(valor, bool):
        return {"key": clave, "value": {"boolValue": valor}}
    if isinstance(valor, int):
        return {"key": clave, "value": {"intValue": str(valor)}}
    if isinstance(valor, float):
        return {"key": clave, "value": {"doubleValue": valor}}
    return {"key": clave, "value": {"stringValue": _texto(valor)}}


def _ts_iso(ts: str, defecto: int) -> int:
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1e9)
    except Exception:
        return defecto


class Sesion:
    """Estado de una sesión de Claude Code. Solo lo toca el receptor, bajo su cerrojo."""

    def __init__(self, datos: dict | None = None):
        d = datos or {}
        self.turno: dict | None = d.get("turno")
        self.ultimo: dict | None = d.get("ultimo")
        self.herr: dict = d.get("herr", {})  # tool_use_id -> {inicio, nombre, entrada, trace, padre, agente}
        self.hechas: list = d.get("hechas", [])  # tool_use_id ya cerrados (por si PreToolUse llega tarde)
        self.agentes: dict = d.get("agentes", {})  # agent_id -> {trace, span, padre, inicio, tipo}
        self.meta: dict = d.get("meta", {})

    def a_dict(self) -> dict:
        return {"turno": self.turno, "ultimo": self.ultimo, "herr": self.herr,
                "hechas": self.hechas[-500:], "agentes": self.agentes, "meta": self.meta}


class Reconstructor:
    def __init__(self, dir_estado: str | Path):
        self.dir = Path(dir_estado)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.sesiones: dict[str, Sesion] = {}
        self.cerrojo = threading.Lock()

    # ---------------------------------------------------------------- estado

    def _ruta(self, sid: str) -> Path:
        return self.dir / ("".join(c for c in sid if c.isalnum() or c in "-_") or "sin-sesion")

    def _sesion(self, sid: str) -> Sesion:
        if sid not in self.sesiones:
            try:
                self.sesiones[sid] = Sesion(json.loads(self._ruta(sid).with_suffix(".json").read_text("utf-8")))
            except Exception:
                self.sesiones[sid] = Sesion()
        return self.sesiones[sid]

    def _guardar(self, sid: str) -> None:
        ruta = self._ruta(sid).with_suffix(".json")
        tmp = ruta.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.sesiones[sid].a_dict(), ensure_ascii=False), "utf-8")
        os.replace(tmp, ruta)

    # ---------------------------------------------------------------- spans

    def _span(self, s: Sesion, sid, *, trace, span, padre, nombre, tipo, inicio, fin, attrs, error=None) -> dict:
        base = {"openinference.span.kind": tipo, "session.id": sid, "user.id": s.meta.get("alumno", "")}
        for k in ("taller.cwd", "taller.proyecto_local", "taller.so", "taller.cliente"):
            if s.meta.get(k):
                base[k] = s.meta[k]
        base.update({k: v for k, v in attrs.items() if v not in (None, "")})
        out = {
            "traceId": trace, "spanId": span, "name": nombre, "kind": 1,
            "startTimeUnixNano": str(int(inicio)), "endTimeUnixNano": str(int(max(fin, inicio))),
            "attributes": [_atributo(k, v) for k, v in base.items()],
            "status": {"code": 2, "message": _texto(error)[:500]} if error else {"code": 1},
        }
        if padre:
            out["parentSpanId"] = padre
        return out

    def _abrir_turno(self, s: Sesion, ts: int, prompt: str = "") -> dict:
        s.turno = {"trace": _id(16), "span": _id(8), "inicio": ts, "prompt": prompt, "n_tools": 0, "errores": 0}
        return s.turno

    def _contexto(self, s: Sesion, ev: dict, ts: int) -> tuple[str, str]:
        """Traza y padre para un evento: el subagente si viene de uno; si no, el turno en curso, o el último."""
        aid = ev.get("agent_id")
        if aid:
            ag = s.agentes.get(aid) or self._registrar_agente(s, ev, ts)
            return ag["trace"], ag["span"]
        t = s.turno or s.ultimo or self._abrir_turno(s, ts)
        return t["trace"], t["span"]

    def _registrar_agente(self, s: Sesion, ev: dict, ts: int) -> dict:
        t = s.turno or s.ultimo or self._abrir_turno(s, ts)
        ag = {"trace": t["trace"], "span": _id(8), "padre": t["span"], "inicio": ts,
              "tipo": ev.get("agent_type", "")}
        s.agentes[ev["agent_id"]] = ag
        return ag

    def _llm(self, s, sid, trace, padre, llamadas: list, inicio: int, fin: int) -> tuple[list, int, int, str]:
        spans, ent, sal, modelo, prev = [], 0, 0, "", inicio
        for ll in llamadas or []:
            f = min(max(_ts_iso(ll.get("ts", ""), fin), inicio), fin)
            ent += int(ll.get("entrada") or 0)
            sal += int(ll.get("salida") or 0)
            modelo = ll.get("modelo") or modelo
            texto = ll.get("texto") or ("herramientas: " + ", ".join(ll.get("herramientas") or []))
            spans.append(self._span(s, sid, trace=trace, span=_id(8), padre=padre, nombre="modelo", tipo="LLM",
                                    inicio=prev, fin=f, attrs={
                                        "llm.model_name": ll.get("modelo"), "llm.provider": "anthropic",
                                        "llm.token_count.prompt": int(ll.get("entrada") or 0),
                                        "llm.token_count.completion": int(ll.get("salida") or 0),
                                        "llm.token_count.total": int(ll.get("entrada") or 0) + int(ll.get("salida") or 0),
                                        "output.value": texto}))
            prev = f
        return spans, ent, sal, modelo

    def _cerrar_turno(self, s: Sesion, sid, ts: int, ev: dict, llamadas, cierre: str, error=None) -> list:
        t = s.turno
        anterior = s.ultimo
        s.turno = None
        s.ultimo = {k: t[k] for k in ("trace", "span", "inicio", "prompt")}
        # El transcript puede traer llamadas de un turno anterior que se cerró sin Stop (Ctrl+C, modo -p,
        # avisos de subagentes). Se reparten por hora: las anteriores a este turno van al anterior.
        margen = 2 * 1_000_000_000
        propias, previas = [], []
        for ll in llamadas or []:
            h = _ts_iso(ll.get("ts", ""), ts)
            (previas if anterior and h < t["inicio"] - margen and h >= anterior["inicio"] - margen else propias).append(ll)
        llamadas = propias
        spans = []
        if previas:
            spans += self._llm(s, sid, anterior["trace"], anterior["span"], previas, anterior["inicio"], t["inicio"])[0]
        llm, ent, sal, modelo = self._llm(s, sid, t["trace"], t["span"], llamadas, t["inicio"], ts)
        spans += llm
        respuesta = ev.get("last_assistant_message") or next(
            (ll.get("texto") for ll in reversed(llamadas or []) if ll.get("texto")), "")
        if cierre == "interrumpido":
            respuesta = (respuesta + "\n\n" if respuesta else "") + "[turno interrumpido]"
        prompt = t.get("prompt", "")
        nombre = "turno"
        if prompt.lstrip().startswith("<task-notification"):
            nombre = "aviso de subagente"
        elif not prompt:
            nombre = "turno sin prompt"
        spans.append(self._span(s, sid, trace=t["trace"], span=t["span"], padre=None, nombre=nombre, tipo="AGENT",
                                inicio=t["inicio"], fin=ts, error=error, attrs={
                                    "input.value": prompt, "output.value": respuesta,
                                    "llm.model_name": modelo, "llm.token_count.prompt": ent,
                                    "llm.token_count.completion": sal, "llm.token_count.total": ent + sal,
                                    "taller.herramientas": t.get("n_tools", 0), "taller.errores": t.get("errores", 0),
                                    "taller.cierre": cierre}))
        return spans

    def _herramienta_sin_resultado(self, s: Sesion, sid, tu: str, h: dict, motivo: str, ts: int) -> dict:
        s.hechas.append(tu)
        return self._span(s, sid, trace=h["trace"], span=_id(8), padre=h["padre"], nombre=h["nombre"], tipo="TOOL",
                          inicio=h["inicio"], fin=max(ts, h["inicio"]), error=motivo,
                          attrs={"tool.name": h["nombre"], "input.value": h.get("entrada"),
                                 "input.mime_type": "application/json", "output.value": motivo,
                                 "taller.agente": h.get("agente", ""), "taller.sin_resultado": True})

    # ---------------------------------------------------------------- entrada

    def procesar(self, msg: dict) -> tuple[str, list[dict]]:
        """Procesa un evento del cliente. Devuelve el proyecto de Phoenix y los spans que hay que emitir."""
        ev = msg.get("evento") or msg  # un hook HTTP manda el evento sin envoltorio
        sid = ev.get("session_id") or "sin-sesion"
        ts = int(msg.get("ts") or time.time_ns())
        nombre = ev.get("hook_event_name", "")
        with self.cerrojo:
            s = self._sesion(sid)
            s.meta.update({k: v for k, v in {
                "alumno": msg.get("alumno") or s.meta.get("alumno") or sid[:8],
                "proyecto": msg.get("proyecto") or s.meta.get("proyecto") or "taller",
                "taller.cwd": ev.get("cwd"), "taller.proyecto_local": os.path.basename((ev.get("cwd") or "").rstrip("/\\").replace("\\", "/")),
                "taller.so": (msg.get("cliente") or {}).get("so"),
                "taller.cliente": (msg.get("cliente") or {}).get("version") or ("hook-http" if "evento" not in msg else None),
            }.items() if v})
            spans = self._evento(s, sid, ev, ts, msg)
            spans += self._caducadas(s, sid, ts)
            self._guardar(sid)
            return s.meta.get("proyecto", "taller"), spans

    def _evento(self, s: Sesion, sid, ev: dict, ts: int, msg: dict) -> list:
        n = ev.get("hook_event_name", "")
        out: list = []
        if n == "SessionStart":
            out.append(self._span(s, sid, trace=_id(16), span=_id(8), padre=None, nombre="inicio de sesión",
                                  tipo="CHAIN", inicio=ts, fin=ts,
                                  attrs={"input.value": ev.get("source"), "llm.model_name": ev.get("model")}))
        elif n == "UserPromptSubmit":
            if s.turno:
                out += self._cerrar_turno(s, sid, ts, {}, [], "interrumpido")
            self._abrir_turno(s, ts, ev.get("prompt", ""))
        elif n == "PreToolUse":
            tu = ev.get("tool_use_id") or _id(8)
            if tu not in s.hechas:
                trace, padre = self._contexto(s, ev, ts)
                s.herr[tu] = {"inicio": ts, "nombre": ev.get("tool_name", "herramienta"), "trace": trace,
                              "padre": padre, "entrada": _texto(ev.get("tool_input")),
                              "agente": ev.get("agent_type", "")}
        elif n in ("PostToolUse", "PostToolUseFailure"):
            tu = ev.get("tool_use_id", "")
            h = s.herr.pop(tu, None)
            if h is None:
                trace, padre = self._contexto(s, ev, ts)
                h = {"inicio": ts - int(ev.get("duration_ms") or 0) * 1_000_000, "trace": trace, "padre": padre}
            error = (ev.get("error") or "fallo") if n == "PostToolUseFailure" else None
            t = s.turno or s.ultimo
            if t is not None and "n_tools" in t:
                t["n_tools"] = t.get("n_tools", 0) + 1
                t["errores"] = t.get("errores", 0) + (1 if error else 0)
            if tu:
                s.hechas.append(tu)
            herramienta = ev.get("tool_name", "herramienta")
            out.append(self._span(s, sid, trace=h["trace"], span=_id(8), padre=h["padre"], nombre=herramienta,
                                  tipo="TOOL", inicio=h["inicio"], fin=ts, error=error, attrs={
                                      "tool.name": herramienta, "input.value": _texto(ev.get("tool_input")),
                                      "input.mime_type": "application/json",
                                      "output.value": _texto(ev.get("tool_response") if error is None else error),
                                      "taller.agente": ev.get("agent_type", ""),
                                      "taller.interrupcion": bool(ev.get("is_interrupt"))}))
        elif n == "PermissionDenied":
            tu = ev.get("tool_use_id") or _id(8)
            h = s.herr.pop(tu, None)
            if h is None:
                trace, padre = self._contexto(s, ev, ts)
                h = {"inicio": ts, "nombre": ev.get("tool_name", "herramienta"), "trace": trace, "padre": padre,
                     "entrada": _texto(ev.get("tool_input")), "agente": ev.get("agent_type", "")}
            out.append(self._herramienta_sin_resultado(
                s, sid, tu, h, "denegada: " + _texto(ev.get("reason") or ev.get("message") or "permiso denegado"), ts))
        elif n == "SubagentStart":
            if ev.get("agent_id") and ev["agent_id"] not in s.agentes:
                self._registrar_agente(s, ev, ts)
        elif n == "SubagentStop":
            aid = ev.get("agent_id") or _id(8)
            if aid not in s.agentes:
                self._registrar_agente(s, {**ev, "agent_id": aid}, ts)
            ag = s.agentes.pop(aid)
            llm, ent, sal, modelo = self._llm(s, sid, ag["trace"], ag["span"], msg.get("llamadas"), ag["inicio"], ts)
            out += llm
            respuesta = ev.get("last_assistant_message") or next(
                (ll.get("texto") for ll in reversed(msg.get("llamadas") or []) if ll.get("texto")), "")
            out.append(self._span(s, sid, trace=ag["trace"], span=ag["span"], padre=ag["padre"],
                                  nombre=f"subagente {ag.get('tipo') or ev.get('agent_type', '')}".strip(),
                                  tipo="AGENT", inicio=ag["inicio"], fin=ts, attrs={
                                      "output.value": respuesta, "llm.model_name": modelo,
                                      "llm.token_count.prompt": ent, "llm.token_count.completion": sal,
                                      "llm.token_count.total": ent + sal,
                                      "taller.agente": ag.get("tipo") or ev.get("agent_type", "")}))
        elif n == "PreCompact":
            out.append(self._span(s, sid, trace=_id(16), span=_id(8), padre=None, nombre="compactación de contexto",
                                  tipo="CHAIN", inicio=ts, fin=ts, attrs={"input.value": ev.get("trigger")}))
        elif n in ("Stop", "StopFailure"):
            if not s.turno:
                self._abrir_turno(s, ts)
            error = _texto(ev.get("error") or ev.get("reason") or "error de la API") if n == "StopFailure" else None
            out += self._cerrar_turno(s, sid, ts, ev, msg.get("llamadas"), "error" if error else "normal", error)
        elif n == "SessionEnd":
            if s.turno:
                out += self._cerrar_turno(s, sid, ts, ev, msg.get("llamadas"), "fin de sesión")
            out.append(self._span(s, sid, trace=_id(16), span=_id(8), padre=None, nombre="fin de sesión",
                                  tipo="CHAIN", inicio=ts, fin=ts, attrs={"input.value": ev.get("reason")}))
        return out

    def _caducadas(self, s: Sesion, sid, ts: int) -> list:
        """Herramientas que empezaron hace más de GRACIA segundos y nunca terminaron."""
        out = []
        for tu, h in list(s.herr.items()):
            if ts - h["inicio"] > GRACIA * 1_000_000_000:
                s.herr.pop(tu)
                out.append(self._herramienta_sin_resultado(s, sid, tu, h, "sin resultado: denegada o interrumpida", ts))
        return out

    def barrer(self) -> list[tuple[str, list[dict]]]:
        """Revisa todas las sesiones en memoria (lo llama un hilo del receptor cada pocos segundos)."""
        salida = []
        ts = time.time_ns()
        with self.cerrojo:
            for sid, s in self.sesiones.items():
                spans = self._caducadas(s, sid, ts)
                if spans:
                    self._guardar(sid)
                    salida.append((s.meta.get("proyecto", "taller"), spans))
        return salida


def lote(proyecto: str, spans: list[dict]) -> dict:
    return {"resourceSpans": [{
        "resource": {"attributes": [_atributo("openinference.project.name", proyecto),
                                    _atributo("service.name", "claude-code")]},
        "scopeSpans": [{"scope": {"name": "taller-monitor-receptor", "version": "2.0.0"}, "spans": spans}],
    }]}
