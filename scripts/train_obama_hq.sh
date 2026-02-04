#!/usr/bin/env bash
set -euo pipefail

ROOT="/workspace/LiveTalking"
PYTHON="/venv/nerfstream/bin/python"
HF_HOME="${HF_HOME:-/workspace/.hf_home}"

export HF_HOME
export TRANSFORMERS_CACHE="$HF_HOME/hub"

ASR_MODEL="cpierse/wav2vec2-large-xlsr-53-esperanto"
DATA_DIR="ernerf/data/obama"
HEAD_WS="ernerf/obama_eo_head"
TORSO_WS="ernerf/obama_eo_torso"
BG_IMG="ernerf/data/obama/bc.jpg"

mkdir -p "$HF_HOME" "$ROOT/logs" "$ROOT/$HEAD_WS" "$ROOT/$TORSO_WS"

cd "$ROOT"

# 1) Head
"$PYTHON" -m ernerf.main "$DATA_DIR" -O \
  --iters 100000 \
  --workspace "$HEAD_WS" \
  --asr_model "$ASR_MODEL" \
  --bg_img "$BG_IMG"

# 2) Lips fine-tune (LPIPS + landmarks)
"$PYTHON" -m ernerf.main "$DATA_DIR" -O \
  --iters 125000 \
  --workspace "$HEAD_WS" \
  --asr_model "$ASR_MODEL" \
  --bg_img "$BG_IMG" \
  --finetune_lips \
  --patch_size 32

# 3) Torso (freeze head)
"$PYTHON" -m ernerf.main "$DATA_DIR" -O \
  --iters 200000 \
  --workspace "$TORSO_WS" \
  --asr_model "$ASR_MODEL" \
  --bg_img "$BG_IMG" \
  --torso \
  --head_ckpt "$HEAD_WS/checkpoints/ngp.pth"
