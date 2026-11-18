"""MCP server: exposes the editor as tools any MCP client (Claude Code, Claude Desktop, Agent SDK) can drive.

Run:  agent-editor-mcp            (stdio)
Env:  AE_PROJECT=/path/to/project (default ./projects/default)
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal, Optional

from mcp.server.mcpserver import Image, MCPServer

from . import analyze, export as exp, render as rnd, review as rev, timeline as T
from .core import AUDIO_EXT, IMAGE_EXT, VIDEO_EXT, EditorError, Project

INSTRUCTIONS = """Agent Editor — a professional, non-destructive video/film editing engine.
Workflow: open_project → ingest → analyze (shots, transcript, audio) → look at shot_sheet images & transcripts →
new_timeline → build the cut (add_phrase/add_words for dialogue, add_shot for visuals, beat_montage for music-driven) →
tighten dialogue → add_broll / add_text / add_audio → set_grade / set_captions → render(preview) → review (LOOK at the
sheet, read QC + pacing) → iterate → render(final) → export for NLE. Times are seconds. Every edit is versioned (undo).
Read the editorial guide resource `guide://editing` before cutting."""

mcp = MCPServer("agent-editor", instructions=INSTRUCTIONS)
_state: dict[str, Any] = {"project": None}


def P() -> Project:
    if _state["project"] is None:
        _state["project"] = Project.open(os.environ.get("AE_PROJECT", "projects/default"))
    return _state["project"]


def _edit(timeline: str, fn, message: str):
    p = P()
    tl = T.load(p, timeline)
    res = fn(p, tl)
    T.save(p, tl, message)
    return res, tl


def _ok(obj: Any) -> str:
    return obj if isinstance(obj, str) else json.dumps(obj, indent=1, default=str)


# --------------------------------------------------------------------------- project & media
@mcp.tool()
def open_project(path: str) -> str:
    """Open (or create) a project directory. All media, analysis, timelines and renders live inside it."""
    _state["project"] = Project.open(path)
    return f"project: {_state['project'].root}\n" + analyze.overview(_state["project"])


@mcp.tool()
def ingest(sources: list[str], tags: list[str] | None = None, copy: bool = False) -> str:
    """Add media files (local paths, directories, or http(s) URLs) to the project bin."""
    p = P(); out = []
    for s in sources:
        paths = [s]
        if not s.startswith("http") and Path(s).is_dir():
            paths = [str(x) for x in sorted(Path(s).iterdir()) if x.suffix.lower() in VIDEO_EXT | AUDIO_EXT | IMAGE_EXT]
        for x in paths:
            try:
                r = p.ingest(x, tags=tags, copy=copy)
                out.append(f"{r['id']}: {r['kind']} {r['duration']:.1f}s {r.get('width', '')}x{r.get('height', '')} {r.get('fps', '')}fps")
            except Exception as e:
                out.append(f"FAILED {x}: {e}")
    return "\n".join(out)


@mcp.tool()
def bin() -> str:
    """List all media in the project with analysis status (like an NLE bin)."""
    return analyze.overview(P())


@mcp.tool()
def media_info(media: str) -> str:
    """Full technical metadata for one media item."""
    return _ok(P().media(media))


# --------------------------------------------------------------------------- analysis
@mcp.tool()
def analyze_media(media: list[str], transcribe: bool = True, faces: bool = False) -> str:
    """Run shot detection, audio analysis (loudness, beats, silences) and Whisper transcription. Cached."""
    return _ok([analyze.analyze_all(P(), m, transcribe, faces) for m in media])


@mcp.tool()
def transcript(media: str, with_times: bool = True) -> str:
    """Transcript by segment with timecodes and word-index ranges (use word indices with add_words)."""
    return analyze.transcript_text(P(), media, with_times)


@mcp.tool()
def transcript_words(media: str, start: float = 0, end: float = 1e9) -> str:
    """Word-level timings in a time window: 'index word start-end'."""
    t = analyze.transcribe(P(), media)
    return "\n".join(f"{w['i']} {w['w']} {w['start']:.2f}-{w['end']:.2f}" for w in t["words"] if start <= w["start"] <= end)


@mcp.tool()
def find_phrase(media: str, phrase: str) -> str:
    """Locate a phrase in a media's transcript → word ranges + times + context."""
    return _ok(analyze.find_phrase(P(), media, phrase))


@mcp.tool()
def search(query: str) -> str:
    """Search dialogue and shot logs across the whole bin."""
    return _ok(analyze.search(P(), query))


@mcp.tool()
def shots(media: str) -> str:
    """Detected shots (index, start, end, duration) plus the AI shot log if one exists."""
    p = P()
    sc = analyze.scenes(p, media)
    log = {s["index"]: s for s in (p.cache_get(media, "shotlog") or {}).get("shots", [])}
    return "\n".join(f"#{s['index']} {s['start']:.2f}-{s['end']:.2f} ({s['duration']:.1f}s)"
                     + (f"  {log[s['index']].get('shot_size')} | {log[s['index']].get('subjects')} | {log[s['index']].get('action')}"
                        if s["index"] in log else "") for s in sc)


@mcp.tool()
def shot_sheet(media: str) -> Image:
    """Contact sheet image: one labelled frame per shot. LOOK at it to understand the footage."""
    return Image(path=str(analyze.scene_sheet(P(), media)))


@mcp.tool()
def frame(media: str, t: float, width: int = 960) -> Image:
    """A single source frame at time t (seconds)."""
    return Image(path=str(analyze.frame_at(P(), media, t, width)))


@mcp.tool()
def shot_log(media: str) -> str:
    """AI shot log via Claude vision (needs ANTHROPIC_API_KEY). Without a key: view shot_sheet and call save_shot_log."""
    return _ok(analyze.describe_shots(P(), media))


@mcp.tool()
def save_shot_log(media: str, shots: list[dict]) -> str:
    """Save your own shot log: [{index, shot_size, subjects, action, setting, mood, quality_issues, b_roll_value, hero_value, tags}]."""
    analyze.save_shot_log(P(), media, shots)
    return f"saved {len(shots)} shots"


@mcp.tool()
def music_info(media: str) -> str:
    """Tempo, beat and downbeat times, energy curve (0.5 s bins) and loudness of an audio/music file."""
    a = analyze.audio(P(), media)
    return _ok({k: (v[:64] if isinstance(v, list) and len(v) > 64 else v) for k, v in a.items()} | {"n_beats": len(a.get("beats", []))})


# --------------------------------------------------------------------------- timelines
@mcp.tool()
def new_timeline(name: str = "main", preset: str = "youtube", width: Optional[int] = None, height: Optional[int] = None,
                 fps: Optional[float] = None) -> str:
    """Create a timeline. Presets: vertical/reel/tiktok/shorts (1080x1920), youtube/hd (1920x1080), uhd/4k, square,
    portrait45 (1080x1350), cinema (2048x858 2.39:1), scope, flat."""
    tl = T.new(P(), name, preset, width, height, fps)
    return f"created '{tl.name}' {tl.width}x{tl.height}@{tl.fps}"


@mcp.tool()
def derive_timeline(source: str, name: str, preset: str, reframe: Literal["auto", "fill", "blur_fill", "fit"] = "auto") -> str:
    """Copy a timeline into a new aspect ratio (e.g. 16:9 master → 9:16 / 1:1 / 4:5 versions) with auto-reframing."""
    p = P(); src = T.load(p, source)
    w, h, fps = T.PRESETS[preset]
    tl = src.model_copy(deep=True); tl.name, tl.width, tl.height, tl.version = name, w, h, 0
    for c in tl.clips:
        c.reframe = reframe
    for o in tl.overlays:
        if o.kind == "media" and o.full_frame:
            o.reframe = reframe if reframe != "fit" else "fill"
    if h > w:
        tl.captions.enabled = True
    T.save(p, tl, f"derived from {source} as {preset}")
    return T.describe(p, tl, words=False)


@mcp.tool()
def show_timeline(timeline: str = "main", words: bool = True) -> str:
    """The cut as an EDL: clips with timeline/source timecodes and the dialogue they contain, overlays, audio."""
    p = P()
    return T.describe(p, T.load(p, timeline), words)


@mcp.tool()
def list_timelines() -> str:
    return "\n".join(x.stem for x in (P().root / "timelines").glob("*.json"))


@mcp.tool()
def add_clip(media: str, src_in: float, src_out: float, timeline: str = "main", position: Optional[int] = None,
             transition: str = "cut", transition_duration: float = 0.0, reframe: str = "fill", speed: float = 1.0,
             mute: bool = False, label: str = "") -> str:
    """Append (or insert at index) a source range to the A-roll storyline."""
    c, tl = _edit(timeline, lambda p, tl: T.add_clip(p, tl, media, src_in, src_out, position, transition, transition_duration,
                                                      reframe=reframe, speed=speed, mute=mute, label=label), f"add_clip {media}")
    return f"added {c.id} ({c.duration:.2f}s). timeline now {tl.duration:.2f}s"


@mcp.tool()
def add_words(media: str, word_start: int, word_end: int, timeline: str = "main", position: Optional[int] = None,
              label: str = "") -> str:
    """Text-based edit: add the exact span of transcript words [word_start..word_end] (indices from transcript)."""
    c, tl = _edit(timeline, lambda p, tl: T.add_words(p, tl, media, word_start, word_end, position, label=label), "add_words")
    return f"added {c.id} {c.src_in:.2f}-{c.src_out:.2f}: “{T.source_words_text(P(), media, c.src_in, c.src_out)}”"


@mcp.tool()
def add_phrase(media: str, phrase_start: str, phrase_end: Optional[str] = None, occurrence: int = 0, timeline: str = "main",
               position: Optional[int] = None, label: str = "") -> str:
    """Text-based edit: add the line from `phrase_start` through `phrase_end` (quote a few words of each)."""
    c, tl = _edit(timeline, lambda p, tl: T.add_phrase(p, tl, media, phrase_start, phrase_end, occurrence, position, label=label), "add_phrase")
    return f"added {c.id} {c.src_in:.2f}-{c.src_out:.2f}: “{T.source_words_text(P(), media, c.src_in, c.src_out)}”"


@mcp.tool()
def add_shot(media: str, shot_index: int, timeline: str = "main", head_trim: float = 0.0, tail_trim: float = 0.0,
             max_duration: Optional[float] = None, position: Optional[int] = None, mute: bool = False, reframe: str = "fill") -> str:
    """Add a detected shot (by index) to the A-roll, optionally trimmed / capped to max_duration around its middle."""
    c, tl = _edit(timeline, lambda p, tl: T.add_shot(p, tl, media, shot_index, head_trim, tail_trim, max_duration, position,
                                                      mute=mute, reframe=reframe), "add_shot")
    return f"added {c.id} ({c.duration:.2f}s)"


@mcp.tool()
def remove_item(item_id: str, timeline: str = "main") -> str:
    """Remove a clip, overlay or audio item."""
    _edit(timeline, lambda p, tl: T.remove(tl, item_id), f"remove {item_id}")
    return "removed"


@mcp.tool()
def move_clip(clip_id: str, new_index: int, timeline: str = "main") -> str:
    _edit(timeline, lambda p, tl: T.move(tl, clip_id, new_index), f"move {clip_id}")
    return "moved"


@mcp.tool()
def trim_clip(clip_id: str, head: float = 0.0, tail: float = 0.0, timeline: str = "main") -> str:
    """Trim seconds off the head/tail of a clip (negative values extend into handles)."""
    c, _ = _edit(timeline, lambda p, tl: T.trim(p, tl, clip_id, head, tail), f"trim {clip_id}")
    return f"{c.id} now {c.src_in:.2f}-{c.src_out:.2f}"


@mcp.tool()
def split_clip(clip_id: str, at_source_time: float, timeline: str = "main") -> str:
    a, _ = _edit(timeline, lambda p, tl: T.split(tl, clip_id, at_source_time), f"split {clip_id}")
    return f"split into {a[0].id}, {a[1].id}"


@mcp.tool()
def set_transition(clip_id: str, type: str = "dissolve", duration: float = 0.5, timeline: str = "main") -> str:
    """Transition INTO clip_id. Types: cut, dissolve, fade, fadeblack, fadewhite, wipeleft, slideleft, smoothleft, zoomin, circleopen, ..."""
    _edit(timeline, lambda p, tl: T.set_transition(tl, clip_id, type, duration), f"transition {clip_id}")
    return "ok"


@mcp.tool()
def tighten_clip(clip_id: str, max_gap: float = 0.35, remove_fillers: bool = True, aggressive: bool = False,
                 timeline: str = "main") -> str:
    """Jump-cut a talking-head clip on pauses > max_gap s and remove ums/uhs (aggressive also: like, you know, I mean)."""
    ids, tl = _edit(timeline, lambda p, tl: T.tighten(p, tl, clip_id, max_gap, 0.1, remove_fillers, aggressive), f"tighten {clip_id}")
    return f"split into {len(ids)} clips; timeline now {tl.duration:.2f}s"


@mcp.tool()
def beat_montage(shots: list[dict], music: str, beats_per_cut: list[int] | int = 2, music_start: float = 0.0,
                 timeline: str = "main", music_gain_db: float = 0.0) -> str:
    """Cut shots to the music's beat grid. shots: [{media, shot?} | {media, start, end}]. beats_per_cut may vary, e.g. [4,4,2,2,1,1]."""
    ids, tl = _edit(timeline, lambda p, tl: T.beat_montage(p, tl, shots, music, beats_per_cut, music_start, music_gain_db=music_gain_db), "beat_montage")
    return f"added {len(ids)} beat-cut clips; timeline {tl.duration:.2f}s"


@mcp.tool()
def split_edit(clip_id: str, seconds: float, kind: Literal["l_cut", "j_cut"] = "l_cut", timeline: str = "main") -> str:
    """L-cut: this clip's picture runs `seconds` over the next clip's sound. J-cut: this clip's sound starts `seconds`
    before its picture. The bread and butter of invisible dialogue editing."""
    fn = T.l_cut if kind == "l_cut" else T.j_cut
    o, _ = _edit(timeline, lambda p, tl: fn(p, tl, clip_id, seconds), f"{kind} {clip_id}")
    return f"added {kind} overlay {o.id}"


@mcp.tool()
def freeze_patch(clip_id: str, seconds: float, from_head: bool = True, at_src: Optional[float] = None, timeline: str = "main") -> str:
    """Cover a flash frame / bad head or tail of a clip with a held frame while keeping its audio (e.g. the first word
    is spoken over a different camera angle)."""
    o, _ = _edit(timeline, lambda p, tl: T.freeze_patch(p, tl, clip_id, seconds, from_head, at_src), f"freeze {clip_id}")
    return f"added freeze patch {o.id}"


@mcp.tool()
def add_broll(media: str, start: float, duration: float, src_in: float = 0.0, timeline: str = "main", full_frame: bool = True,
              x: float = 0.5, y: float = 0.5, scale: float = 0.35, fade_in: float = 0.0, fade_out: float = 0.0,
              speed: float = 1.0, audio_gain_db: Optional[float] = None, reframe: str = "fill", label: str = "") -> str:
    """Overlay B-roll (full frame cutaway, or picture-in-picture when full_frame=False) at timeline time `start`."""
    o, _ = _edit(timeline, lambda p, tl: T.add_overlay(p, tl, kind="media", media=media, start=start, duration=duration, src_in=src_in,
                                                        full_frame=full_frame, x=x, y=y, scale=scale, fade_in=fade_in, fade_out=fade_out,
                                                        speed=speed, audio_gain_db=audio_gain_db, reframe=reframe, label=label), "add_broll")
    return f"added overlay {o.id}"


@mcp.tool()
def add_text(text: str, start: float, duration: float, kind: Literal["title", "lower_third", "text"] = "text", subtext: str = "",
             timeline: str = "main", x: float = 0.5, y: float = 0.5, style: dict | None = None, fade_in: float = 0.25,
             fade_out: float = 0.25) -> str:
    """Titles, lower thirds (name + role) or free text. style: {color:'#hex', size:int, x, y}."""
    o, _ = _edit(timeline, lambda p, tl: T.add_overlay(p, tl, kind=kind, text=text, subtext=subtext, start=start, duration=duration,
                                                        x=x, y=y, style=style or {}, fade_in=fade_in, fade_out=fade_out), "add_text")
    return f"added {kind} {o.id}"


@mcp.tool()
def add_audio(media: str, start: float = 0.0, src_in: float = 0.0, duration: Optional[float] = None, gain_db: float = 0.0,
              fade_in: float = 0.0, fade_out: float = 1.0, duck: bool = True,
              role: Literal["music", "vo", "sfx", "ambience", "dialogue"] = "music", timeline: str = "main") -> str:
    """Place music / VO / SFX / ambience. duck=True lowers it automatically under A-roll dialogue."""
    a, _ = _edit(timeline, lambda p, tl: T.add_audio(p, tl, media=media, start=start, src_in=src_in, duration=duration, gain_db=gain_db,
                                                      fade_in=fade_in, fade_out=fade_out, duck=duck, role=role), "add_audio")
    return f"added audio {a.id}"


@mcp.tool()
def update_item(item_id: str, fields: dict, timeline: str = "main") -> str:
    """Patch any field on a clip/overlay/audio item, or item_id='timeline'|'captions'|'grade'.
    e.g. {"speed":0.5}, {"reframe":"auto"}, {"zoom":1.0,"zoom_to":1.12}, {"gain_db":-3}, {"loudness_target":-23}."""
    r, _ = _edit(timeline, lambda p, tl: T.update(tl, item_id, fields), f"update {item_id} {list(fields)}")
    return _ok(r)


@mcp.tool()
def set_grade(timeline: str = "main", look: Optional[str] = None, lut: Optional[str] = None, exposure: float = 0.0,
              contrast: float = 1.0, saturation: float = 1.0, temperature: Optional[int] = None, vignette: float = 0.0,
              grain: float = 0.0, sharpen: float = 0.0) -> str:
    """Global look. looks: teal_orange, warm_film, kodak_2383, bleach_bypass, noir, cool_thriller, matte, vibrant_social,
    vintage, day_for_night, neutral. lut: path to a .cube file."""
    g = T.Grade(look=look, lut=lut, exposure=exposure, contrast=contrast, saturation=saturation, temperature=temperature,
                vignette=vignette, grain=grain, sharpen=sharpen)
    _edit(timeline, lambda p, tl: setattr(tl, "grade", g), "set_grade")
    return _ok(g.model_dump(exclude_defaults=True))


@mcp.tool()
def set_captions(enabled: bool = True, style: Literal["bold_pop", "karaoke", "clean", "film", "minimal"] = "bold_pop",
                 position: Literal["bottom", "lower_third", "center", "top"] = "lower_third", max_words: int = 4,
                 uppercase: bool = False, highlight: str = "#FFD400", corrections: dict | None = None, timeline: str = "main") -> str:
    """Burned-in captions from the A-roll transcript. corrections: {'misheard':'Correct'} spelling fixes."""
    def f(p, tl):
        tl.captions = T.Captions(enabled=enabled, style=style, position=position, max_words=max_words, uppercase=uppercase,
                                 highlight=highlight, corrections={k.lower(): v for k, v in (corrections or {}).items()})
    _edit(timeline, f, "set_captions")
    return "captions updated"


@mcp.tool()
def fit_duration(target_seconds: float, protect: list[str] | None = None, timeline: str = "main") -> str:
    """Proportionally trim clip tails to hit an exact runtime (15/30/60 s spots). Protect clip ids that must not change."""
    d, _ = _edit(timeline, lambda p, tl: T.fit_to_duration(tl, target_seconds, protect), f"fit {target_seconds}")
    return f"duration now {d:.2f}s"


@mcp.tool()
def undo(timeline: str = "main", to_version: Optional[int] = None) -> str:
    """Revert to the previous (or a specific) version."""
    tl = T.undo(P(), timeline, to_version)
    return f"restored; now v{tl.version}\n" + T.describe(P(), tl, words=False)


@mcp.tool()
def timeline_history(timeline: str = "main") -> str:
    return T.history(P(), timeline)


# --------------------------------------------------------------------------- render / review / export
@mcp.tool()
def render(timeline: str = "main", quality: Literal["preview", "final", "hevc", "master", "web"] = "preview",
           start: Optional[float] = None, end: Optional[float] = None) -> str:
    """Render. preview = half-res fast; final = H.264 high quality; hevc; master = ProRes 422 HQ; web = VP9.
    Loudness-normalised to the timeline target; an .srt sidecar is written when there is dialogue."""
    p = P()
    return _ok(rnd.render(p, T.load(p, timeline), quality, start=start, end=end))


@mcp.tool()
def review(render_path: str, timeline: str = "main", mode: Literal["cuts", "interval"] = "cuts") -> list:
    """Watch a render: returns contact sheet image + QC (black/frozen frames, silence, loudness, clipping) + pacing stats.
    Critique it like a senior editor before calling it done."""
    p = P(); tl = T.load(p, timeline)
    img = rev.sheet(p, tl, render_path, mode)
    report = {"qc": rev.qc(render_path, tl.duration, tl.loudness_target), "pacing": rev.pacing(p, tl)}
    return [Image(path=str(img)), _ok(report)]


@mcp.tool()
def review_page(render_path: str, timeline: str = "main") -> str:
    """Write an HTML review page (player, shots, QC, EDL) next to the render, for sharing with humans."""
    p = P()
    return str(rev.review_page(p, T.load(p, timeline), render_path))


@mcp.tool()
def export_nle(timeline: str = "main", formats: list[str] | None = None, path_map: dict[str, str] | None = None) -> str:
    """Export to editing apps: premiere (XML for Premiere Pro: File > Import), otio (DaVinci Resolve), edl,
    fcpxml (experimental). Also writes an .srt caption sidecar. path_map rewrites media path prefixes when the
    footage lives at a different path on the editing machine: {"<path here>": "<path on that Mac>"}."""
    p = P()
    return _ok(exp.export(p, T.load(p, timeline), formats, path_map))


@mcp.resource("guide://editing")
def editing_guide() -> str:
    """Editorial craft guide."""
    f = Path(__file__).with_name("EDITING_GUIDE.md")
    return f.read_text() if f.exists() else ""


def main():
    mcp.run()


if __name__ == "__main__":
    main()
