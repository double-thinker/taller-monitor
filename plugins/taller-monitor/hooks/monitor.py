#!/usr/bin/env python3
"""Hook de taller-monitor: reenvía cada evento de Claude Code al receptor del profesor.

Cliente sin estado: no guarda nada entre eventos ni usa cerrojos. El receptor monta las trazas.
Solo añade lo que el receptor no puede ver: la hora del evento y, en Stop y SubagentStop, las
llamadas al modelo leídas del transcript local (modelo, tokens y texto).

Si el envío falla, el evento se guarda como un fichero suelto en la carpeta `cola/` del directorio
de datos del plugin y se reintenta en el siguiente evento. Nunca bloquea ni cambia el comportamiento
de Claude Code: ante cualquier error termina con código 0 y sin salida.

Configuración. Gana la variable de entorno, después la opción del plugin y después el fichero
.claude/taller-monitor.json del repositorio ({"url": ..., "token": ..., "proyecto": ...}):
  TALLER_MONITOR_URL       o la opción "url"      URL base del receptor
  TALLER_MONITOR_TOKEN     o la opción "token"    token que da el profesor
  TALLER_MONITOR_ALUMNO    o la opción "alumno"   nombre visible (por defecto, el correo de git sin dominio)
  TALLER_MONITOR_PROYECTO  o la opción "proyecto" proyecto de Phoenix (por defecto, "taller")
  TALLER_MONITOR_OFF=1     desactiva el envío sin desinstalar el plugin
"""

from __future__ import annotations

import getpass
import json
import os
import platform
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

VERSION = "2.0.0"
MAX_TEXTO = 30_000  # caracteres por campo de texto
MAX_TRANSCRIPT = 4_000_000  # bytes leídos como máximo del final de un transcript
MAX_COLA = 500  # eventos pendientes como máximo
SINCRONOS = ("Stop", "SubagentStop", "SessionEnd", "StopFailure")


# --------------------------------------------------------------------------- configuración

def config_repo() -> dict:
    base = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    try:
        return json.loads((Path(base) / ".claude" / "taller-monitor.json").read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def opcion(nombre: str, defecto: str = "", repo: dict | None = None) -> str:
    v = os.environ.get(f"TALLER_MONITOR_{nombre.upper()}", "").strip()
    if not v:
        v = os.environ.get(f"CLAUDE_PLUGIN_OPTION_{nombre.upper()}", "").strip()
    if not v and repo:
        v = str(repo.get(nombre, "") or "").strip()
    return v or defecto


def alumno_por_defecto() -> str:
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
        "url": url.rstrip("/") + "/v1/events",
        "token": token,
        "alumno": opcion("alumno", repo=repo) or alumno_por_defecto(),
        "proyecto": opcion("proyecto", "taller", repo=repo),
    }


def dir_datos() -> Path:
    base = os.environ.get("CLAUDE_PLUGIN_DATA") or str(Path.home() / ".claude" / "taller-monitor")
    p = Path(base)
    p.mkdir(parents=True, exist_ok=True)
    return p


# --------------------------------------------------------------------------- utilidades

def recortar(v, limite: int = MAX_TEXTO):
    """Recorta recursivamente las cadenas largas de un objeto JSON."""
    if isinstance(v, str):
        return v if len(v) <= limite else v[:limite] + f"\n[... recortado, {len(v)} caracteres en total]"
    if isinstance(v, dict):
        return {k: recortar(x, limite) for k, x in v.items()}
    if isinstance(v, list):
        return [recortar(x, limite) for x in v[:500]]
    return v


def registrar_error(msg: str) -> None:
    """Rastro local de fallos (sin contenido de la sesión), acotado a ~200 KB."""
    try:
        ruta = dir_datos() / "errores.log"
        if ruta.exists() and ruta.stat().st_size > 200_000:
            os.replace(ruta, ruta.with_name("errores.log.1"))
        with open(ruta, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg[:500] + "\n")
    except Exception:
        pass


# --------------------------------------------------------------------------- transcript

def _leer_final(ruta: str) -> list[dict]:
    try:
        with open(ruta, "rb") as f:
            f.seek(0, os.SEEK_END)
            tam = f.tell()
            f.seek(max(0, tam - MAX_TRANSCRIPT))
            bruto = f.read()
    except Exception:
        return []
    lineas = bruto.split(b"\n")
    if tam > MAX_TRANSCRIPT:
        lineas = lineas[1:]  # la primera puede estar cortada
    salida = []
    for l in lineas:
        try:
            salida.append(json.loads(l))
        except Exception:
            pass
    return salida


def _es_prompt(e: dict) -> bool:
    """Una línea de usuario escrita por la persona (no un resultado de herramienta)."""
    if e.get("type") != "user" or e.get("isMeta"):
        return False
    c = (e.get("message") or {}).get("content")
    if isinstance(c, str):
        return True
    if isinstance(c, list):
        return not any(isinstance(p, dict) and p.get("type") == "tool_result" for p in c)
    return False


def _llamada(e: dict) -> dict:
    m = e.get("message") or {}
    uso = m.get("usage") or {}
    partes = m.get("content") or []
    return {
        "id": m.get("id", ""),
        "modelo": m.get("model", ""),
        "entrada": sum(int(uso.get(k, 0) or 0) for k in
                       ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")),
        "salida": int(uso.get("output_tokens", 0) or 0),
        "texto": "\n".join(p.get("text", "") for p in partes if isinstance(p, dict) and p.get("type") == "text"),
        "herramientas": [p.get("name", "") for p in partes if isinstance(p, dict) and p.get("type") == "tool_use"],
        "ts": e.get("timestamp", ""),
    }


def _unir(llamadas: list[dict]) -> list[dict]:
    """Las respuestas en streaming ocupan varias líneas con el mismo id: se unen en una."""
    por_id: dict[str, dict] = {}
    for ll in llamadas:
        clave = ll["id"] or secrets.token_hex(4)
        if clave in por_id:
            prev = por_id[clave]
            prev["texto"] = "\n".join(t for t in (prev["texto"], ll["texto"]) if t)
            prev["herramientas"] += ll["herramientas"]
            prev["salida"] = max(prev["salida"], ll["salida"])
            prev["ts"] = ll["ts"] or prev["ts"]
        else:
            por_id[clave] = ll
    return list(por_id.values())


def llamadas_del_turno(transcript: str) -> list[dict]:
    """Llamadas al modelo desde el último prompt de la persona hasta el final del transcript."""
    turno = []
    for e in reversed(_leer_final(transcript)):
        if _es_prompt(e):
            break
        if e.get("type") == "assistant":
            turno.append(_llamada(e))
    return _unir(list(reversed(turno)))


def llamadas_del_subagente(transcript: str) -> list[dict]:
    return _unir([_llamada(e) for e in _leer_final(transcript) if e.get("type") == "assistant"])


# --------------------------------------------------------------------------- envío y cola

def _post(cfg: dict, cuerpo: bytes, timeout: float) -> bool:
    """True si el receptor lo aceptó o lo rechazó de forma definitiva (no merece reintento)."""
    req = urllib.request.Request(
        cfg["url"], data=cuerpo, method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg['token']}",
            "User-Agent": f"taller-monitor/{VERSION}",  # Cloudflare rechaza el de Python (error 1010)
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status < 300
    except urllib.error.HTTPError as e:
        registrar_error(f"HTTP {e.code} en {cfg['url']}")
        return e.code not in (408, 429) and e.code < 500
    except Exception as e:
        registrar_error(f"{type(e).__name__} en {cfg['url']}: {e}")
        return False


def encolar(cuerpo: bytes) -> None:
    cola = dir_datos() / "cola"
    cola.mkdir(exist_ok=True)
    if sum(1 for _ in cola.glob("*.json")) >= MAX_COLA:
        registrar_error("cola llena: se descarta un evento")
        return
    nombre = f"{time.time_ns()}-{os.getpid()}-{secrets.token_hex(3)}"
    tmp = cola / f"{nombre}.tmp"
    tmp.write_bytes(cuerpo)
    os.replace(tmp, cola / f"{nombre}.json")  # aparece completo o no aparece


def vaciar_cola(cfg: dict, hasta: float) -> None:
    """Reintenta los eventos pendientes. Cada fichero se reclama renombrándolo: si otro proceso lo ha
    reclamado antes, el renombrado falla y se salta. No hacen falta cerrojos."""
    cola = dir_datos() / "cola"
    if not cola.exists():
        return
    ahora = time.time()
    for f in cola.glob("*.enviando-*"):  # reclamados por un proceso que murió
        try:
            if ahora - f.stat().st_mtime > 120:
                os.replace(f, cola / (f.name.split(".enviando-")[0] + ".json"))
        except OSError:
            pass
    for f in sorted(cola.glob("*.json")):
        if time.monotonic() > hasta:
            return
        reclamado = cola / f"{f.stem}.enviando-{os.getpid()}"
        try:
            os.replace(f, reclamado)
        except OSError:
            continue
        try:
            ok = _post(cfg, reclamado.read_bytes(), timeout=max(0.5, min(5, hasta - time.monotonic())))
        except OSError:
            ok = False
        try:
            if ok:
                reclamado.unlink()
            else:
                os.replace(reclamado, f)
                return  # el receptor no responde: se reintenta en el siguiente evento
        except OSError:
            pass


# --------------------------------------------------------------------------- principal

def construir(ev: dict, cfg: dict) -> dict:
    nombre = ev.get("hook_event_name", "")
    extra: dict = {}
    if nombre == "Stop" and ev.get("transcript_path"):
        extra["llamadas"] = llamadas_del_turno(ev["transcript_path"])
    elif nombre == "SubagentStop" and ev.get("agent_transcript_path"):
        extra["llamadas"] = llamadas_del_subagente(ev["agent_transcript_path"])
    return recortar({
        "ts": time.time_ns(),
        "alumno": cfg["alumno"],
        "proyecto": cfg["proyecto"],
        "cliente": {"version": VERSION, "so": platform.system(), "python": platform.python_version()},
        "evento": ev,
        **extra,
    })


def main() -> int:
    try:
        cfg = config()
        if cfg is None:
            return 0
        ev = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
        cuerpo = json.dumps(construir(ev, cfg), ensure_ascii=False).encode("utf-8")
        sincrono = ev.get("hook_event_name") in SINCRONOS
        # En los eventos síncronos Claude Code espera al hook: timeout corto y sin vaciar la cola.
        if not _post(cfg, cuerpo, timeout=2 if sincrono else 5):
            encolar(cuerpo)
        elif not sincrono:
            vaciar_cola(cfg, hasta=time.monotonic() + 15)
    except Exception as e:
        registrar_error(f"{type(e).__name__}: {e}")
        if os.environ.get("TALLER_MONITOR_DEBUG"):
            raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
