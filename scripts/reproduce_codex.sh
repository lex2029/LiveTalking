#!/usr/bin/env bash
set -euo pipefail

ROOT="/workspace/LiveTalking"
VENV="/venv/nerfstream"
HF_HOME="${HF_HOME:-/workspace/.hf_home}"

export HF_HOME
export TRANSFORMERS_CACHE="$HF_HOME/hub"

mkdir -p "$HF_HOME" "$ROOT/logs"

printf "== LiveTalking reproducible setup (Codex) ==\n"

# 1) Python venv + deps
if [[ ! -d "$VENV" ]]; then
  python -m venv "$VENV"
fi
source "$VENV/bin/activate"

pip install -U pip
pip install -r "$ROOT/requirements.txt"

# 2) HuggingFace model (ASR)
python - <<'PY'
from transformers import AutoProcessor, AutoModelForCTC
model = "facebook/wav2vec2-base-960h"
AutoProcessor.from_pretrained(model)
AutoModelForCTC.from_pretrained(model)
print("Downloaded:", model)
PY

# 3) LPIPS weights (used by renderer)
python - <<'PY'
import lpips
_ = lpips.LPIPS(net='alex')
print("LPIPS alex weights ready")
PY

# 4) Data + checkpoints (optional downloads)
#    If you already have these in the repo, this step will skip.

# Obama dataset
if [[ ! -d "$ROOT/ernerf/data/obama" ]]; then
  if [[ -n "${OBAMA_DATA_URL:-}" ]]; then
    echo "Downloading Obama dataset..."
    mkdir -p "$ROOT/ernerf/data"
    curl -L "$OBAMA_DATA_URL" -o /tmp/obama_data.tgz
    tar -xzf /tmp/obama_data.tgz -C "$ROOT/ernerf/data"
  else
    echo "[WARN] ernerf/data/obama missing. Set OBAMA_DATA_URL to auto-download or place the dataset manually."
  fi
fi

# ER-NeRF checkpoints
if [[ ! -f "$ROOT/ernerf/trial_obama_torso/checkpoints/ngp.pth" ]]; then
  if [[ -n "${ERNERF_CKPT_URL:-}" ]]; then
    echo "Downloading ER-NeRF checkpoints..."
    mkdir -p "$ROOT/ernerf/trial_obama_torso/checkpoints"
    curl -L "$ERNERF_CKPT_URL" -o /tmp/ernerf_ckpt.zip
    unzip -o /tmp/ernerf_ckpt.zip -d "$ROOT/ernerf/trial_obama_torso/checkpoints"
  else
    echo "[WARN] ER-NeRF checkpoint missing. Set ERNERF_CKPT_URL or place ngp.pth manually."
  fi
fi

# Face parsing weights
if [[ ! -f "$ROOT/ernerf/data_utils/face_parsing/79999_iter.pth" ]]; then
  if [[ -n "${FACE_PARSING_URL:-}" ]]; then
    echo "Downloading face parsing weights..."
    mkdir -p "$ROOT/ernerf/data_utils/face_parsing"
    curl -L "$FACE_PARSING_URL" -o "$ROOT/ernerf/data_utils/face_parsing/79999_iter.pth"
  else
    echo "[WARN] face_parsing/79999_iter.pth missing. Set FACE_PARSING_URL or place it manually."
  fi
fi

# Basel Face Model (restricted)
if [[ ! -f "$ROOT/ernerf/data_utils/face_tracking/3DMM/01_MorphableModel.mat" ]]; then
  if [[ -n "${BFM_URL:-}" && -n "${BFM_USER:-}" && -n "${BFM_PASS:-}" ]]; then
    echo "Downloading BFM (restricted)..."
    mkdir -p "$ROOT/ernerf/data_utils/face_tracking/3DMM"
    curl -L -u "$BFM_USER:$BFM_PASS" "$BFM_URL" -o /tmp/BaselFaceModel.tgz
    tar -xzf /tmp/BaselFaceModel.tgz -C "$ROOT/ernerf/data_utils/face_tracking/3DMM" --strip-components=1
  else
    echo "[WARN] BFM missing. Set BFM_URL/BFM_USER/BFM_PASS or place 3DMM files manually."
  fi
fi

# 5) Secrets (manual)
if [[ ! -f "$ROOT/keys.json" ]]; then
  echo "[WARN] keys.json missing. Copy keys.example.json to keys.json and fill API keys."
fi

if [[ ! -f "$ROOT/daily.env" ]]; then
  echo "[WARN] daily.env missing. Copy daily.example.env to daily.env and fill Daily keys."
fi

# 6) Start gateway
if [[ -x "$ROOT/start_gateway.sh" ]]; then
  "$ROOT/start_gateway.sh"
else
  echo "[WARN] start_gateway.sh not found."
fi

printf "\nSetup complete. Open http://127.0.0.1:8090/dashboard.html\n"
