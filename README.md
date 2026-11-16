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

- **Plugin del alumno** (`plugins/taller-monitor/hooks/monitor.py`): se ejecuta en `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `SubagentStop`, `PreCompact`, `Stop` y `SessionEnd`. Guarda el estado de cada sesión en el directorio de datos del plugin y, al cerrar cada turno, lee del transcript las llamadas al modelo para sacar los tokens. Manda OTLP en JSON con atributos [OpenInference](https://github.com/Arize-ai/openinference), que es lo que entiende Phoenix. Si el receptor no responde, guarda los envíos en una cola y los reintenta en el siguiente evento. Nunca bloquea ni cambia el comportamiento de Claude Code: ante cualquier error, sale con código 0.
- **Receptor** (`receptor/receptor.py`): comprueba el token, convierte el JSON a protobuf (Phoenix solo acepta protobuf) y lo reenvía a Phoenix.
- **Agente del profesor**: Phoenix 19 o superior trae un servidor MCP en `/mcp`. El plugin `taller-profesor` lo registra y la skill explica qué significa cada span.

## Tests

```bash
python3 -m unittest discover -s tests -v                   # plugin del alumno
receptor/.venv/bin/python -m unittest discover -s tests -v # también el receptor
```

## Licencia

Apache 2.0.
