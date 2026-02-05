#!/usr/bin/env bash
set -euo pipefail

if [ -f /workspace/LiveTalking/daily.env ]; then
  source /workspace/LiveTalking/daily.env
fi

# Limit CPU thread sprawl
export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1

# Stop old gateway/workers if any
pkill -f "/workspace/LiveTalking/gateway.py" || true
pkill -f "/workspace/LiveTalking/app.py" || true

exec /venv/nerfstream/bin/python /workspace/LiveTalking/gateway.py \
  --listenport 8090 \
  --profiles_file /workspace/LiveTalking/worker_profiles.json \
  --idle_timeout 300 \
  >> /workspace/LiveTalking/gateway.log 2>&1
