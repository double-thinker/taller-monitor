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
        self.assertEqual(c["urls"], ["https://m.example/v1/events"])
        self.assertEqual((c["alumno"], c["proyecto"]), ("eva", "p"))

    def test_variable_gana_a_opcion(self):
        os.environ.update(CLAUDE_PLUGIN_OPTION_URL="https://a", CLAUDE_PLUGIN_OPTION_TOKEN="k",
                          TALLER_MONITOR_URL="https://b")
        self.assertEqual(monitor.config()["urls"], ["https://b/v1/events"])

    def test_varias_urls_y_alumno_local(self):
        repo = Path(self.tmp.name) / "r2"
        (repo / ".claude").mkdir(parents=True)
        (repo / ".claude" / "taller-monitor.json").write_text(json.dumps({"url": ["https://a", "https://b"], "token": "k"}), "utf-8")
        (repo / ".claude" / "taller-monitor.local.json").write_text(json.dumps({"alumno": "luis"}), "utf-8")
        os.environ["CLAUDE_PROJECT_DIR"] = str(repo)
        c = monitor.config()
        self.assertEqual(c["urls"], ["https://a/v1/events", "https://b/v1/events"])
        self.assertEqual(c["alumno"], "luis")

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

    def test_llamadas_recientes_y_transcript_del_subagente(self):
        t = self.escribir([
            {"type": "user", "message": {"content": "prompt"}},
            self.asistente("a", "uno"),
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x"}]}},
            self.asistente("b", "dos"),
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "y"}]}},
        ])
        self.assertEqual([x["id"] for x in monitor.llamadas_recientes(t)], ["b"])
        ev = {"transcript_path": "/p/sesion.jsonl", "agent_id": "ab1"}
        self.assertEqual(Path(monitor.transcript_de(ev)).as_posix(), "/p/sesion/subagents/agent-ab1.jsonl")

    def test_transcript_inexistente(self):
        self.assertEqual(monitor.llamadas_del_turno("/no/existe.jsonl"), [])


class TestCola(Base):
    def test_fallo_de_red_encola_y_se_reintenta(self):
        cfg = {"urls": ["http://127.0.0.1:9/v1/events", "http://127.0.0.1:8/v1/events"], "token": "t", "alumno": "a", "proyecto": "p"}
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

    @staticmethod
    def uno(spans, nombre):
        return [s for s in spans if s["name"] == nombre][0]

    def ll(self, id_, texto, dt=None, ent=10, sal=5):
        from datetime import datetime, timezone
        ts = datetime.fromtimestamp((self.t + dt * S) / S, timezone.utc).isoformat() if dt is not None else ""
        return {"id": id_, "modelo": "m", "entrada": ent, "salida": sal, "texto": texto, "ts": ts}

    def test_raiz_al_empezar_y_cierre_al_final(self):
        raiz = self.uno(self.ev("UserPromptSubmit", 0, prompt="arregla"), "turno")
        self.assertNotIn("parentSpanId", raiz)
        self.assertEqual(self.a(raiz)["input.value"], "arregla")
        tool = self.ev("PostToolUse", 2, tool_name="Bash", tool_use_id="u1", duration_ms=1000,
                       tool_response={"stdout": "ok"}, llamadas=[self.ll("m1", "voy", 1)])
        self.assertEqual({s["name"] for s in tool}, {"modelo", "Bash"})
        self.assertTrue(all(s["parentSpanId"] == raiz["spanId"] for s in tool))
        cierre = self.ev("Stop", 3, last_assistant_message="hecho", llamadas=[self.ll("m1", "voy", 1), self.ll("m2", "hecho", 3)])
        self.assertEqual([s["name"] for s in cierre], ["modelo", "fin del turno"])  # m1 ya estaba: no se repite
        fin = self.a(cierre[-1])
        self.assertEqual((fin["output.value"], fin["llm.token_count.total"], fin["taller.duracion_s"]), ("hecho", "30", 3.0))
        self.assertEqual(self.a(raiz)["taller.proyecto_local"], "x")

    def test_post_antes_que_pre_usa_duration_ms(self):
        self.ev("UserPromptSubmit", 0, prompt="x")
        tool = self.uno(self.ev("PostToolUse", 5, tool_name="Read", tool_use_id="u2", duration_ms=2000), "Read")
        self.assertEqual(int(tool["endTimeUnixNano"]) - int(tool["startTimeUnixNano"]), 2 * S)
        self.assertEqual(self.ev("PreToolUse", 6, tool_name="Read", tool_use_id="u2"), [])
        self.assertNotIn("u2", self.r.sesiones["s1"].herr)

    def test_subagente_en_vivo(self):
        raiz = self.uno(self.ev("UserPromptSubmit", 0, prompt="explora"), "turno")
        self.ev("PreToolUse", 1, tool_name="Agent", tool_use_id="a1", tool_input={"prompt": "busca el bug", "subagent_type": "Explore"})
        sub = self.uno(self.ev("SubagentStart", 1, agent_id="ag1", agent_type="Explore"), "subagente Explore")
        self.assertEqual(sub["parentSpanId"], raiz["spanId"])
        self.assertEqual(self.a(sub)["input.value"], "busca el bug")
        en_vivo = self.ev("PostToolUse", 2, tool_name="Grep", tool_use_id="u3", agent_id="ag1", llamadas=[self.ll("s1", "pienso", 2)])
        self.assertTrue(all(s["parentSpanId"] == sub["spanId"] for s in en_vivo))
        fin = self.ev("SubagentStop", 3, agent_id="ag1", last_assistant_message="informe", llamadas=[self.ll("s1", "pienso", 2)])
        self.assertEqual([s["name"] for s in fin], ["fin del subagente"])
        self.assertEqual(self.a(fin[0])["output.value"], "informe")
        self.assertEqual(self.a(fin[0])["llm.token_count.total"], "15")

    def test_subagente_en_segundo_plano_tras_stop(self):
        raiz = self.uno(self.ev("UserPromptSubmit", 0, prompt="lanza"), "turno")
        self.ev("SubagentStart", 1, agent_id="ag2", agent_type="general-purpose")
        self.ev("Stop", 2, llamadas=[])
        t = self.uno(self.ev("PostToolUse", 5, tool_name="Bash", tool_use_id="u4", agent_id="ag2"), "Bash")
        fin = self.ev("SubagentStop", 6, agent_id="ag2")[0]
        self.assertEqual(t["traceId"], raiz["traceId"])
        self.assertEqual(t["parentSpanId"], fin["parentSpanId"])

    def test_herramienta_sin_resultado_caduca(self):
        self.ev("UserPromptSubmit", 0, prompt="x")
        self.ev("PreToolUse", 1, tool_name="Bash", tool_use_id="u5", tool_input={"command": "sleep 30"})
        self.ev("Stop", 2, llamadas=[])
        perdida = self.uno(self.ev("UserPromptSubmit", 2 + trazas.GRACIA + 1, prompt="otra"), "Bash")
        self.assertEqual(perdida["status"]["code"], 2)
        self.assertIn("sin resultado", self.a(perdida)["output.value"])

    def test_permiso_denegado(self):
        self.ev("UserPromptSubmit", 0, prompt="x")
        self.ev("PreToolUse", 1, tool_name="Bash", tool_use_id="u6")
        s = self.ev("PermissionDenied", 2, tool_name="Bash", tool_use_id="u6", reason="clasificador")[0]
        self.assertIn("denegada: clasificador", self.a(s)["output.value"])

    def test_aviso_de_subagente_y_error_de_api(self):
        self.assertEqual(self.ev("UserPromptSubmit", 0, prompt="<task-notification>x</task-notification>")[0]["name"], "aviso de subagente")
        self.ev("Stop", 1, llamadas=[])
        self.ev("UserPromptSubmit", 2, prompt="hola")
        self.assertEqual(self.ev("StopFailure", 3, error="overloaded")[-1]["status"]["code"], 2)

    def test_llamadas_de_un_turno_sin_stop_van_a_su_turno(self):
        r1 = self.uno(self.ev("UserPromptSubmit", 0, prompt="primero"), "turno")
        self.ev("UserPromptSubmit", 10, prompt="segundo")  # sin Stop antes
        spans = self.ev("Stop", 20, llamadas=[self.ll("a", "del primero", 5), self.ll("b", "del segundo", 15)])
        llm = {self.a(s)["output.value"]: s for s in spans if s["name"] == "modelo"}
        self.assertEqual(llm["del primero"]["parentSpanId"], r1["spanId"])
        self.assertNotEqual(llm["del segundo"]["traceId"], r1["traceId"])

    def test_hook_http_sin_envoltorio_y_alumno_por_carpeta(self):
        _, spans = self.r.procesar({"hook_event_name": "UserPromptSubmit", "session_id": "s9", "prompt": "hola",
                                    "cwd": "/home/eva/taller/a"}, alumno_por_cwd=lambda c: "eva" if c else None)
        self.assertEqual(self.a(spans[0])["user.id"], "eva")
        self.assertEqual(self.a(spans[0])["taller.cliente"], "hook-http")

    def test_estado_sobrevive_a_un_reinicio(self):
        self.ev("UserPromptSubmit", 0, prompt="antes del reinicio")
        self.r = trazas.Reconstructor(Path(self.tmp.name) / "sesiones")
        fin = self.ev("Stop", 1, llamadas=[])[-1]
        self.assertEqual(self.a(fin)["input.value"], "antes del reinicio")


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
        proyecto, spans = r.procesar({"ts": 1, "alumno": "ana", "evento": {"hook_event_name": "UserPromptSubmit", "session_id": "a", "prompt": "p"}})
        pb, n, _ = receptor.a_protobuf(trazas.lote(proyecto, spans))
        self.assertEqual(n, 1)
        from opentelemetry.proto.collector.trace.v1 import trace_service_pb2

        msg = trace_service_pb2.ExportTraceServiceRequest.FromString(pb)
        self.assertEqual(msg.resource_spans[0].scope_spans[0].spans[0].name, "turno")

    def test_registro(self):
        reg = receptor.Registro(str(Path(self.tmp.name) / "registro.json"))
        self.assertFalse(reg.consulta("eva")["inscrito"])
        reg.inscribir("eva", cwd="C:\\Users\\Eva\\taller", so="Windows", via="plugin")
        c = reg.consulta("eva")
        self.assertEqual((c["inscrito"], c["recibiendo"]), (True, False))
        self.assertEqual(reg.alumno_por_cwd("c:/users/eva/taller/b-satelite"), "eva")
        reg.visto("eva", "plugin", "s1")
        self.assertTrue(reg.consulta("eva")["recibiendo"])


if __name__ == "__main__":
    unittest.main()
