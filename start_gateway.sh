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

# Stop old gateway if running
pkill -f "/workspace/LiveTalking/gateway.py" || true
# Stop any orphan workers from previous runs
pkill -f "/workspace/LiveTalking/app.py" || true

nohup /venv/nerfstream/bin/python /workspace/LiveTalking/gateway.py \
  --listenport 8090 \
  --profiles_file /workspace/LiveTalking/worker_profiles.json \
  --idle_timeout 300 \
  > /workspace/LiveTalking/gateway.log 2>&1 &

sleep 1
ss -ltnp | rg ':8090' || true
