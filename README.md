# taller-monitor

Monitorización de las sesiones de Claude Code durante un taller. Cada alumno instala un plugin que manda lo que hace (prompts, herramientas, respuestas y tokens) a un receptor del profesor. El profesor lo ve en [Phoenix](https://github.com/Arize-ai/phoenix) y le pregunta a un agente cómo va cada alumno.

```
Claude Code del alumno ── plugin taller-monitor (hooks) ──HTTPS + token──▶ receptor ──▶ Phoenix (solo local)
                                                                                           │
                                                      Claude Code del profesor ◀── MCP ────┘
                                                      (plugin taller-profesor, skill resumen-taller)
```

El repositorio es un marketplace de Claude Code con dos plugins:

| Plugin | Para quién | Qué hace |
|---|---|---|
| `taller-monitor` | Alumnos | Hooks que mandan cada turno como una traza: el prompt, cada llamada al modelo con sus tokens, cada herramienta con su entrada y su salida, los subagentes y la respuesta final |
| `taller-profesor` | Profesor | Conecta Claude Code al MCP de Phoenix y añade la skill `resumen-taller` |

## Para los alumnos

Requisitos: Claude Code y `python3` en el `PATH` (el plugin solo usa la biblioteca estándar).

```bash
claude plugin marketplace add double-thinker/taller-monitor
claude plugin install taller-monitor@taller-monitor \
  --config alumno=TU-NOMBRE \
  --config url=URL-DEL-PROFESOR \
  --config token=TOKEN-DEL-PROFESOR
```

Si instalas sin `--config`, Claude Code te pide los tres valores con `/plugin configure taller-monitor@taller-monitor`.

También se puede configurar con variables de entorno, que tienen prioridad sobre la configuración del plugin: `TALLER_MONITOR_URL`, `TALLER_MONITOR_TOKEN`, `TALLER_MONITOR_ALUMNO` y `TALLER_MONITOR_PROYECTO`.

Para dejar de enviar:

```bash
claude plugin uninstall taller-monitor@taller-monitor     # al acabar el taller
export TALLER_MONITOR_OFF=1                                # pausa, sin desinstalar
```

**Qué se envía:** el texto de tus prompts, las respuestas de Claude, los comandos que ejecuta, los ficheros que lee o edita (contenido incluido, recortado a 30 000 caracteres por campo), la carpeta en la que trabajas y los tokens. No se envían variables de entorno ni ficheros que Claude no haya tocado. Los datos se quedan en el ordenador del profesor y no se comparten con terceros.

## Para el profesor

Requisitos: [`uv`](https://docs.astral.sh/uv/) y una forma de exponer un puerto a internet (Tailscale Funnel o Cloudflare).

```bash
git clone https://github.com/double-thinker/taller-monitor
cd taller-monitor/receptor
./arrancar.sh            # instala Phoenix la primera vez (unos 700 MB)
```

El script arranca Phoenix en `127.0.0.1:6006`, el receptor en `127.0.0.1:6010` y la exposición pública. Al final imprime la URL y el token que hay que dar a los alumnos. El token se genera la primera vez y se guarda en `receptor/data/token`.

Exposición, con `TALLER_EXPONER`:

| Valor | Qué hace |
|---|---|
| `tailscale` (por defecto) | `tailscale funnel` en el puerto `TALLER_FUNNEL_PUERTO` (por defecto 10000). El equipo tiene que estar autorizado para Funnel en la política del tailnet; si no lo está, el script imprime el enlace para autorizarlo |
| `cloudflare-rapido` | Túnel temporal de Cloudflare sin cuenta (`*.trycloudflare.com`). La URL cambia en cada arranque |
| `cloudflare` | Túnel con nombre propio creado en el panel de Cloudflare apuntando a `http://127.0.0.1:6010`. Necesita `CLOUDFLARE_TUNNEL_TOKEN` y `TALLER_URL_PUBLICA` |
| `ninguno` | Solo local, para pruebas |

Solo el receptor queda expuesto, y solo responde a `GET /salud` y a `POST /v1/traces` con el token. La interfaz, la API y el MCP de Phoenix siguen en `127.0.0.1`.

Para parar todo: `./parar.sh`. Los datos quedan en `receptor/data/` (Phoenix usa SQLite).

### Ver qué hacen

- **Panel:** <http://127.0.0.1:6006>. Proyecto `taller`. Cada turno es una traza, y las sesiones y los alumnos (`user.id`) se pueden filtrar.
- **Log en vivo:** `tail -f receptor/data/receptor.log` muestra una línea por envío con el alumno y el número de spans.
- **Agente:** instala el plugin del profesor y pregúntale.

  ```bash
  claude plugin marketplace add double-thinker/taller-monitor
  claude plugin install taller-profesor@taller-monitor
  claude
  > /taller-profesor:resumen-taller
  > ¿quién está atascado en el ticket 08?
  ```

  La skill agrupa por alumno, resume en qué está cada uno y señala atascos (errores repetidos, prompts reformulados, turnos interrumpidos) y riesgos (comandos destructivos, lectura de `.env`, `curl` hacia fuera).

## Cómo funciona

- **Plugin del alumno** (`plugins/taller-monitor/hooks/monitor.py`): sin estado ni cerrojos. En cada evento de Claude Code (`SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `PermissionDenied`, `SubagentStart`, `SubagentStop`, `PreCompact`, `Stop`, `StopFailure`, `SessionEnd`) reenvía el evento tal cual al receptor (`POST /v1/events`) con su hora. En `Stop` y `SubagentStop` añade las llamadas al modelo leídas del transcript local (modelo, tokens y texto), que el receptor no puede ver. Si el envío falla, el evento se guarda como un fichero suelto en `cola/`, dentro del directorio de datos del plugin (`~/.claude/plugins/data/taller-monitor-taller-monitor/`), y se reintenta en el siguiente evento. Los fallos quedan en `errores.log`, en ese mismo directorio. Nunca bloquea ni cambia el comportamiento de Claude Code.
- **Receptor** (`receptor/receptor.py` y `receptor/trazas.py`): comprueba el token, guarda el estado de cada sesión y monta las trazas [OpenInference](https://github.com/Arize-ai/openinference) para Phoenix:
  - `turno` (AGENT), con el prompt, la respuesta y los tokens. Los avisos de subagentes en segundo plano salen como `aviso de subagente`.
  - `modelo` (LLM), una por llamada al modelo.
  - Herramientas (TOOL), con entrada y salida. Las que empiezan y no terminan en 90 s, o que se deniegan, salen en rojo como "sin resultado" o "denegada".
  - `subagente <tipo>` (AGENT), con sus propias herramientas y llamadas al modelo colgando de él, también si termina después del turno que lo lanzó.
- **Otras fuentes que acepta el receptor:**
  - Hooks HTTP de Claude Code (`"type": "http"`) contra `/v1/events`, con la cabecera `X-Alumno`. No necesitan nada instalado, pero no envían tokens ni `SessionStart`.
  - Telemetría nativa de Claude Code (`OTEL_EXPORTER_OTLP_ENDPOINT` con `http/json`): las trazas se traducen a OpenInference y van al proyecto `taller-otel`; eventos y métricas se guardan en `receptor/data/`.
- **Agente del profesor**: Phoenix 19 o superior trae un servidor MCP en `/mcp`. El plugin `taller-profesor` lo registra y la skill explica qué significa cada span.

## Endpoints del receptor

Todos piden `Authorization: Bearer <token>` salvo `/salud`.

| Método y ruta | Para qué |
|---|---|
| `GET /salud` | Comprobar que el receptor responde |
| `POST /v1/events` | Eventos de hooks: del plugin (con envoltorio) o de hooks HTTP (el evento tal cual, alumno por `X-Alumno` o por la carpeta inscrita) |
| `POST /v1/traces`, `/v1/logs`, `/v1/metrics` | Telemetría OTLP en JSON: la del plugin 1.x y la nativa de Claude Code |
| `POST /v1/registro` | Inscribir a un alumno: `{"alumno", "cwd", "so", "via"}`. La carpeta sirve para reconocer sus sesiones de hooks HTTP |
| `GET /v1/registro?alumno=<nombre>` | Si está inscrito y si llegan datos suyos (`"recibiendo": true`) |
| `POST /v1/feedback` | Problemas de instalación que reportan los agentes. Se guardan en `receptor/data/feedback.jsonl` |

El registro se guarda en `receptor/data/registro.json`.

## Tests

```bash
python3 -m unittest discover -s tests -v                   # plugin del alumno
receptor/.venv/bin/python -m unittest discover -s tests -v # también el receptor
```

## Licencia

Apache 2.0.
