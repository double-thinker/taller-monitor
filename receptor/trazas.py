"""Builds OpenInference traces for Phoenix from Claude Code hook events.

The client (plugin or HTTP hook) forwards each event as is. This module keeps per-session state and
emits spans following the same model as Arize's own Claude Code tracing plugin:

- One trace per turn. Its root span `Turn` (AGENT) carries the prompt (`input.value`), the final
  answer (`output.value`) and the token totals. Phoenix's Sessions view reads the conversation from
  these root spans.
- `LLM call: <model>` (LLM): every model call, with tokens and text.
- Tools (TOOL), named after the tool, with input, output, `tool.call.id` and `error.message`.
- `Subagent: <type>` (AGENT): a subagent, with its own LLM calls and tools as children.

Live view: span ids for the turn and for each subagent are reserved when they start, and their
children are sent as soon as they happen. The parent span itself is emitted when it ends, with the
right start time and its final input and output. Until then Phoenix shows the trace with its first
child as a provisional root; when the real root arrives it takes over (checked against Phoenix 21).
A long-running tool shows up from the start through a `Tool started: <name>` marker span.

Events may arrive late or out of order: each one carries its own time (`ts`).
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path

MAX_TEXT = 30_000
GRACE = 90  # seconds before a tool that started but never finished is reported
S = 1_000_000_000
GRACIA = GRACE  # backwards-compatible name


def _id(n: int) -> str:
    return secrets.token_hex(n)


def _text(v) -> str:
    if v is None:
        return ""
    if not isinstance(v, str):
        try:
            v = json.dumps(v, ensure_ascii=False)
        except Exception:
            v = str(v)
    return v if len(v) <= MAX_TEXT else v[:MAX_TEXT] + f"\n[... truncated, {len(v)} characters]"


def _attribute(key: str, value) -> dict:
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": _text(value)}}


def _ts_iso(ts: str, default: int) -> int:
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * S)
    except Exception:
        return default


def folder(cwd: str) -> str:
    return os.path.basename((cwd or "").replace("\\", "/").rstrip("/"))


class Session:
    """State of one Claude Code session. Only the receiver touches it, under its lock."""

    FIELDS = ("turn", "last", "tools", "done", "agents", "briefs", "llm_seen", "llm_end", "meta")

    def __init__(self, d: dict | None = None):
        d = d or {}
        self.turn: dict | None = d.get("turn")
        self.last: dict | None = d.get("last")
        self.tools: dict = d.get("tools", {})        # tool_use_id -> started tool
        self.done: list = d.get("done", [])          # finished tool_use_ids
        self.agents: dict = d.get("agents", {})      # agent_id -> running subagent
        self.briefs: list = d.get("briefs", [])      # prompts of Agent tool calls not yet matched
        self.llm_seen: list = d.get("llm_seen", [])
        self.llm_end: dict = d.get("llm_end", {})    # parent span -> end of its last LLM call
        self.meta: dict = d.get("meta", {})

    def to_dict(self) -> dict:
        self.done = self.done[-1000:]
        self.llm_seen = self.llm_seen[-2000:]
        return {f: getattr(self, f) for f in self.FIELDS}


class Reconstructor:
    def __init__(self, state_dir: str | Path):
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.sessions: dict[str, Session] = {}
        self.lock = threading.Lock()
        self._out: list = []

    # ---------------------------------------------------------------- state

    def _path(self, sid: str) -> Path:
        return self.dir / (("".join(c for c in sid if c.isalnum() or c in "-_") or "no-session") + ".json")

    def _session(self, sid: str) -> Session:
        if sid not in self.sessions:
            try:
                self.sessions[sid] = Session(json.loads(self._path(sid).read_text("utf-8")))
            except Exception:
                self.sessions[sid] = Session()
        return self.sessions[sid]

    def _save(self, sid: str) -> None:
        path = self._path(sid)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.sessions[sid].to_dict(), ensure_ascii=False), "utf-8")
        os.replace(tmp, path)

    # ---------------------------------------------------------------- spans

    def _span(self, s: Session, sid, *, trace, span, parent, name, kind, start, end, attrs, error=None) -> dict:
        base = {"openinference.span.kind": kind, "session.id": sid, "user.id": s.meta.get("student", "")}
        for k in ("workshop.cwd", "workshop.folder", "workshop.os", "workshop.client"):
            if s.meta.get(k):
                base[k] = s.meta[k]
        base.update({k: v for k, v in attrs.items() if v not in (None, "")})
        if error:
            base["error.message"] = _text(error)
        out = {
            "traceId": trace, "spanId": span, "name": name, "kind": 1,
            "startTimeUnixNano": str(int(start)), "endTimeUnixNano": str(int(max(end, start))),
            "attributes": [_attribute(k, v) for k, v in base.items()],
            "status": {"code": 2, "message": _text(error)[:500]} if error else {"code": 1},
        }
        if parent:
            out["parentSpanId"] = parent
        return out

    def _emit(self, span: dict) -> None:
        self._out.append(span)

    def _open_turn(self, s: Session, ts: int, prompt: str = "") -> dict:
        """Reserves the ids of a new turn. Its root span is emitted when it closes."""
        s.turn = {"trace": _id(16), "span": _id(8), "start": ts, "prompt": prompt, "tools": 0, "errors": 0,
                  "in": 0, "out": 0, "cache_read": 0, "cache_write": 0, "model": ""}
        return s.turn

    def _turn_or_last(self, s: Session, ts: int) -> dict:
        return s.turn or s.last or self._open_turn(s, ts)

    def _agent(self, s: Session, ev: dict, ts: int) -> dict:
        """The subagent an event belongs to. Its span is emitted when it stops."""
        aid = ev["agent_id"]
        if aid not in s.agents:
            t = self._turn_or_last(s, ts)
            brief = s.briefs.pop(0) if s.briefs else {}
            s.agents[aid] = {"trace": t["trace"], "span": _id(8), "parent": t["span"], "start": ts,
                             "type": ev.get("agent_type") or brief.get("type", ""), "brief": brief.get("prompt", ""),
                             "in": 0, "out": 0, "cache_read": 0, "cache_write": 0, "model": ""}
        return s.agents[aid]

    def _parent(self, s: Session, ev: dict, ts: int) -> dict:
        return self._agent(s, ev, ts) if ev.get("agent_id") else self._turn_or_last(s, ts)

    def _llm_calls(self, s: Session, sid, parent: dict, calls, ts: int) -> None:
        """Emits model calls not seen before, as children of `parent` (turn or subagent)."""
        for c in calls or []:
            cid = c.get("id") or ""
            if cid and cid in s.llm_seen:
                continue
            if cid:
                s.llm_seen.append(cid)
            end = min(max(_ts_iso(c.get("ts", ""), ts), parent["start"]), ts)
            start = min(max(s.llm_end.get(parent["span"], parent["start"]), parent["start"]), end)
            s.llm_end[parent["span"]] = end
            tin, tout = int(c.get("entrada") or 0), int(c.get("salida") or 0)
            cr, cw = int(c.get("cache_lectura") or 0), int(c.get("cache_escritura") or 0)
            for k, v in (("in", tin), ("out", tout), ("cache_read", cr), ("cache_write", cw)):
                parent[k] = parent.get(k, 0) + v
            model = c.get("modelo") or ""
            parent["model"] = model or parent.get("model", "")
            text = c.get("texto") or ("tool calls: " + ", ".join(c.get("herramientas") or []))
            attrs = {"llm.model_name": model, "llm.provider": "anthropic", "llm.system": "anthropic",
                     "llm.token_count.prompt": tin, "llm.token_count.completion": tout,
                     "llm.token_count.total": tin + tout, "output.value": text, "llm.message.id": cid}
            if cr:
                attrs["llm.token_count.prompt_details.cache_read"] = cr
            if cw:
                attrs["llm.token_count.prompt_details.cache_write"] = cw
            self._emit(self._span(s, sid, trace=parent["trace"], span=_id(8), parent=parent["span"],
                                  name=f"LLM call: {model}" if model else "LLM call", kind="LLM",
                                  start=start, end=end, attrs=attrs))

    @staticmethod
    def _totals(p: dict) -> dict:
        attrs = {"llm.model_name": p.get("model"), "llm.token_count.prompt": p.get("in", 0),
                 "llm.token_count.completion": p.get("out", 0),
                 "llm.token_count.total": p.get("in", 0) + p.get("out", 0)}
        if p.get("cache_read"):
            attrs["llm.token_count.prompt_details.cache_read"] = p["cache_read"]
        if p.get("cache_write"):
            attrs["llm.token_count.prompt_details.cache_write"] = p["cache_write"]
        return attrs

    def _close_turn(self, s: Session, sid, ts: int, ev: dict, calls, reason: str, error=None) -> None:
        t = s.turn
        previous = s.last
        s.turn = None
        # Calls of an earlier turn that closed without Stop (Ctrl+C, -p mode): assigned by time.
        margin = 2 * S
        own, earlier = [], []
        for c in calls or []:
            h = _ts_iso(c.get("ts", ""), ts)
            is_earlier = previous and previous["start"] - margin <= h < t["start"] - margin
            (earlier if is_earlier else own).append(c)
        if earlier:
            self._llm_calls(s, sid, previous, earlier, t["start"])
        self._llm_calls(s, sid, t, own, ts)
        s.last = {k: t[k] for k in ("trace", "span", "start", "prompt")}
        answer = ev.get("last_assistant_message") or next((c.get("texto") for c in reversed(own) if c.get("texto")), "")
        prompt = t.get("prompt", "")
        notification = prompt.lstrip().startswith(("<task-notification", "<agent-message"))
        attrs = {"input.value": prompt, "output.value": answer, "llm.model_name": t.get("model"),
                 "workshop.close_reason": reason, "workshop.tool_count": t.get("tools", 0),
                 "workshop.tool_errors": t.get("errors", 0), "workshop.subagent_notification": notification}
        self._emit(self._span(s, sid, trace=t["trace"], span=t["span"], parent=None,
                              name="Turn (subagent notification)" if notification else "Turn", kind="AGENT",
                              start=t["start"], end=ts, error=error, attrs=attrs))

    def _no_result(self, s: Session, sid, tu: str, h: dict, reason: str, ts: int) -> None:
        s.done.append(tu)
        self._emit(self._span(s, sid, trace=h["trace"], span=_id(8), parent=h["parent"], name=h["name"],
                              kind="TOOL", start=h["start"], end=max(ts, h["start"]), error=reason,
                              attrs={"tool.name": h["name"], "tool.call.id": tu, "input.value": h.get("input"),
                                     "input.mime_type": "application/json", "subagent.type": h.get("agent_type"),
                                     "workshop.no_result": True}))

    # ---------------------------------------------------------------- entry point

    def procesar(self, msg: dict, alumno_por_cwd=None) -> tuple[str, list[dict]]:
        """Processes one client event. Returns the Phoenix project and the spans to emit."""
        ev = msg.get("evento") or msg  # an HTTP hook sends the bare event
        sid = ev.get("session_id") or "no-session"
        ts = int(msg.get("ts") or time.time_ns())
        with self.lock:
            self._out = []
            s = self._session(sid)
            student = msg.get("alumno") or (alumno_por_cwd(ev.get("cwd")) if alumno_por_cwd else None)
            client = msg.get("cliente") or {}
            s.meta.update({k: v for k, v in {
                "student": student or s.meta.get("student") or "unnamed-" + sid[:8],
                "project": msg.get("proyecto") or s.meta.get("project") or "taller",
                "workshop.cwd": ev.get("cwd"), "workshop.folder": folder(ev.get("cwd")),
                "workshop.os": client.get("so"),
                "workshop.client": client.get("version") or ("http-hooks" if "evento" not in msg else None),
            }.items() if v})
            self._event(s, sid, ev, ts, msg)
            self._expired(s, sid, ts)
            self._save(sid)
            return s.meta.get("project", "taller"), self._out

    def _event(self, s: Session, sid, ev: dict, ts: int, msg: dict) -> None:
        n = ev.get("hook_event_name", "")
        if n == "SessionStart":
            s.meta["workshop.session_source"] = ev.get("source", "")
        elif n == "UserPromptSubmit":
            # The client sends the previous turn's model calls again: those Stop did not see yet.
            if s.turn:
                self._close_turn(s, sid, ts, {}, msg.get("llamadas"), "interrupted")
            elif s.last and msg.get("llamadas"):
                self._llm_calls(s, sid, s.last, msg["llamadas"], ts)
            self._open_turn(s, ts, ev.get("prompt", ""))
        elif n == "PreToolUse":
            tu = ev.get("tool_use_id") or _id(8)
            if ev.get("tool_name") == "Agent" and not ev.get("agent_id"):
                ti = ev.get("tool_input") or {}
                s.briefs.append({"prompt": _text(ti.get("prompt") or ti), "type": ti.get("subagent_type", "")})
            if tu not in s.done:
                p = self._parent(s, ev, ts)
                h = {"start": ts, "name": ev.get("tool_name", "tool"), "trace": p["trace"], "parent": p["span"],
                     "input": _text(ev.get("tool_input")), "agent_type": ev.get("agent_type", "")}
                s.tools[tu] = h
                self._emit(self._span(s, sid, trace=h["trace"], span=_id(8), parent=h["parent"],
                                      name=f"Tool started: {h['name']}", kind="CHAIN", start=ts, end=ts, attrs={
                                          "tool.name": h["name"], "tool.call.id": tu, "input.value": h["input"],
                                          "input.mime_type": "application/json", "subagent.type": h["agent_type"]}))
        elif n in ("PostToolUse", "PostToolUseFailure"):
            p = self._parent(s, ev, ts)
            self._llm_calls(s, sid, p, msg.get("llamadas"), ts)
            tu = ev.get("tool_use_id", "")
            h = s.tools.pop(tu, None) or {"start": ts - int(ev.get("duration_ms") or 0) * 1_000_000,
                                          "trace": p["trace"], "parent": p["span"]}
            error = (ev.get("error") or "failed") if n == "PostToolUseFailure" else None
            t = s.turn or s.last
            if t is not None:
                t["tools"] = t.get("tools", 0) + 1
                t["errors"] = t.get("errors", 0) + (1 if error else 0)
            if tu:
                s.done.append(tu)
            tool = ev.get("tool_name", "tool")
            kind = "TOOL"
            attrs = {"tool.name": tool, "tool.call.id": tu, "input.value": _text(ev.get("tool_input")),
                     "input.mime_type": "application/json", "subagent.type": ev.get("agent_type"),
                     "subagent.id": ev.get("agent_id"), "workshop.interrupted": bool(ev.get("is_interrupt"))}
            if error is None:
                attrs["output.value"] = _text(ev.get("tool_response"))
            self._emit(self._span(s, sid, trace=h["trace"], span=_id(8), parent=h["parent"], name=tool, kind=kind,
                                  start=h["start"], end=ts, error=error, attrs=attrs))
        elif n == "PermissionDenied":
            tu = ev.get("tool_use_id") or _id(8)
            h = s.tools.pop(tu, None)
            if h is None:
                p = self._parent(s, ev, ts)
                h = {"start": ts, "name": ev.get("tool_name", "tool"), "trace": p["trace"], "parent": p["span"],
                     "input": _text(ev.get("tool_input")), "agent_type": ev.get("agent_type", "")}
            self._no_result(s, sid, tu, h, "permission denied: " + _text(
                ev.get("reason") or ev.get("message") or "denied"), ts)
        elif n == "SubagentStart":
            if ev.get("agent_id"):
                self._agent(s, ev, ts)
        elif n == "SubagentStop":
            ev = {**ev, "agent_id": ev.get("agent_id") or _id(8)}
            ag = self._agent(s, ev, ts)
            self._llm_calls(s, sid, ag, msg.get("llamadas"), ts)
            s.agents.pop(ev["agent_id"], None)
            answer = ev.get("last_assistant_message") or next(
                (c.get("texto") for c in reversed(msg.get("llamadas") or []) if c.get("texto")), "")
            kind_name = ag.get("type") or ev.get("agent_type") or "unknown"
            self._emit(self._span(s, sid, trace=ag["trace"], span=ag["span"], parent=ag["parent"],
                                  name=f"Subagent: {kind_name}", kind="AGENT", start=ag["start"], end=ts, attrs={
                                      "input.value": ag.get("brief"), "output.value": answer,
                                      "llm.model_name": ag.get("model"),
                                      "subagent.id": ev["agent_id"], "subagent.type": kind_name}))
        elif n == "PreCompact":
            t = self._turn_or_last(s, ts)
            self._emit(self._span(s, sid, trace=t["trace"], span=_id(8), parent=t["span"], name="Context compaction",
                                  kind="CHAIN", start=ts, end=ts, attrs={"input.value": ev.get("trigger")}))
        elif n in ("Stop", "StopFailure"):
            if not s.turn:
                self._open_turn(s, ts)
            error = _text(ev.get("error") or ev.get("reason") or "API error") if n == "StopFailure" else None
            self._close_turn(s, sid, ts, ev, msg.get("llamadas"), "error" if error else "stop", error)
        elif n == "SessionEnd":
            if s.turn:
                self._close_turn(s, sid, ts, ev, msg.get("llamadas"), "session_end")
            elif s.last and msg.get("llamadas"):
                self._llm_calls(s, sid, s.last, msg["llamadas"], ts)

    def _expired(self, s: Session, sid, ts: int) -> None:
        for tu, h in list(s.tools.items()):
            if ts - h["start"] > GRACE * S:
                s.tools.pop(tu)
                self._no_result(s, sid, tu, h, "no result: denied or interrupted", ts)

    def barrer(self) -> list[tuple[str, list[dict]]]:
        """Checks every session in memory (called by a receiver thread every few seconds)."""
        result = []
        ts = time.time_ns()
        with self.lock:
            for sid, s in self.sessions.items():
                self._out = []
                self._expired(s, sid, ts)
                if self._out:
                    self._save(sid)
                    result.append((s.meta.get("project", "taller"), self._out))
            self._out = []
        return result


def lote(project: str, spans: list[dict]) -> dict:
    return {"resourceSpans": [{
        "resource": {"attributes": [_attribute("openinference.project.name", project),
                                    _attribute("service.name", "claude-code")]},
        "scopeSpans": [{"scope": {"name": "taller-monitor-receiver", "version": "3.0.0"}, "spans": spans}],
    }]}
