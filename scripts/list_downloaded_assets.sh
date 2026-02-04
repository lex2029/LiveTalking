#!/usr/bin/env bash
set -euo pipefail

ROOT="/workspace/LiveTalking"
HF_HOME="${HF_HOME:-/workspace/.hf_home}"

printf "== LiveTalking downloaded assets (local) ==\n"

# HuggingFace caches
if [[ -d "${HF_HOME}/hub" ]]; then
  printf "\n[HuggingFace cache]\n"
  ls -1 "${HF_HOME}/hub" | sed 's/^/  - /'
  if [[ -d "${HF_HOME}/hub/models--facebook--wav2vec2-base-960h" ]]; then
    du -sh "${HF_HOME}/hub/models--facebook--wav2vec2-base-960h" | awk '{print "  - facebook/wav2vec2-base-960h: " $1}'
  fi
  if [[ -d "${HF_HOME}/hub/models--cpierse--wav2vec2-large-xlsr-53-esperanto" ]]; then
    du -sh "${HF_HOME}/hub/models--cpierse--wav2vec2-large-xlsr-53-esperanto" | awk '{print "  - cpierse/wav2vec2-large-xlsr-53-esperanto: " $1}'
  fi
else
  printf "\n[HuggingFace cache] not found (%s)\n" "${HF_HOME}/hub"
fi

# ER-NeRF data and checkpoints
printf "\n[ER-NeRF data]\n"
if [[ -d "${ROOT}/ernerf/data/obama" ]]; then
  du -sh "${ROOT}/ernerf/data/obama" | awk '{print "  - data/obama: " $1}'
else
  echo "  - data/obama: MISSING"
fi

if [[ -d "${ROOT}/ernerf/trial_obama_torso/checkpoints" ]]; then
  du -sh "${ROOT}/ernerf/trial_obama_torso/checkpoints" | awk '{print "  - trial_obama_torso/checkpoints: " $1}'
  find "${ROOT}/ernerf/trial_obama_torso/checkpoints" -maxdepth 1 -type f -print0 | xargs -0 ls -lh | awk '{print "    " $9 "  " $5}'
else
  echo "  - trial_obama_torso/checkpoints: MISSING"
fi

if [[ -f "${ROOT}/ernerf/checkpoints.zip" ]]; then
  ls -lah "${ROOT}/ernerf/checkpoints.zip" | awk '{print "  - checkpoints.zip: " $5}'
fi

# Face parsing weights
printf "\n[Face parsing]\n"
if [[ -f "${ROOT}/ernerf/data_utils/face_parsing/79999_iter.pth" ]]; then
  ls -lah "${ROOT}/ernerf/data_utils/face_parsing/79999_iter.pth" | awk '{print "  - 79999_iter.pth: " $5}'
else
  echo "  - 79999_iter.pth: MISSING"
fi

# 3DMM files
printf "\n[3DMM / Face tracking]\n"
if [[ -d "${ROOT}/ernerf/data_utils/face_tracking/3DMM" ]]; then
  du -sh "${ROOT}/ernerf/data_utils/face_tracking/3DMM" | awk '{print "  - 3DMM: " $1}'
  find "${ROOT}/ernerf/data_utils/face_tracking/3DMM" -maxdepth 1 -type f -print0 | xargs -0 ls -lh | awk '{print "    " $9 "  " $5}'
else
  echo "  - 3DMM: MISSING"
fi

# LPIPS weights
printf "\n[LPIPS weights]\n"
if [[ -f "/venv/nerfstream/lib/python3.10/site-packages/lpips/weights/v0.1/alex.pth" ]]; then
  ls -lah /venv/nerfstream/lib/python3.10/site-packages/lpips/weights/v0.1/alex.pth | awk '{print "  - alex.pth: " $5}'
else
  echo "  - alex.pth: MISSING"
fi

printf "\nDone.\n"
