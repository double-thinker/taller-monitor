---
name: resumen-taller
description: Resume qué está haciendo cada alumno del taller a partir de las trazas que llegan a Phoenix. Úsala cuando el profesor pregunte cómo van los alumnos, quién está atascado, qué ha hecho un alumno concreto o pida un resumen de la sesión.
argument-hint: "[alumno] [proyecto] [últimos N minutos]"
---

# Resumen del taller

Las sesiones de Claude Code de los alumnos llegan a un Phoenix local (`http://127.0.0.1:6006`) con el plugin `taller-monitor`. Tu trabajo es leerlas y contarle al profesor, en español y en pocas líneas, cómo va cada alumno.

## Datos

Cada alumno es un `user.id`. Cada sesión de Claude Code es un `session.id` (vista Sessions de Phoenix). Cada turno es una traza. Spans:

| Span | Tipo | Qué contiene |
|---|---|---|
| `Turn` | AGENT, raíz de la traza | `input.value`: el prompt del alumno. `output.value`: la respuesta final. `workshop.close_reason` (stop, interrupted, error, session_end), `workshop.tool_count`, `workshop.tool_errors` |
| `Turn (subagent notification)` | AGENT, raíz | Turno que abre Claude Code cuando termina un subagente en segundo plano |
| `LLM call: <modelo>` | LLM | Cada llamada al modelo: tokens (también caché) y texto |
| `Bash`, `Edit`, `Read`... | TOOL | `input.value`: argumentos en JSON. `output.value`: resultado. Si falló, estado ERROR y `error.message`. `workshop.no_result`: empezó y no terminó (denegada o interrumpida) |
| `Tool started: <herramienta>` | CHAIN | Marca de inicio: una herramienta larga se ve desde que empieza |
| `Agent` | TOOL | La llamada que lanza un subagente, con su encargo |
| `Subagent: <tipo>` | AGENT | Un subagente: su encargo y su respuesta. Sus llamadas al modelo y sus herramientas cuelgan de él |
| `Context compaction` | CHAIN | Compactación del contexto dentro de un turno |

Mientras un turno o un subagente sigue en marcha, sus hijos ya están en Phoenix pero el span padre aún no (se emite al terminar, con su entrada y su salida). Phoenix muestra esa traza con el primer hijo como raíz provisional.

`workshop.folder` es la carpeta en la que trabaja el alumno y `workshop.os` su sistema. El proyecto de Phoenix por defecto es `taller`.

## Cómo leerlos

1. Usa las herramientas del MCP `phoenix`: lista los proyectos, las sesiones y los spans del proyecto. Filtra por ventana de tiempo si el profesor la da (por defecto, la última hora).
2. Si el MCP no basta, la API REST local sirve igual: `curl -s "http://127.0.0.1:6006/v1/projects/<proyecto>/spans?limit=500"`. Devuelve `data[]` con `name`, `start_time`, `end_time`, `status_code`, `parent_id` y `attributes`.
3. Agrupa por `user.id` y, dentro, ordena los turnos por hora.
4. Para saber quién ha instalado la monitorización y quién no, lee `receptor/data/registro.json` del repositorio taller-monitor. Los problemas de instalación que mandan los agentes están en `receptor/data/feedback.jsonl`.

## Qué contar de cada alumno

- **En qué está**: el último prompt resumido y la carpeta.
- **Avance**: turnos, ediciones de ficheros y tests ejecutados (comandos Bash con `test`, `pytest`, `dotnet test`, `npm test`...) y si pasaron.
- **Señales de atasco**: el mismo error de herramienta repetido, el mismo prompt reformulado varias veces, turnos interrumpidos, más de 10 minutos sin turnos, o el agente dando vueltas sobre los mismos ficheros.
- **Señales de riesgo**: comandos destructivos (`rm -rf`, `git reset --hard`, `git push --force`), lectura de `.env` o secretos, `curl` o `wget` hacia fuera. En la actividad de prompt injection, son justo lo que hay que detectar.
- **Coste**: tokens totales, solo si el profesor lo pide o si alguien destaca mucho.

## Formato de la respuesta

Una tabla corta (alumno, en qué está, estado: va bien, atascado o riesgo) y debajo, solo para los atascados o en riesgo, dos o tres líneas con la evidencia (la hora y lo que pasó) y qué le dirías al alumno. No pegues prompts enteros: resume. Si no hay datos, dilo y sugiere comprobar `curl <url>/salud` y el log del receptor.
