#!/usr/bin/env bash
# Para la exposición pública, el receptor y Phoenix. Los datos quedan en data/.
set -uo pipefail
cd "$(dirname "$0")"
FUNNEL="${TALLER_FUNNEL_PUERTO:-10000}"
if [ "${TALLER_EXPONER:-tailscale}" = "tailscale" ]; then
  # Solo si hay un Funnel en ese puerto; con timeout, porque sin autorización el comando espera.
  if tailscale funnel status 2>/dev/null | grep -q ":${FUNNEL} (Funnel on)"; then
    timeout 15 tailscale funnel --https="$FUNNEL" off >/dev/null 2>&1 && echo "Funnel :$FUNNEL cerrado"
  fi
fi
for p in cloudflared receptor phoenix; do
  if [ -s "data/$p.pid" ] && kill -0 "$(cat "data/$p.pid")" 2>/dev/null; then
    kill "$(cat "data/$p.pid")" && echo "$p parado"
  fi
  rm -f "data/$p.pid"
done
