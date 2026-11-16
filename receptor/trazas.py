"""Reconstrucción de trazas OpenInference a partir de los eventos de hooks de Claude Code.

El cliente (plugin o hook HTTP) manda cada evento tal cual. Aquí se guarda el estado de cada sesión y
se emiten spans en cuanto se sabe algo, para que el profesor lo vea en vivo aunque un turno o un
subagente dure horas. Phoenix no actualiza un span ya recibido, así que:

- turno (AGENT, raíz): se emite al empezar, con el prompt. Al terminar se añade el hijo
  `fin del turno` con la respuesta, los tokens y la duración.
- subagente (AGENT): se emite al empezar, con el encargo. Al terminar, hijo `fin del subagente`.
- modelo (LLM): cada llamada al modelo según llega (el plugin las lee del transcript en cada
  herramienta y al terminar). Se descartan las repetidas por su id.
- `empieza <herramienta>` (CHAIN): en PreToolUse, para ver las herramientas largas desde que empiezan.
- herramienta (TOOL): PostToolUse o PostToolUseFailure, con entrada, salida y duración.
- herramienta sin resultado: PreToolUse sin PostToolUse pasados GRACIA segundos, o PermissionDenied.

Los eventos pueden llegar desordenados o tarde: cada uno lleva su hora (`ts`) y su padre se decide
con lo que se sabía cuando empezó.
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
GRACIA = 90  # segundos antes de dar por perdida una herramienta sin resultado
S = 1_000_000_000


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
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * S)
    except Exception:
        return defecto


def carpeta(cwd: str) -> str:
    return os.path.basename((cwd or "").replace("\\", "/").rstrip("/"))


class Sesion:
    """Estado de una sesión de Claude Code. Solo lo toca el receptor, bajo su cerrojo."""

    CAMPOS = ("turno", "ultimo", "herr", "hechas", "agentes", "encargos", "llm_vistos", "llm_fin", "meta")

    def __init__(self, d: dict | None = None):
        d = d or {}
        self.turno: dict | None = d.get("turno")
        self.ultimo: dict | None = d.get("ultimo")
        self.herr: dict = d.get("herr", {})          # tool_use_id -> herramienta empezada
        self.hechas: list = d.get("hechas", [])      # tool_use_id cerrados
        self.agentes: dict = d.get("agentes", {})    # agent_id -> subagente en curso
        self.encargos: list = d.get("encargos", [])  # prompts de herramientas Agent aún sin subagente
        self.llm_vistos: list = d.get("llm_vistos", [])
        self.llm_fin: dict = d.get("llm_fin", {})    # span padre -> fin de la última llamada al modelo
        self.meta: dict = d.get("meta", {})

    def a_dict(self) -> dict:
        self.hechas = self.hechas[-1000:]
        self.llm_vistos = self.llm_vistos[-2000:]
        return {c: getattr(self, c) for c in self.CAMPOS}


class Reconstructor:
    def __init__(self, dir_estado: str | Path):
        self.dir = Path(dir_estado)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.sesiones: dict[str, Sesion] = {}
        self.cerrojo = threading.Lock()
        self._salida: list = []

    # ---------------------------------------------------------------- estado

    def _ruta(self, sid: str) -> Path:
        return self.dir / (("".join(c for c in sid if c.isalnum() or c in "-_") or "sin-sesion") + ".json")

    def _sesion(self, sid: str) -> Sesion:
        if sid not in self.sesiones:
            try:
                self.sesiones[sid] = Sesion(json.loads(self._ruta(sid).read_text("utf-8")))
            except Exception:
                self.sesiones[sid] = Sesion()
        return self.sesiones[sid]

    def _guardar(self, sid: str) -> None:
        ruta = self._ruta(sid)
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

    def _emitir(self, span: dict) -> None:
        self._salida.append(span)

    def _abrir_turno(self, s: Sesion, sid, ts: int, prompt: str = "") -> dict:
        nombre = "turno"
        if prompt.lstrip().startswith("<task-notification"):
            nombre = "aviso de subagente"
        elif not prompt:
            nombre = "turno sin prompt"
        s.turno = {"trace": _id(16), "span": _id(8), "inicio": ts, "prompt": prompt, "n_tools": 0, "errores": 0,
                   "ent": 0, "sal": 0, "modelo": ""}
        self._emitir(self._span(s, sid, trace=s.turno["trace"], span=s.turno["span"], padre=None, nombre=nombre,
                                tipo="AGENT", inicio=ts, fin=ts, attrs={"input.value": prompt}))
        return s.turno

    def _turno_o_ultimo(self, s: Sesion, sid, ts: int) -> dict:
        return s.turno or s.ultimo or self._abrir_turno(s, sid, ts)

    def _agente(self, s: Sesion, sid, ev: dict, ts: int) -> dict:
        """El subagente de un evento. Si es nuevo, se emite su span al momento, colgando del turno."""
        aid = ev["agent_id"]
        if aid not in s.agentes:
            t = self._turno_o_ultimo(s, sid, ts)
            encargo = s.encargos.pop(0) if s.encargos else {}
            tipo = ev.get("agent_type") or encargo.get("tipo", "")
            ag = {"trace": t["trace"], "span": _id(8), "padre": t["span"], "inicio": ts, "tipo": tipo,
                  "ent": 0, "sal": 0, "modelo": ""}
            s.agentes[aid] = ag
            self._emitir(self._span(s, sid, trace=ag["trace"], span=ag["span"], padre=ag["padre"],
                                    nombre=f"subagente {tipo}".strip(), tipo="AGENT", inicio=ts, fin=ts,
                                    attrs={"input.value": encargo.get("prompt"), "taller.agente": tipo,
                                           "taller.agent_id": aid}))
        return s.agentes[aid]

    def _padre(self, s: Sesion, sid, ev: dict, ts: int) -> dict:
        return self._agente(s, sid, ev, ts) if ev.get("agent_id") else self._turno_o_ultimo(s, sid, ts)

    def _llamadas(self, s: Sesion, sid, padre: dict, llamadas, ts: int) -> None:
        """Emite las llamadas al modelo que aún no se habían visto, colgando de `padre` (turno o subagente)."""
        for ll in llamadas or []:
            lid = ll.get("id") or ""
            if lid and lid in s.llm_vistos:
                continue
            if lid:
                s.llm_vistos.append(lid)
            fin = min(max(_ts_iso(ll.get("ts", ""), ts), padre["inicio"]), ts)
            inicio = min(max(s.llm_fin.get(padre["span"], padre["inicio"]), padre["inicio"]), fin)
            s.llm_fin[padre["span"]] = fin
            ent, sal = int(ll.get("entrada") or 0), int(ll.get("salida") or 0)
            padre["ent"] = padre.get("ent", 0) + ent
            padre["sal"] = padre.get("sal", 0) + sal
            padre["modelo"] = ll.get("modelo") or padre.get("modelo", "")
            texto = ll.get("texto") or ("herramientas: " + ", ".join(ll.get("herramientas") or []))
            self._emitir(self._span(s, sid, trace=padre["trace"], span=_id(8), padre=padre["span"], nombre="modelo",
                                    tipo="LLM", inicio=inicio, fin=fin, attrs={
                                        "llm.model_name": ll.get("modelo"), "llm.provider": "anthropic",
                                        "llm.token_count.prompt": ent, "llm.token_count.completion": sal,
                                        "llm.token_count.total": ent + sal, "output.value": texto}))

    def _cerrar_turno(self, s: Sesion, sid, ts: int, ev: dict, llamadas, cierre: str, error=None) -> None:
        t = s.turno
        anterior = s.ultimo
        s.turno = None
        # Llamadas de un turno anterior que se cerró sin Stop (Ctrl+C, modo -p): se reparten por hora.
        margen = 2 * S
        propias, previas = [], []
        for ll in llamadas or []:
            h = _ts_iso(ll.get("ts", ""), ts)
            es_previa = anterior and anterior["inicio"] - margen <= h < t["inicio"] - margen
            (previas if es_previa else propias).append(ll)
        if previas:
            self._llamadas(s, sid, anterior, previas, t["inicio"])
        self._llamadas(s, sid, t, propias, ts)
        s.ultimo = {k: t[k] for k in ("trace", "span", "inicio", "prompt", "ent", "sal", "modelo")}
        respuesta = ev.get("last_assistant_message") or next(
            (ll.get("texto") for ll in reversed(propias) if ll.get("texto")), "")
        if cierre == "interrumpido":
            respuesta = (respuesta + "\n\n" if respuesta else "") + "[turno interrumpido]"
        self._emitir(self._span(s, sid, trace=t["trace"], span=_id(8), padre=t["span"], nombre="fin del turno",
                                tipo="CHAIN", inicio=t["inicio"], fin=ts, error=error, attrs={
                                    "input.value": t.get("prompt"), "output.value": respuesta,
                                    "llm.model_name": t.get("modelo"), "llm.token_count.prompt": t.get("ent", 0),
                                    "llm.token_count.completion": t.get("sal", 0),
                                    "llm.token_count.total": t.get("ent", 0) + t.get("sal", 0),
                                    "taller.herramientas": t.get("n_tools", 0), "taller.errores": t.get("errores", 0),
                                    "taller.cierre": cierre, "taller.duracion_s": round((ts - t["inicio"]) / S, 1)}))

    def _sin_resultado(self, s: Sesion, sid, tu: str, h: dict, motivo: str, ts: int) -> None:
        s.hechas.append(tu)
        self._emitir(self._span(s, sid, trace=h["trace"], span=_id(8), padre=h["padre"], nombre=h["nombre"],
                                tipo="TOOL", inicio=h["inicio"], fin=max(ts, h["inicio"]), error=motivo,
                                attrs={"tool.name": h["nombre"], "input.value": h.get("entrada"),
                                       "input.mime_type": "application/json", "output.value": motivo,
                                       "taller.agente": h.get("agente", ""), "taller.sin_resultado": True}))

    # ---------------------------------------------------------------- entrada

    def procesar(self, msg: dict, alumno_por_cwd=None) -> tuple[str, list[dict]]:
        """Procesa un evento del cliente. Devuelve el proyecto de Phoenix y los spans que hay que emitir."""
        ev = msg.get("evento") or msg  # un hook HTTP manda el evento sin envoltorio
        sid = ev.get("session_id") or "sin-sesion"
        ts = int(msg.get("ts") or time.time_ns())
        with self.cerrojo:
            self._salida = []
            s = self._sesion(sid)
            alumno = msg.get("alumno") or (alumno_por_cwd(ev.get("cwd")) if alumno_por_cwd else None)
            cliente = msg.get("cliente") or {}
            s.meta.update({k: v for k, v in {
                "alumno": alumno or s.meta.get("alumno") or "sin-nombre-" + sid[:8],
                "proyecto": msg.get("proyecto") or s.meta.get("proyecto") or "taller",
                "taller.cwd": ev.get("cwd"), "taller.proyecto_local": carpeta(ev.get("cwd")),
                "taller.so": cliente.get("so"),
                "taller.cliente": cliente.get("version") or ("hook-http" if "evento" not in msg else None),
            }.items() if v})
            self._evento(s, sid, ev, ts, msg)
            self._caducadas(s, sid, ts)
            self._guardar(sid)
            return s.meta.get("proyecto", "taller"), self._salida

    def _evento(self, s: Sesion, sid, ev: dict, ts: int, msg: dict) -> None:
        n = ev.get("hook_event_name", "")
        if n == "SessionStart":
            self._emitir(self._span(s, sid, trace=_id(16), span=_id(8), padre=None, nombre="inicio de sesión",
                                    tipo="CHAIN", inicio=ts, fin=ts,
                                    attrs={"input.value": ev.get("source"), "llm.model_name": ev.get("model")}))
        elif n == "UserPromptSubmit":
            if s.turno:
                self._cerrar_turno(s, sid, ts, {}, [], "interrumpido")
            self._abrir_turno(s, sid, ts, ev.get("prompt", ""))
        elif n == "PreToolUse":
            tu = ev.get("tool_use_id") or _id(8)
            if ev.get("tool_name") == "Agent" and not ev.get("agent_id"):
                ti = ev.get("tool_input") or {}
                s.encargos.append({"prompt": _texto(ti.get("prompt") or ti), "tipo": ti.get("subagent_type", "")})
            if tu not in s.hechas:
                p = self._padre(s, sid, ev, ts)
                s.herr[tu] = {"inicio": ts, "nombre": ev.get("tool_name", "herramienta"), "trace": p["trace"],
                              "padre": p["span"], "entrada": _texto(ev.get("tool_input")),
                              "agente": ev.get("agent_type", "")}
                # Marca de inicio: una herramienta larga (un Bash de una hora) se ve desde que empieza.
                # Phoenix no actualiza spans, así que el resultado llegará después en otro span.
                h = s.herr[tu]
                self._emitir(self._span(s, sid, trace=h["trace"], span=_id(8), padre=h["padre"],
                                        nombre=f"empieza {h['nombre']}", tipo="CHAIN", inicio=ts, fin=ts, attrs={
                                            "tool.name": h["nombre"], "input.value": h["entrada"],
                                            "input.mime_type": "application/json", "taller.agente": h["agente"],
                                            "taller.tool_use_id": tu}))
        elif n in ("PostToolUse", "PostToolUseFailure"):
            p = self._padre(s, sid, ev, ts)
            self._llamadas(s, sid, p, msg.get("llamadas"), ts)
            tu = ev.get("tool_use_id", "")
            h = s.herr.pop(tu, None) or {"inicio": ts - int(ev.get("duration_ms") or 0) * 1_000_000,
                                          "trace": p["trace"], "padre": p["span"]}
            error = (ev.get("error") or "fallo") if n == "PostToolUseFailure" else None
            t = s.turno or s.ultimo
            if t is not None:
                t["n_tools"] = t.get("n_tools", 0) + 1
                t["errores"] = t.get("errores", 0) + (1 if error else 0)
            if tu:
                s.hechas.append(tu)
            herramienta = ev.get("tool_name", "herramienta")
            self._emitir(self._span(s, sid, trace=h["trace"], span=_id(8), padre=h["padre"], nombre=herramienta,
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
                p = self._padre(s, sid, ev, ts)
                h = {"inicio": ts, "nombre": ev.get("tool_name", "herramienta"), "trace": p["trace"],
                     "padre": p["span"], "entrada": _texto(ev.get("tool_input")), "agente": ev.get("agent_type", "")}
            self._sin_resultado(s, sid, tu, h, "denegada: " + _texto(
                ev.get("reason") or ev.get("message") or "permiso denegado"), ts)
        elif n == "SubagentStart":
            if ev.get("agent_id"):
                self._agente(s, sid, ev, ts)
        elif n == "SubagentStop":
            ev = {**ev, "agent_id": ev.get("agent_id") or _id(8)}
            ag = self._agente(s, sid, ev, ts)
            self._llamadas(s, sid, ag, msg.get("llamadas"), ts)
            s.agentes.pop(ev["agent_id"], None)
            respuesta = ev.get("last_assistant_message") or next(
                (ll.get("texto") for ll in reversed(msg.get("llamadas") or []) if ll.get("texto")), "")
            self._emitir(self._span(s, sid, trace=ag["trace"], span=_id(8), padre=ag["span"], nombre="fin del subagente",
                                    tipo="CHAIN", inicio=ag["inicio"], fin=ts, attrs={
                                        "output.value": respuesta, "llm.model_name": ag.get("modelo"),
                                        "llm.token_count.prompt": ag.get("ent", 0),
                                        "llm.token_count.completion": ag.get("sal", 0),
                                        "llm.token_count.total": ag.get("ent", 0) + ag.get("sal", 0),
                                        "taller.agente": ag.get("tipo"),
                                        "taller.duracion_s": round((ts - ag["inicio"]) / S, 1)}))
        elif n == "PreCompact":
            self._emitir(self._span(s, sid, trace=_id(16), span=_id(8), padre=None, nombre="compactación de contexto",
                                    tipo="CHAIN", inicio=ts, fin=ts, attrs={"input.value": ev.get("trigger")}))
        elif n in ("Stop", "StopFailure"):
            if not s.turno:
                self._abrir_turno(s, sid, ts)
            error = _texto(ev.get("error") or ev.get("reason") or "error de la API") if n == "StopFailure" else None
            self._cerrar_turno(s, sid, ts, ev, msg.get("llamadas"), "error" if error else "normal", error)
        elif n == "SessionEnd":
            if s.turno:
                self._cerrar_turno(s, sid, ts, ev, msg.get("llamadas"), "fin de sesión")
            self._emitir(self._span(s, sid, trace=_id(16), span=_id(8), padre=None, nombre="fin de sesión",
                                    tipo="CHAIN", inicio=ts, fin=ts, attrs={"input.value": ev.get("reason")}))

    def _caducadas(self, s: Sesion, sid, ts: int) -> None:
        for tu, h in list(s.herr.items()):
            if ts - h["inicio"] > GRACIA * S:
                s.herr.pop(tu)
                self._sin_resultado(s, sid, tu, h, "sin resultado: denegada o interrumpida", ts)

    def barrer(self) -> list[tuple[str, list[dict]]]:
        """Revisa todas las sesiones en memoria (lo llama un hilo del receptor cada pocos segundos)."""
        salida = []
        ts = time.time_ns()
        with self.cerrojo:
            for sid, s in self.sesiones.items():
                self._salida = []
                self._caducadas(s, sid, ts)
                if self._salida:
                    self._guardar(sid)
                    salida.append((s.meta.get("proyecto", "taller"), self._salida))
            self._salida = []
        return salida


def lote(proyecto: str, spans: list[dict]) -> dict:
    return {"resourceSpans": [{
        "resource": {"attributes": [_atributo("openinference.project.name", proyecto),
                                    _atributo("service.name", "claude-code")]},
        "scopeSpans": [{"scope": {"name": "taller-monitor-receptor", "version": "2.1.0"}, "spans": spans}],
    }]}
