# Agent Editor

An AI editor for video and film. Claude logs your footage, builds the cut, watches its own renders, fixes what it
sees, and delivers both finished masters **and** a timeline you can open in Premiere, DaVinci Resolve or Final Cut.

```
footage ─▶ analyze ─────────────▶ timeline (JSON, versioned) ─▶ render ─▶ review ─┐
           shots · transcript        A-roll · B-roll · titles     FFmpeg     sheet │
           beats · loudness · faces  music · captions · grade               QC    │
                 ▲                                                      pacing    │
                 └──────────────── Claude edits, looks, iterates ◀────────────────┘
                                                      └─▶ final / ProRes master · OTIO / XML / EDL
```

## What it can do

| Area | Capabilities |
|---|---|
| **Logging** | Shot detection (PySceneDetect), word-timed transcripts (Whisper large-v3-turbo, local), loudness / silences, tempo + beat & downbeat grid, face tracks (YuNet), per-shot contact sheets, AI shot logs (Claude vision), search across dialogue + shots |
| **Editing** | Text-based editing (cut by phrase or word range), jump-cut tightening + filler removal, L/J split edits, freeze-frame patches, beat-synced montage, B-roll & picture-in-picture, titles / lower thirds / text, music · VO · SFX with auto-ducking, exact-runtime fitting for ads, multi-aspect derivation (16:9 → 9:16 / 1:1 / 4:5), full undo history |
| **Picture** | Auto-reframe that follows faces, blur-fill, punch-ins & slow push-ins, Ken Burns on stills, 26 transition types, named looks (kodak_2383, teal_orange, bleach_bypass, noir, …), .cube LUTs, grain / vignette / sharpen |
| **Sound** | Two-pass EBU R128 loudness to target (−14 social, −23 broadcast…), sidechain ducking, de-click at edit points, limiter |
| **Captions** | Burned-in word-pop / karaoke / clean / film subtitle styles, spelling corrections, SRT sidecars |
| **Review** | Agent "watches" renders via contact sheets; QC for black / frozen frames, silence, clipping, loudness, flash shots; pacing stats (cuts/min, WPM, hook words); HTML review page |
| **Delivery** | H.264, HEVC, ProRes 422 HQ master, VP9; OpenTimelineIO (Resolve imports directly), FCP7 XML (Premiere / Resolve), CMX 3600 EDL, FCPXML (experimental) |

## Three ways to drive it

1. **Claude Code (no API key):** open this repo — `.mcp.json` registers the MCP server and `CLAUDE.md` tells
   Claude how to edit. Just ask: *"Cut a 30 s vertical reel from the interview in projects/demo."*
2. **Standalone agent:** `ae agent projects/demo "45s reel, strongest story, captions, music bed"` (needs `ANTHROPIC_API_KEY`).
3. **CLI / Python:** `ae ingest`, `ae analyze`, `ae render`, `ae export`, or `agent_editor.timeline` directly.

## Install

```bash
bash scripts/setup.sh        # venv + deps + local models (CPU is fine; GPU not required)
.venv/bin/pytest -q          # engine tests on synthetic media
```

Requires FFmpeg ≥ 6 with libx264/libass. Python 3.11+.

## Notes
- Runs on CPU: Whisper turbo transcribes ~3.5× faster than real time on 4 cores; 1080p final renders ≈ 3× real time.
- Model downloads from Hugging Face; set `HF_HUB_DISABLE_XET=1` behind restrictive proxies.
- You are responsible for rights to the footage and music you edit.
