---
name: resumen-taller
description: Resume qué está haciendo cada alumno del taller a partir de las trazas que llegan a Phoenix. Úsala cuando el profesor pregunte cómo van los alumnos, quién está atascado, qué ha hecho un alumno concreto o pida un resumen de la sesión.
argument-hint: "[alumno] [proyecto] [últimos N minutos]"
---

# Resumen del taller

Las sesiones de Claude Code de los alumnos llegan a un Phoenix local (`http://127.0.0.1:6006`) con el plugin `taller-monitor`. Tu trabajo es leerlas y contarle al profesor, en español y en pocas líneas, cómo va cada alumno.

## Datos

Cada alumno es un `user.id`. Cada sesión de Claude Code es un `session.id`. Spans que vas a encontrar:

| Span | Tipo | Qué contiene |
|---|---|---|
| `turno` | AGENT, raíz de la traza | `input.value`: el prompt del alumno. `output.value`: la respuesta final. `taller.herramientas`, `taller.errores`, `taller.cierre` (normal, interrumpido, error, fin de sesión), tokens |
| `modelo` | LLM, hijo de `turno` | Cada llamada al modelo: modelo, tokens y texto |
| `Bash`, `Edit`, `Read`... | TOOL, hijo de `turno` | `input.value`: argumentos en JSON. `output.value`: resultado. Estado de error si la herramienta falló |
| `subagente <tipo>` | AGENT | Un subagente: su respuesta final y, colgando de él, sus llamadas al modelo y sus herramientas |
| `aviso de subagente` | AGENT, raíz | Turno que abre Claude Code cuando termina un subagente en segundo plano |
| Herramienta en rojo con `taller.sin_resultado` | TOOL | Empezó y no terminó (denegada o interrumpida), o se denegó el permiso |
| `inicio de sesión`, `fin de sesión`, `compactación de contexto` | CHAIN | Marcas de la sesión |

`taller.proyecto_local` es la carpeta en la que trabaja el alumno. El proyecto de Phoenix por defecto es `taller`.

## Cómo leerlos

1. Usa las herramientas del MCP `phoenix`: lista los proyectos, las sesiones y los spans del proyecto. Filtra por ventana de tiempo si el profesor la da (por defecto, la última hora).
2. Si el MCP no basta, la API REST local sirve igual: `curl -s "http://127.0.0.1:6006/v1/projects/<proyecto>/spans?limit=500"`. Devuelve `data[]` con `name`, `start_time`, `end_time`, `status_code`, `parent_id` y `attributes`.
3. Agrupa por `user.id` y, dentro, ordena los turnos por hora.

## Qué contar de cada alumno

- **En qué está**: el último prompt resumido y la carpeta.
- **Avance**: turnos, ediciones de ficheros y tests ejecutados (comandos Bash con `test`, `pytest`, `dotnet test`, `npm test`...) y si pasaron.
- **Señales de atasco**: el mismo error de herramienta repetido, el mismo prompt reformulado varias veces, turnos interrumpidos, más de 10 minutos sin turnos, o el agente dando vueltas sobre los mismos ficheros.
- **Señales de riesgo**: comandos destructivos (`rm -rf`, `git reset --hard`, `git push --force`), lectura de `.env` o secretos, `curl` o `wget` hacia fuera. En la actividad de prompt injection, son justo lo que hay que detectar.
- **Coste**: tokens totales, solo si el profesor lo pide o si alguien destaca mucho.

## Formato de la respuesta

Una tabla corta (alumno, en qué está, estado: va bien, atascado o riesgo) y debajo, solo para los atascados o en riesgo, dos o tres líneas con la evidencia (la hora y lo que pasó) y qué le dirías al alumno. No pegues prompts enteros: resume. Si no hay datos, dilo y sugiere comprobar `curl <url>/salud` y el log del receptor.
