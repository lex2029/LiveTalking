#!/usr/bin/env bash
set -euo pipefail

fail=0

archive_hint() {
  if [ -f "archives/obama_assets.tar.zst" ]; then
    echo "HINT: extract archives/obama_assets.tar.zst -> tar --zstd -xf archives/obama_assets.tar.zst -C ."
  fi
}

check_file() {
  local path="$1"
  if [ ! -f "$path" ]; then
    echo "MISSING: $path"
    fail=1
  else
    echo "OK: $path"
  fi
}

check_dir_count() {
  local dir="$1"
  local expected="$2"
  if [ ! -d "$dir" ]; then
    echo "MISSING: $dir"
    archive_hint
    fail=1
    return
  fi
  local count
  count=$(ls -1 "$dir" | wc -l | tr -d ' ')
  if [ "$count" -lt "$expected" ]; then
    echo "WARN: $dir has $count files (expected >= $expected)"
  else
    echo "OK: $dir has $count files"
  fi
}

check_file "ernerf/obama_eo_head/checkpoints/ngp.pth"
check_file "ernerf/obama_eo_torso/checkpoints/ngp.pth"
check_file "ernerf/data/obama/transforms_train.json"
check_file "ernerf/data/obama/transforms_val.json"
check_file "ernerf/data/obama/au.csv"
check_file "ernerf/data/obama/bc.jpg"

check_dir_count "ernerf/data/obama/gt_imgs" 7000
check_dir_count "ernerf/data/obama/torso_imgs" 7000

check_file "data/customvideo/idle/audio.wav"
check_dir_count "data/customvideo/idle/img" 30

if [ "$fail" -ne 0 ]; then
  echo "FAIL: missing required assets"
  exit 1
fi

echo "All required assets are present."
