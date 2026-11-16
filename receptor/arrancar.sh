#!/usr/bin/env bash
# Arranca Phoenix (solo local), el receptor y la exposición pública.
#
# Variables (todas opcionales):
#   TALLER_EXPONER        tailscale (por defecto) | cloudflare-rapido | cloudflare | ninguno
#   CLOUDFLARE_TUNNEL_TOKEN, TALLER_URL_PUBLICA   solo con TALLER_EXPONER=cloudflare
#   TALLER_FUNNEL_PUERTO  puerto público de Tailscale Funnel: 443, 8443 o 10000 (por defecto 10000)
#   RECEPTOR_PUERTO       puerto local del receptor (por defecto 6010)
#   PHOENIX_PORT          puerto local de Phoenix (por defecto 6006)
#   TALLER_MONITOR_TOKEN  token; si no se da, se genera uno y se guarda en data/token
set -euo pipefail
cd "$(dirname "$0")"

EXPONER="${TALLER_EXPONER:-tailscale}"
FUNNEL="${TALLER_FUNNEL_PUERTO:-10000}"
export RECEPTOR_PUERTO="${RECEPTOR_PUERTO:-6010}"
export PHOENIX_PORT="${PHOENIX_PORT:-6006}"
export PHOENIX_URL="http://127.0.0.1:${PHOENIX_PORT}"
mkdir -p data

# 1. Entorno con Phoenix
if [ ! -x .venv/bin/phoenix ]; then
  echo "Instalando Phoenix en receptor/.venv (unos 700 MB)..."
  uv venv -q .venv --python 3.12
  uv pip install -q --python .venv/bin/python arize-phoenix
fi

# 2. Token
if [ -z "${TALLER_MONITOR_TOKEN:-}" ]; then
  [ -s data/token ] || .venv/bin/python -c "import secrets;print(secrets.token_urlsafe(24))" > data/token
  TALLER_MONITOR_TOKEN="$(cat data/token)"
fi
export TALLER_MONITOR_TOKEN

vivo() { [ -s "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }

# 3. Phoenix, solo en 127.0.0.1
if ! vivo data/phoenix.pid; then
  PHOENIX_WORKING_DIR="$PWD/data" PHOENIX_HOST=127.0.0.1 PHOENIX_GRPC_PORT="${PHOENIX_GRPC_PORT:-4317}" \
    nohup .venv/bin/phoenix serve >> data/phoenix.log 2>&1 &
  echo $! > data/phoenix.pid
fi
for _ in $(seq 1 60); do curl -fs -o /dev/null "$PHOENIX_URL/" && break; sleep 1; done
curl -fs -o /dev/null "$PHOENIX_URL/" || { echo "Phoenix no arranca: mira receptor/data/phoenix.log"; exit 1; }

# 4. Receptor
if ! vivo data/receptor.pid; then
  nohup .venv/bin/python receptor.py >> data/receptor.log 2>&1 &
  echo $! > data/receptor.pid
fi
for _ in $(seq 1 20); do curl -fs -o /dev/null "http://127.0.0.1:${RECEPTOR_PUERTO}/salud" && break; sleep 0.5; done

# 5. Exposición
URL_PUBLICA="http://127.0.0.1:${RECEPTOR_PUERTO}"
case "$EXPONER" in
  tailscale)
    SALIDA="$(timeout 20 tailscale funnel --bg --https="$FUNNEL" "http://127.0.0.1:${RECEPTOR_PUERTO}" 2>&1 || true)"
    if echo "$SALIDA" | grep -q "login.tailscale.com"; then
      echo "$SALIDA"
      echo
      echo "Tailscale Funnel no está autorizado para este equipo. Abre el enlace de arriba, autorízalo y vuelve a ejecutar ./arrancar.sh."
      echo "O usa otra salida: TALLER_EXPONER=cloudflare-rapido ./arrancar.sh"
      exit 1
    fi
    NOMBRE="$(tailscale status --json | .venv/bin/python -c 'import json,sys;print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
    URL_PUBLICA="https://${NOMBRE}"
    [ "$FUNNEL" = "443" ] || URL_PUBLICA="${URL_PUBLICA}:${FUNNEL}"
    ;;
  cloudflare-rapido)
    # Túnel temporal de Cloudflare, sin cuenta: la URL cambia cada vez que se arranca.
    if ! vivo data/cloudflared.pid; then
      : > data/cloudflared.log
      nohup cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:${RECEPTOR_PUERTO}" >> data/cloudflared.log 2>&1 &
      echo $! > data/cloudflared.pid
    fi
    for _ in $(seq 1 30); do grep -qo 'https://[a-z0-9-]*\.trycloudflare\.com' data/cloudflared.log && break; sleep 1; done
    URL_PUBLICA="$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' data/cloudflared.log | head -1)"
    [ -n "$URL_PUBLICA" ] || { echo "cloudflared no dio URL: mira receptor/data/cloudflared.log"; exit 1; }
    ;;
  cloudflare)
    # Túnel con nombre propio (p. ej. monitor.midominio.com). Se crea en el panel de Cloudflare
    # apuntando a http://127.0.0.1:${RECEPTOR_PUERTO}; aquí solo se arranca con su token.
    : "${CLOUDFLARE_TUNNEL_TOKEN:?Falta CLOUDFLARE_TUNNEL_TOKEN}" "${TALLER_URL_PUBLICA:?Falta TALLER_URL_PUBLICA}"
    if ! vivo data/cloudflared.pid; then
      TUNNEL_TOKEN="$CLOUDFLARE_TUNNEL_TOKEN" nohup cloudflared tunnel --no-autoupdate run >> data/cloudflared.log 2>&1 &
      echo $! > data/cloudflared.pid
    fi
    URL_PUBLICA="$TALLER_URL_PUBLICA"
    ;;
  ninguno) ;;
  *) echo "TALLER_EXPONER no válido: $EXPONER"; exit 1 ;;
esac

cat <<EOF

Receptor listo.
  Panel de Phoenix (solo tú):  $PHOENIX_URL
  Para los alumnos:
    URL:    $URL_PUBLICA
    Token:  $TALLER_MONITOR_TOKEN
  Comprobación:  curl $URL_PUBLICA/salud
  Log del receptor:  tail -f $PWD/data/receptor.log
EOF
