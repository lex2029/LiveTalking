#!/usr/bin/env bash
set -euo pipefail

exec cloudflared --config /root/.cloudflared/config.yml tunnel run liveavatar \
  >> /workspace/LiveTalking/logs/cloudflared-liveavatar.log 2>&1
