#!/usr/bin/env bash
set -euo pipefail

export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/opt/instance-tools/bin:/venv/main/bin"

LOG_DIR="/workspace/LiveTalking/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/autostart.log"

log() {
  echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] $*" >> "$LOG_FILE"
}

start_gateway_tmux() {
  if tmux has-session -t liveavatar-gateway 2>/dev/null; then
    return
  fi
  log "starting tmux liveavatar-gateway"
  tmux new-session -d -s liveavatar-gateway "/workspace/LiveTalking/scripts/run_gateway_forever.sh"
}

start_cloudflared_tmux() {
  if tmux has-session -t liveavatar-cloudflared 2>/dev/null; then
    return
  fi
  log "starting tmux liveavatar-cloudflared"
  tmux new-session -d -s liveavatar-cloudflared "/workspace/LiveTalking/scripts/run_cloudflared_forever.sh"
}

# Restart gateway session if port/health is down
if ! ss -ltnp 2>/dev/null | grep -q ':8090'; then
  tmux kill-session -t liveavatar-gateway 2>/dev/null || true
  start_gateway_tmux
else
  if ! curl -sSf http://127.0.0.1:8090/health >/dev/null 2>&1; then
    tmux kill-session -t liveavatar-gateway 2>/dev/null || true
    start_gateway_tmux
  else
    start_gateway_tmux
  fi
fi

start_cloudflared_tmux
