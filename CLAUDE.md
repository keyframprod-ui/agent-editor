# Agent Editor — instructions for Claude

This repo is an agentic video/film editor. When the user asks you to edit video, **you are the editor**:
drive the `agent-editor` MCP server (configured in `.mcp.json`) and follow
`src/agent_editor/EDITING_GUIDE.md` (also served as MCP resource `guide://editing`). Read the guide first.

## Setup
- `bash scripts/setup.sh` — venv, deps, Whisper large-v3-turbo + YuNet face model (CPU).
- Projects live in `projects/<name>/` (git-ignored). Open one with the `open_project` tool.
- Footage from Google Drive: download with the Drive connector into `projects/<name>/media/`, then `ingest`.
  Deliver renders back to Drive the same way.

## Workflow (see guide for craft)
open_project → ingest → analyze_media → shot_sheet (LOOK) + transcript → new_timeline →
add_phrase/add_words/add_shot/beat_montage → tighten_clip / split_edit / freeze_patch →
add_broll / add_text / add_audio → set_grade / set_captions → render(preview) → review (LOOK, read QC) →
fix → render(final) → export_nle → review_page.

## Code map
- `core.py` project/media registry, ffprobe · `analyze.py` shots, Whisper, audio/beat grid, YuNet faces, AI shot log, search
- `timeline.py` JSON timeline model + edit ops (versioned, undo) · `render.py` FFmpeg renderer (segment cache, xfade, overlays, ducking, ASS captions, two-pass loudness)
- `captions.py` ASS/SRT · `review.py` contact sheets, QC, pacing, flash-shot detection, HTML review page · `export.py` OTIO/FCPXML/XML/EDL
- `mcp_server.py` MCP tools · `agent.py` standalone tool-runner agent (needs ANTHROPIC_API_KEY) · `cli.py` `ae` CLI

## Conventions
- Times are seconds (floats). Media ids are slugs. Every timeline save writes `timelines/.history/`.
- Run `pytest -q` after engine changes (synthetic media, ~15 s).
- Network: Hugging Face downloads need `HF_HUB_DISABLE_XET=1` in this environment.
