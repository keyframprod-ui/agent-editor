"""Standalone editing agent (Claude API tool runner) — needs ANTHROPIC_API_KEY.

Reuses every MCP tool as a Claude tool, so the agent and any MCP client share one toolset.
The loop runs locally, next to the footage and FFmpeg.

    ae agent projects/myfilm "Cut a 45s vertical reel from the interview: strongest story, captions, music bed"
"""
from __future__ import annotations

import base64
import functools
import inspect
import os
from pathlib import Path
from typing import Any

import anthropic
from anthropic import beta_tool

from . import mcp_server as M
from .core import EditorError

TOOL_NAMES = [
    "bin", "media_info", "ingest", "analyze_media", "transcript", "transcript_words", "find_phrase", "search", "shots",
    "shot_sheet", "frame", "save_shot_log", "music_info", "new_timeline", "derive_timeline", "show_timeline",
    "list_timelines", "add_clip", "add_words", "add_phrase", "add_shot", "remove_item", "move_clip", "trim_clip",
    "split_clip", "set_transition", "tighten_clip", "beat_montage", "split_edit", "freeze_patch", "add_broll",
    "add_text", "add_audio", "update_item", "set_grade", "set_captions", "fit_duration", "undo", "timeline_history",
    "render", "review", "review_page", "export_nle",
]

SYSTEM = """You are a senior film & video editor working autonomously in a non-destructive editing engine.
You have no GUI: you see footage through shot sheets / frames, hear it through transcripts and audio analysis,
and you watch your own cuts through `review` (contact sheet + QC + pacing). Follow the editing guide below.
Work in phases (log → select → assemble → refine → polish → review → deliver) and always run at least one
render(preview) → review → fix cycle before render(final). Finish with a short report: deliverables (paths),
key creative choices, and anything a human should verify.

<editing_guide>
{guide}
</editing_guide>"""


def _to_blocks(res: Any):
    """Convert MCP-style results (str / Image / list) into tool_result content."""
    items = res if isinstance(res, list) else [res]
    out = []
    for it in items:
        if isinstance(it, M.Image):
            path = Path(it.path) if getattr(it, "path", None) else None
            data = path.read_bytes() if path else it.data
            mt = "image/png" if path and path.suffix == ".png" else "image/jpeg"
            out.append({"type": "image", "source": {"type": "base64", "media_type": mt, "data": base64.b64encode(data).decode()}})
        else:
            out.append({"type": "text", "text": it if isinstance(it, str) else str(it)})
    if len(out) == 1 and out[0]["type"] == "text":
        return out[0]["text"]
    return out


def _wrap(fn):
    @functools.wraps(fn)
    def w(*a, **kw):
        try:
            return _to_blocks(fn(*a, **kw))
        except EditorError as e:
            return f"ERROR: {e}"
        except Exception as e:  # surface tool failures to the model instead of crashing the run
            return f"ERROR ({type(e).__name__}): {e}"
    w.__signature__ = inspect.signature(fn)
    return beta_tool(w)


def build_tools():
    return [_wrap(getattr(M, n)) for n in TOOL_NAMES]


def run(project: str, brief: str, model: str | None = None, effort: str = "high", max_iterations: int = 120,
        verbose: bool = True) -> str:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise EditorError("ANTHROPIC_API_KEY is not set. Inside Claude Code, use the MCP server instead (no key needed).")
    M.open_project(project)
    guide = Path(__file__).with_name("EDITING_GUIDE.md").read_text()
    client = anthropic.Anthropic()
    runner = client.beta.messages.tool_runner(
        model=model or os.environ.get("AE_AGENT_MODEL", "claude-opus-5-5"),
        max_tokens=16000,
        max_iterations=max_iterations,
        system=SYSTEM.format(guide=guide),
        tools=build_tools(),
        thinking={"type": "adaptive"},
        output_config={"effort": effort},
        # refusal fallback (routes by category) + clear old tool results/images so long sessions fit
        betas=["server-side-fallback-2026-07-01", "context-management-2025-06-27"],
        fallbacks="default",
        context_management={"edits": [{"type": "clear_tool_uses_20250919"}]},
        messages=[{"role": "user", "content": f"Project: {project}\n\nBrief:\n{brief}\n\nCurrent bin:\n{M.bin()}"}],
    )
    final_text = ""
    for message in runner:
        for block in message.content:
            if block.type == "text" and block.text.strip():
                final_text = block.text
                if verbose:
                    print(f"\n🎬 {block.text}")
            elif block.type == "tool_use" and verbose:
                args = ", ".join(f"{k}={str(v)[:60]}" for k, v in (block.input or {}).items())
                print(f"  → {block.name}({args})")
        if message.stop_reason == "refusal":
            final_text = "The request was declined by the model's safety system."
            break
    return final_text
