"""Tests del hook del alumno y de la conversión del receptor.

    python3 -m unittest discover -s tests -v
    (el test del receptor necesita protobuf: receptor/.venv/bin/python -m unittest discover -s tests -v)
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent


def cargar(nombre, ruta):
    spec = importlib.util.spec_from_file_location(nombre, ruta)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


monitor = cargar("monitor", RAIZ / "plugins/taller-monitor/hooks/monitor.py")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = dict(os.environ)
        for k in list(os.environ):
            if k.startswith(("TALLER_MONITOR_", "CLAUDE_PLUGIN_OPTION_")):
                del os.environ[k]
        os.environ["CLAUDE_PLUGIN_DATA"] = self.tmp.name
        self.transcript = Path(self.tmp.name) / "t.jsonl"
        self.transcript.write_text("")
        self.cfg = {"url": "http://x/v1/traces", "token": "t", "alumno": "ana", "proyecto": "taller"}

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)
        self.tmp.cleanup()

    def ev(self, nombre, **kw):
        return {"hook_event_name": nombre, "session_id": "s1", "transcript_path": str(self.transcript),
                "cwd": "/home/ana/a-webapp", **kw}

    def asistente(self, id_, texto, entrada=10, salida=5):
        linea = {"type": "assistant", "timestamp": "2026-11-16T10:00:00Z",
                 "message": {"id": id_, "model": "claude-x", "usage": {"input_tokens": entrada, "output_tokens": salida},
                             "content": [{"type": "text", "text": texto}]}}
        with open(self.transcript, "a") as f:
            f.write(json.dumps(linea) + "\n")


class TestConfig(Base):
    def test_sin_url_no_hace_nada(self):
        self.assertIsNone(monitor.config())

    def test_opciones_del_plugin(self):
        os.environ.update(CLAUDE_PLUGIN_OPTION_URL="https://r.example/", CLAUDE_PLUGIN_OPTION_TOKEN="k",
                          CLAUDE_PLUGIN_OPTION_ALUMNO="luis")
        c = monitor.config()
        self.assertEqual(c["url"], "https://r.example/v1/traces")
        self.assertEqual(c["alumno"], "luis")
        self.assertEqual(c["proyecto"], "taller")

    def test_variable_de_entorno_gana(self):
        os.environ.update(CLAUDE_PLUGIN_OPTION_URL="https://a", CLAUDE_PLUGIN_OPTION_TOKEN="k",
                          TALLER_MONITOR_URL="https://b")
        self.assertEqual(monitor.config()["url"], "https://b/v1/traces")

    def test_off(self):
        os.environ.update(TALLER_MONITOR_URL="https://b", TALLER_MONITOR_TOKEN="k", TALLER_MONITOR_OFF="1")
        self.assertIsNone(monitor.config())


class TestTurno(Base):
    def test_turno_completo(self):
        p = monitor.procesar
        self.assertEqual(len(p(self.ev("SessionStart", source="startup"), self.cfg)), 1)
        self.assertEqual(p(self.ev("UserPromptSubmit", prompt="arregla el test"), self.cfg), [])
        p(self.ev("PreToolUse", tool_name="Bash", tool_use_id="u1", tool_input={"command": "npm test"}), self.cfg)
        tool = p(self.ev("PostToolUse", tool_name="Bash", tool_use_id="u1", tool_input={"command": "npm test"},
                         tool_response={"stdout": "ok"}), self.cfg)
        self.asistente("m1", "Hecho: el test pasa.")
        cierre = p(self.ev("Stop", last_assistant_message="Hecho: el test pasa."), self.cfg)

        self.assertEqual(len(tool), 1)
        raiz = [s for s in cierre if "parentSpanId" not in s][0]
        llm = [s for s in cierre if s["name"] == "modelo"]
        self.assertEqual(raiz["name"], "turno")
        self.assertEqual(tool[0]["traceId"], raiz["traceId"])
        self.assertEqual(tool[0]["parentSpanId"], raiz["spanId"])
        self.assertEqual(len(llm), 1)
        attrs = {a["key"]: a["value"] for a in raiz["attributes"]}
        self.assertEqual(attrs["input.value"]["stringValue"], "arregla el test")
        self.assertEqual(attrs["output.value"]["stringValue"], "Hecho: el test pasa.")
        self.assertEqual(attrs["user.id"]["stringValue"], "ana")
        self.assertEqual(attrs["llm.token_count.total"]["intValue"], "15")
        self.assertEqual(attrs["taller.herramientas"]["intValue"], "1")

    def test_herramienta_fallida_marca_error(self):
        p = monitor.procesar
        p(self.ev("UserPromptSubmit", prompt="x"), self.cfg)
        s = p(self.ev("PostToolUseFailure", tool_name="Bash", tool_use_id="u2", error="exit 1"), self.cfg)[0]
        self.assertEqual(s["status"]["code"], 2)

    def test_fin_de_sesion_cierra_turno_con_texto_del_transcript(self):
        p = monitor.procesar
        p(self.ev("SessionStart", source="startup"), self.cfg)
        p(self.ev("UserPromptSubmit", prompt="hola"), self.cfg)
        self.asistente("m1", "respuesta final")
        spans = p(self.ev("SessionEnd", reason="other"), self.cfg)
        turno = [s for s in spans if s["name"] == "turno"][0]
        attrs = {a["key"]: a["value"] for a in turno["attributes"]}
        self.assertEqual(attrs["output.value"]["stringValue"], "respuesta final")
        self.assertEqual(attrs["taller.cierre"]["stringValue"], "fin de sesión")

    def test_subagente_en_segundo_plano_va_al_turno_que_lo_lanzo(self):
        p = monitor.procesar
        p(self.ev("UserPromptSubmit", prompt="explora"), self.cfg)
        cierre = p(self.ev("Stop", last_assistant_message="lanzado"), self.cfg)
        raiz = [s for s in cierre if s["name"] == "turno"][0]
        s = p(self.ev("PostToolUse", tool_name="Grep", tool_use_id="u3", agent_id="a1", agent_type="Explore"), self.cfg)[0]
        self.assertEqual(s["traceId"], raiz["traceId"])
        self.assertEqual(s["parentSpanId"], raiz["spanId"])

    def test_texto_largo_se_recorta(self):
        self.assertLess(len(monitor.texto("x" * 100_000)), monitor.MAX_TEXTO + 100)


class TestEnvio(Base):
    def test_fallo_de_red_va_a_la_cola_y_se_reintenta(self):
        cfg = dict(self.cfg, url="http://127.0.0.1:9/v1/traces")
        span = monitor.span(cfg, "s1", trace_id="0" * 32, span_id="1" * 16, padre=None, nombre="x",
                            tipo="CHAIN", inicio=1, fin=2, attrs={})
        monitor.enviar(cfg, [span])
        cola = Path(self.tmp.name) / "pendientes.jsonl"
        self.assertTrue(cola.exists())
        self.assertEqual(len(cola.read_text().splitlines()), 1)


try:
    sys.path.insert(0, str(RAIZ / "receptor"))
    import receptor  # noqa: E402

    TIENE_PROTOBUF = True
except Exception:
    TIENE_PROTOBUF = False


@unittest.skipUnless(TIENE_PROTOBUF, "necesita protobuf (receptor/.venv)")
class TestReceptor(Base):
    def test_json_a_protobuf(self):
        span = monitor.span(self.cfg, "s1", trace_id="ab" * 16, span_id="cd" * 8, padre="ef" * 8, nombre="x",
                            tipo="TOOL", inicio=1, fin=2, attrs={"input.value": "hola"})
        pb, n, alumnos = receptor.a_protobuf(monitor.lote(self.cfg, [span]))
        self.assertEqual(n, 1)
        self.assertEqual(alumnos, {"ana"})
        from opentelemetry.proto.collector.trace.v1 import trace_service_pb2

        msg = trace_service_pb2.ExportTraceServiceRequest.FromString(pb)
        s = msg.resource_spans[0].scope_spans[0].spans[0]
        self.assertEqual(s.trace_id.hex(), "ab" * 16)
        self.assertEqual(s.parent_span_id.hex(), "ef" * 8)


if __name__ == "__main__":
    unittest.main()
