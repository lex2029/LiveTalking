#!/usr/bin/env bash
set -euo pipefail

LOG_DIR="/workspace/LiveTalking/logs"
mkdir -p "$LOG_DIR"

{
  echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] autostart begin"
  echo "PATH=$PATH"

  if ! pgrep -f "/workspace/LiveTalking/gateway.py" >/dev/null 2>&1; then
    echo "starting gateway"
    /workspace/LiveTalking/start_gateway.sh
  else
    echo "gateway already running"
  fi

  if ! pgrep -f "cloudflared.*tunnel run liveavatar" >/dev/null 2>&1; then
    echo "starting cloudflared liveavatar tunnel"
    nohup cloudflared --config /root/.cloudflared/config.yml tunnel run liveavatar \
      >> "$LOG_DIR/cloudflared-liveavatar.log" 2>&1 &
  else
    echo "cloudflared already running"
  fi

  echo "autostart done"
} >> "$LOG_DIR/autostart.log" 2>&1
