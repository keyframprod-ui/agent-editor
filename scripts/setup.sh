#!/usr/bin/env bash
# Install agent-editor and pre-fetch its local models. Idempotent; run from the repo root.
set -euo pipefail
command -v ffmpeg >/dev/null || { sudo apt-get update && sudo apt-get install -y ffmpeg; }
command -v uv >/dev/null || pip install -q uv
[ -d .venv ] || uv venv -q .venv --python 3.12
uv pip install -q --python .venv/bin/python -e ".[dev]"
HF_HUB_DISABLE_XET=1 .venv/bin/python - <<'PY'
from faster_whisper import WhisperModel
from huggingface_hub import hf_hub_download
WhisperModel("large-v3-turbo", device="cpu", compute_type="int8")
hf_hub_download("opencv/face_detection_yunet", "face_detection_yunet_2023mar.onnx")
print("agent-editor ready")
PY
