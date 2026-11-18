#!/usr/bin/env bash
# Install agent-editor and pre-fetch its local models. Idempotent; run from the repo root.
# macOS (Homebrew) and Linux (apt) supported.
set -euo pipefail

if [[ "$(uname)" == "Darwin" ]]; then
  command -v brew >/dev/null || { echo "Install Homebrew first: https://brew.sh"; exit 1; }
  command -v ffmpeg >/dev/null || brew install ffmpeg
  command -v uv >/dev/null || brew install uv
  # Inter is the caption/title font; libass falls back to a system font without it
  ls ~/Library/Fonts/Inter* /Library/Fonts/Inter* >/dev/null 2>&1 || brew install --cask font-inter || true
else
  command -v ffmpeg >/dev/null || { sudo apt-get update && sudo apt-get install -y ffmpeg; }
  command -v uv >/dev/null || pip install -q uv
fi

[ -d .venv ] || uv venv -q .venv --python 3.12
uv pip install -q --python .venv/bin/python -e ".[dev]"
HF_HUB_DISABLE_XET=1 .venv/bin/python - <<'PY'
from faster_whisper import WhisperModel
from huggingface_hub import hf_hub_download
WhisperModel("large-v3-turbo", device="cpu", compute_type="int8")
hf_hub_download("opencv/face_detection_yunet", "face_detection_yunet_2023mar.onnx")
print("agent-editor ready")
PY
