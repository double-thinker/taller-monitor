"""Tests del cliente (hook) y del receptor (reconstrucción de trazas).

    python3 -m unittest discover -s tests -v                     # cliente y reconstrucción
    receptor/.venv/bin/python -m unittest discover -s tests -v   # también la conversión a protobuf
"""

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent


def cargar(nombre, ruta):
    spec = importlib.util.spec_from_file_location(nombre, ruta)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


monitor = cargar("monitor", RAIZ / "plugins/taller-monitor/hooks/monitor.py")
trazas = cargar("trazas", RAIZ / "receptor/trazas.py")
S = 1_000_000_000


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = dict(os.environ)
        for k in list(os.environ):
            if k.startswith(("TALLER_MONITOR_", "CLAUDE_PLUGIN_OPTION_", "CLAUDE_PROJECT_DIR")):
                del os.environ[k]
        os.environ["CLAUDE_PLUGIN_DATA"] = self.tmp.name

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)
        self.tmp.cleanup()


# ------------------------------------------------------------------- cliente

class TestConfig(Base):
    def test_sin_url_no_hace_nada(self):
        os.environ["CLAUDE_PROJECT_DIR"] = self.tmp.name
        self.assertIsNone(monitor.config())

    def test_fichero_del_repositorio(self):
        repo = Path(self.tmp.name) / "repo"
        (repo / ".claude").mkdir(parents=True)
        (repo / ".claude" / "taller-monitor.json").write_text(
            json.dumps({"url": "https://m.example/", "token": "k", "proyecto": "p"}), encoding="utf-8")
        os.environ.update(CLAUDE_PROJECT_DIR=str(repo), TALLER_MONITOR_ALUMNO="eva")
        c = monitor.config()
        self.assertEqual(c["url"], "https://m.example/v1/events")
        self.assertEqual((c["alumno"], c["proyecto"]), ("eva", "p"))

    def test_variable_gana_a_opcion(self):
        os.environ.update(CLAUDE_PLUGIN_OPTION_URL="https://a", CLAUDE_PLUGIN_OPTION_TOKEN="k",
                          TALLER_MONITOR_URL="https://b")
        self.assertEqual(monitor.config()["url"], "https://b/v1/events")

    def test_off(self):
        os.environ.update(TALLER_MONITOR_URL="https://b", TALLER_MONITOR_TOKEN="k", TALLER_MONITOR_OFF="1")
        self.assertIsNone(monitor.config())


class TestTranscript(Base):
    def escribir(self, lineas):
        p = Path(self.tmp.name) / "t.jsonl"
        p.write_text("\n".join(json.dumps(l, ensure_ascii=False) for l in lineas) + "\n", encoding="utf-8")
        return str(p)

    def asistente(self, id_, texto, entrada=10, salida=5):
        return {"type": "assistant", "timestamp": "2026-11-16T10:00:00Z",
                "message": {"id": id_, "model": "m", "usage": {"input_tokens": entrada, "output_tokens": salida},
                            "content": [{"type": "text", "text": texto}]}}

    def test_solo_las_llamadas_del_ultimo_turno(self):
        t = self.escribir([
            {"type": "user", "message": {"content": "primero"}},
            self.asistente("a", "viejo"),
            {"type": "user", "message": {"content": "segundo ¿ñ?"}},
            self.asistente("b", "uno"),
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x"}]}},
            self.asistente("c", "dos"),
            self.asistente("c", "dos-bis"),  # streaming: mismo id
        ])
        ll = monitor.llamadas_del_turno(t)
        self.assertEqual([x["id"] for x in ll], ["b", "c"])
        self.assertEqual(ll[1]["texto"], "dos\ndos-bis")

    def test_transcript_inexistente(self):
        self.assertEqual(monitor.llamadas_del_turno("/no/existe.jsonl"), [])


class TestCola(Base):
    def test_fallo_de_red_encola_y_se_reintenta(self):
        cfg = {"url": "http://127.0.0.1:9/v1/events", "token": "t", "alumno": "a", "proyecto": "p"}
        cuerpo = json.dumps(monitor.construir({"hook_event_name": "UserPromptSubmit"}, cfg)).encode()
        self.assertFalse(monitor._post(cfg, cuerpo, timeout=1))
        monitor.encolar(cuerpo)
        cola = Path(self.tmp.name) / "cola"
        self.assertEqual(len(list(cola.glob("*.json"))), 1)
        monitor.vaciar_cola(cfg, hasta=time.monotonic() + 3)  # sigue sin red: vuelve a la cola
        self.assertEqual(len(list(cola.glob("*.json"))), 1)
        self.assertEqual(len(list(cola.glob("*.enviando-*"))), 0)
        self.assertIn("en http://127.0.0.1:9", (Path(self.tmp.name) / "errores.log").read_text("utf-8"))

    def test_recorta_textos_largos(self):
        r = monitor.recortar({"a": ["x" * 100_000]})
        self.assertLess(len(r["a"][0]), monitor.MAX_TEXTO + 100)


# ------------------------------------------------------------------- receptor

class TestReconstruccion(Base):
    def setUp(self):
        super().setUp()
        self.r = trazas.Reconstructor(Path(self.tmp.name) / "sesiones")
        self.t = 1_800_000_000 * S

    def ev(self, nombre, dt=0, llamadas=None, **kw):
        msg = {"ts": self.t + dt * S, "alumno": "ana", "proyecto": "taller",
               "evento": {"hook_event_name": nombre, "session_id": "s1", "cwd": "C:\\proyectos\\x", **kw}}
        if llamadas is not None:
            msg["llamadas"] = llamadas
        return self.r.procesar(msg)[1]

    @staticmethod
    def a(span):
        return {x["key"]: next(iter(x["value"].values())) for x in span["attributes"]}

    def test_turno_con_herramienta_y_llamadas(self):
        self.ev("UserPromptSubmit", 0, prompt="arregla")
        self.ev("PreToolUse", 1, tool_name="Bash", tool_use_id="u1", tool_input={"command": "ls"})
        tool = self.ev("PostToolUse", 2, tool_name="Bash", tool_use_id="u1", tool_response={"stdout": "ok"})
        cierre = self.ev("Stop", 3, last_assistant_message="hecho",
                         llamadas=[{"modelo": "m", "entrada": 10, "salida": 5, "texto": "hecho", "ts": ""}])
        raiz = [s for s in cierre if s["name"] == "turno"][0]
        self.assertEqual(tool[0]["parentSpanId"], raiz["spanId"])
        self.assertEqual(tool[0]["traceId"], raiz["traceId"])
        a = self.a(raiz)
        self.assertEqual((a["input.value"], a["output.value"], a["user.id"]), ("arregla", "hecho", "ana"))
        self.assertEqual(a["llm.token_count.total"], "15")
        self.assertEqual(a["taller.proyecto_local"], "x")
        self.assertEqual(int(tool[0]["endTimeUnixNano"]) - int(tool[0]["startTimeUnixNano"]), 1 * S)

    def test_post_antes_que_pre_usa_duration_ms(self):
        self.ev("UserPromptSubmit", 0, prompt="x")
        tool = self.ev("PostToolUse", 5, tool_name="Read", tool_use_id="u2", duration_ms=2000)[0]
        self.assertEqual(int(tool["endTimeUnixNano"]) - int(tool["startTimeUnixNano"]), 2 * S)
        self.assertEqual(self.ev("PreToolUse", 6, tool_name="Read", tool_use_id="u2"), [])  # tardío: se ignora
        self.assertEqual(self.ev("Stop", 200, llamadas=[]) and [], [])
        self.assertNotIn("u2", self.r.sesiones["s1"].herr)

    def test_subagente_con_sus_herramientas_y_llamadas(self):
        self.ev("UserPromptSubmit", 0, prompt="explora")
        self.ev("SubagentStart", 1, agent_id="ag1", agent_type="Explore")
        t1 = self.ev("PostToolUse", 2, tool_name="Grep", tool_use_id="u3", agent_id="ag1", agent_type="Explore")[0]
        fin = self.ev("SubagentStop", 3, agent_id="ag1", agent_type="Explore", last_assistant_message="informe",
                      llamadas=[{"modelo": "h", "entrada": 7, "salida": 3, "texto": "pienso", "ts": ""}])
        sub = [s for s in fin if s["name"] == "subagente Explore"][0]
        llm = [s for s in fin if s["name"] == "modelo"][0]
        self.assertEqual(t1["parentSpanId"], sub["spanId"])
        self.assertEqual(llm["parentSpanId"], sub["spanId"])
        self.assertEqual(self.a(sub)["output.value"], "informe")
        raiz = [s for s in self.ev("Stop", 4, llamadas=[]) if s["name"] == "turno"][0]
        self.assertEqual(sub["parentSpanId"], raiz["spanId"])

    def test_subagente_en_segundo_plano_tras_stop(self):
        self.ev("UserPromptSubmit", 0, prompt="lanza")
        self.ev("SubagentStart", 1, agent_id="ag2", agent_type="general-purpose")
        raiz = [s for s in self.ev("Stop", 2, llamadas=[]) if s["name"] == "turno"][0]
        t = self.ev("PostToolUse", 5, tool_name="Bash", tool_use_id="u4", agent_id="ag2")[0]
        sub = [s for s in self.ev("SubagentStop", 6, agent_id="ag2") if s["name"].startswith("subagente")][0]
        self.assertEqual(t["parentSpanId"], sub["spanId"])
        self.assertEqual(sub["traceId"], raiz["traceId"])

    def test_herramienta_sin_resultado_caduca(self):
        self.ev("UserPromptSubmit", 0, prompt="x")
        self.ev("PreToolUse", 1, tool_name="Bash", tool_use_id="u5", tool_input={"command": "sleep 30"})
        self.ev("Stop", 2, llamadas=[])
        tarde = self.ev("UserPromptSubmit", 2 + trazas.GRACIA + 1, prompt="otra")
        perdida = [s for s in tarde if s["name"] == "Bash"][0]
        self.assertEqual(perdida["status"]["code"], 2)
        self.assertIn("sin resultado", self.a(perdida)["output.value"])

    def test_permiso_denegado(self):
        self.ev("UserPromptSubmit", 0, prompt="x")
        self.ev("PreToolUse", 1, tool_name="Bash", tool_use_id="u6")
        s = self.ev("PermissionDenied", 2, tool_name="Bash", tool_use_id="u6", reason="clasificador")[0]
        self.assertIn("denegada: clasificador", self.a(s)["output.value"])

    def test_aviso_de_subagente_y_error_de_api(self):
        self.ev("UserPromptSubmit", 0, prompt="<task-notification>hecho</task-notification>")
        self.assertEqual(self.ev("Stop", 1, llamadas=[])[-1]["name"], "aviso de subagente")
        self.ev("UserPromptSubmit", 2, prompt="hola")
        raiz = self.ev("StopFailure", 3, error="overloaded")[-1]
        self.assertEqual(raiz["status"]["code"], 2)

    def test_llamadas_de_un_turno_sin_stop_van_a_su_turno(self):
        def iso(dt):
            from datetime import datetime, timezone
            return datetime.fromtimestamp((self.t + dt * S) / S, timezone.utc).isoformat()
        self.ev("UserPromptSubmit", 0, prompt="primero")
        self.ev("UserPromptSubmit", 10, prompt="<task-notification>x</task-notification>")  # sin Stop antes
        spans = self.ev("Stop", 20, llamadas=[{"modelo": "m", "entrada": 1, "salida": 1, "texto": "del primero", "ts": iso(5)},
                                              {"modelo": "m", "entrada": 1, "salida": 1, "texto": "del segundo", "ts": iso(15)}])
        raiz = spans[-1]
        llm = {self.a(s)["output.value"]: s for s in spans if s["name"] == "modelo"}
        self.assertEqual(llm["del segundo"]["parentSpanId"], raiz["spanId"])
        self.assertNotEqual(llm["del primero"]["traceId"], raiz["traceId"])

    def test_hook_http_sin_envoltorio(self):
        proyecto, spans = self.r.procesar({"hook_event_name": "UserPromptSubmit", "session_id": "s9", "prompt": "hola"})
        self.assertEqual((proyecto, spans), ("taller", []))

    def test_estado_sobrevive_a_un_reinicio(self):
        self.ev("UserPromptSubmit", 0, prompt="antes del reinicio")
        self.r = trazas.Reconstructor(Path(self.tmp.name) / "sesiones")
        raiz = self.ev("Stop", 1, llamadas=[])[-1]
        self.assertEqual(self.a(raiz)["input.value"], "antes del reinicio")


try:
    sys.path.insert(0, str(RAIZ / "receptor"))
    os.environ.setdefault("TALLER_MONITOR_TOKEN", "x")
    import receptor  # noqa: E402

    TIENE_PROTOBUF = True
except Exception:
    TIENE_PROTOBUF = False


@unittest.skipUnless(TIENE_PROTOBUF, "necesita protobuf (receptor/.venv)")
class TestProtobuf(Base):
    def test_lote_a_protobuf(self):
        r = trazas.Reconstructor(Path(self.tmp.name) / "s")
        r.procesar({"ts": 1, "alumno": "ana", "evento": {"hook_event_name": "UserPromptSubmit", "session_id": "a", "prompt": "p"}})
        proyecto, spans = r.procesar({"ts": 2, "alumno": "ana", "evento": {"hook_event_name": "Stop", "session_id": "a"}})
        pb, n, _ = receptor.a_protobuf(trazas.lote(proyecto, spans))
        self.assertEqual(n, 1)
        from opentelemetry.proto.collector.trace.v1 import trace_service_pb2

        msg = trace_service_pb2.ExportTraceServiceRequest.FromString(pb)
        self.assertEqual(msg.resource_spans[0].scope_spans[0].spans[0].name, "turno")


if __name__ == "__main__":
    unittest.main()
