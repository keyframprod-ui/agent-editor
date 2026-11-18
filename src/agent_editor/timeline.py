"""Editable timeline model + editing operations.

The timeline is a JSON document an LLM can read and reason about:

* ``clips``     — the primary storyline (A-roll), played back-to-back. Each clip's
                  ``transition`` describes how it enters from the previous clip.
* ``overlays``  — B-roll, picture-in-picture, titles, lower thirds placed at timeline time.
* ``audio``     — music / VO / SFX / ambience beds placed at timeline time (with ducking).
* ``captions``  — burned-in captions generated from the A-roll transcript.
* ``grade``     — a global look (LUT + primary adjustments); clips may override.

Every save keeps a history snapshot so the agent can undo / compare versions.
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

from . import analyze
from .core import EditorError, Project, fmt_tc

PRESETS = {
    "vertical": (1080, 1920, 30), "reel": (1080, 1920, 30), "tiktok": (1080, 1920, 30), "shorts": (1080, 1920, 30),
    "youtube": (1920, 1080, 30), "hd": (1920, 1080, 24), "uhd": (3840, 2160, 24), "4k": (3840, 2160, 24),
    "square": (1080, 1080, 30), "portrait45": (1080, 1350, 30),
    "cinema": (2048, 858, 24), "scope": (1920, 804, 24), "flat": (1998, 1080, 24),
}

XFADE_TYPES = ["fade", "dissolve", "fadeblack", "fadewhite", "wipeleft", "wiperight", "wipeup", "wipedown", "slideleft",
               "slideright", "slideup", "slidedown", "circleopen", "circleclose", "radial", "smoothleft", "smoothright",
               "zoomin", "pixelize", "hblur", "distance", "squeezeh", "squeezev", "diagtl", "rectcrop", "hlslice"]


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:5]}"


class Transition(BaseModel):
    type: str = "cut"          # cut | any XFADE_TYPES ("dissolve", "fadeblack", "wipeleft", ...)
    duration: float = 0.0


class Grade(BaseModel):
    lut: Optional[str] = None          # path to .cube
    lut_mix: float = 1.0
    exposure: float = 0.0              # -1..1 (brightness)
    contrast: float = 1.0
    saturation: float = 1.0
    gamma: float = 1.0
    temperature: Optional[int] = None  # Kelvin, e.g. 5000 cool .. 8000 warm (6500 neutral)
    vignette: float = 0.0              # 0..1
    grain: float = 0.0                 # 0..1
    sharpen: float = 0.0               # 0..1
    look: Optional[str] = None         # named look from render.LOOKS ("teal_orange", "bleach_bypass", "warm_film", "noir", "kodak_2383"...)


class Clip(BaseModel):
    id: str = Field(default_factory=lambda: _id("c"))
    media: str
    src_in: float
    src_out: float
    speed: float = 1.0
    transition: Transition = Field(default_factory=Transition)
    reframe: Literal["fill", "fit", "auto", "blur_fill", "manual"] = "fill"
    reframe_x: Optional[float] = None   # manual horizontal centre 0..1
    reframe_y: Optional[float] = None
    zoom: float = 1.0                   # punch-in (1.0 = none, 1.15 = 15% in)
    zoom_to: Optional[float] = None     # animate zoom over the clip (slow push)
    gain_db: float = 0.0
    mute: bool = False
    grade: Optional[Grade] = None
    label: str = ""
    notes: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, (self.src_out - self.src_in) / self.speed)


class Overlay(BaseModel):
    id: str = Field(default_factory=lambda: _id("o"))
    kind: Literal["media", "text", "title", "lower_third"] = "media"
    start: float
    duration: float
    media: Optional[str] = None
    src_in: float = 0.0
    speed: float = 1.0
    text: str = ""
    subtext: str = ""
    # layout for media overlays: full-frame B-roll by default
    full_frame: bool = True
    x: float = 0.5      # centre, 0..1
    y: float = 0.5
    scale: float = 0.35 # fraction of frame width when not full_frame
    opacity: float = 1.0
    fade_in: float = 0.0
    fade_out: float = 0.0
    audio_gain_db: Optional[float] = None   # None = muted B-roll; number = mix its audio
    reframe: Literal["fill", "fit", "auto", "blur_fill"] = "fill"
    freeze: bool = False                    # hold the frame at src_in (freeze-frame patch)
    style: dict = Field(default_factory=dict)  # text: font, size, color, position ("top","center","bottom"), align, box
    label: str = ""


class AudioClip(BaseModel):
    id: str = Field(default_factory=lambda: _id("a"))
    media: str
    start: float = 0.0
    src_in: float = 0.0
    duration: Optional[float] = None  # None = until source ends or timeline ends
    gain_db: float = 0.0
    fade_in: float = 0.0
    fade_out: float = 0.0
    duck: bool = True                 # duck under dialogue (A-roll audio)
    role: Literal["music", "vo", "sfx", "ambience", "dialogue"] = "music"
    label: str = ""


class Captions(BaseModel):
    enabled: bool = False
    style: Literal["bold_pop", "karaoke", "clean", "film", "minimal"] = "bold_pop"
    max_words: int = 4
    max_chars: int = 32
    position: Literal["bottom", "lower_third", "center", "top"] = "lower_third"
    font: str = "Inter"
    size: Optional[int] = None        # auto from frame height
    color: str = "#FFFFFF"
    highlight: str = "#FFD400"
    uppercase: bool = False
    corrections: dict[str, str] = Field(default_factory=dict)  # misheard word → correct spelling


class Timeline(BaseModel):
    name: str
    width: int = 1920
    height: int = 1080
    fps: float = 30
    clips: list[Clip] = Field(default_factory=list)
    overlays: list[Overlay] = Field(default_factory=list)
    audio: list[AudioClip] = Field(default_factory=list)
    captions: Captions = Field(default_factory=Captions)
    grade: Grade = Field(default_factory=Grade)
    loudness_target: float = -14.0    # LUFS: -14 social/streaming, -16 podcast, -23/-24 broadcast, -27 theatrical-ish
    dialogue_gain_db: float = 0.0
    duck_db: float = 12.0             # how far music ducks under dialogue
    background: str = "black"
    fade_in: float = 0.0              # global fade from black
    fade_out: float = 0.0
    notes: str = ""
    version: int = 0

    # ---------------------------------------------------------------- layout
    def layout(self) -> list[dict]:
        """Timeline positions of A-roll clips (accounts for transition overlaps)."""
        out, t = [], 0.0
        for i, c in enumerate(self.clips):
            d = c.transition.duration if (i > 0 and c.transition.type != "cut") else 0.0
            d = min(d, c.duration * 0.5, out[-1]["duration"] * 0.5) if out else 0.0
            start = t - d
            out.append({"id": c.id, "index": i, "start": round(start, 3), "end": round(start + c.duration, 3),
                        "duration": round(c.duration, 3), "overlap": round(d, 3)})
            t = start + c.duration
        return out

    @property
    def duration(self) -> float:
        lay = self.layout()
        end = lay[-1]["end"] if lay else 0.0
        for o in self.overlays:
            end = max(end, o.start + o.duration)
        return round(end, 3)

    def clip(self, cid: str) -> Clip:
        for c in self.clips:
            if c.id == cid:
                return c
        raise EditorError(f"no clip '{cid}'")

    def clip_index(self, cid: str) -> int:
        return next(i for i, c in enumerate(self.clips) if c.id == cid)

    def clip_at(self, t: float) -> tuple[Clip, dict] | None:
        for c, l in zip(self.clips, self.layout()):
            if l["start"] <= t < l["end"]:
                return c, l
        return None


# ===================================================================== persistence
def tl_path(p: Project, name: str) -> Path:
    return p.root / "timelines" / f"{name}.json"


def load(p: Project, name: str) -> Timeline:
    f = tl_path(p, name)
    if not f.exists():
        avail = [x.stem for x in (p.root / "timelines").glob("*.json")]
        raise EditorError(f"no timeline '{name}'. Available: {avail}")
    return Timeline.model_validate_json(f.read_text())


def save(p: Project, tl: Timeline, message: str = "") -> Timeline:
    tl.version += 1
    f = tl_path(p, tl.name)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(tl.model_dump_json(indent=2))
    h = p.root / "timelines" / ".history" / tl.name
    h.mkdir(parents=True, exist_ok=True)
    (h / f"v{tl.version:04d}.json").write_text(tl.model_dump_json(indent=2))
    with open(h / "log.txt", "a") as lg:
        lg.write(f"v{tl.version}: {message}\n")
    return tl


def undo(p: Project, name: str, to_version: int | None = None) -> Timeline:
    h = p.root / "timelines" / ".history" / name
    cur = load(p, name)
    target = to_version or cur.version - 1
    f = h / f"v{target:04d}.json"
    if not f.exists():
        raise EditorError(f"no version {target}")
    tl = Timeline.model_validate_json(f.read_text())
    tl.version = cur.version
    return save(p, tl, f"restore v{target}")


def history(p: Project, name: str) -> str:
    f = p.root / "timelines" / ".history" / name / "log.txt"
    return f.read_text() if f.exists() else ""


def new(p: Project, name: str, preset: str = "youtube", width: int | None = None, height: int | None = None,
        fps: float | None = None, **kw) -> Timeline:
    w, h, r = PRESETS.get(preset, PRESETS["youtube"])
    tl = Timeline(name=name, width=width or w, height=height or h, fps=fps or r, **kw)
    if tl.height > tl.width:   # vertical defaults
        tl.captions.enabled = True
        tl.loudness_target = -14
    return save(p, tl, f"new timeline preset={preset}")


# ===================================================================== describe
def describe(p: Project, tl: Timeline, words: bool = True) -> str:
    """Human/LLM-readable EDL with dialogue under each clip."""
    out = [f"TIMELINE '{tl.name}' v{tl.version}  {tl.width}x{tl.height}@{tl.fps}  duration {fmt_tc(tl.duration)}  "
           f"loudness {tl.loudness_target} LUFS  captions={'on/' + tl.captions.style if tl.captions.enabled else 'off'}"]
    if tl.grade.model_dump(exclude_defaults=True):
        out.append(f"  grade: {tl.grade.model_dump(exclude_defaults=True)}")
    out.append("A-ROLL:")
    for c, l in zip(tl.clips, tl.layout()):
        tr = "" if c.transition.type == "cut" or l["index"] == 0 else f" [{c.transition.type} {c.transition.duration}s]"
        extra = []
        if c.speed != 1: extra.append(f"speed {c.speed}x")
        if c.reframe != "fill": extra.append(c.reframe)
        if c.zoom != 1 or c.zoom_to: extra.append(f"zoom {c.zoom}->{c.zoom_to or c.zoom}")
        if c.mute: extra.append("muted")
        if c.gain_db: extra.append(f"{c.gain_db:+.1f}dB")
        out.append(f"  {l['index']:>2} {c.id} {fmt_tc(l['start'])}-{fmt_tc(l['end'])} ({l['duration']:.2f}s){tr}  "
                   f"{c.media} [{fmt_tc(c.src_in)}-{fmt_tc(c.src_out)}] {' '.join(extra)} {c.label}")
        if words:
            txt = source_words_text(p, c.media, c.src_in, c.src_out)
            if txt:
                out.append(f"       “{txt}”")
    if tl.overlays:
        out.append("OVERLAYS:")
        for o in sorted(tl.overlays, key=lambda o: o.start):
            what = f"{o.media} @src {fmt_tc(o.src_in)}" if o.kind == "media" else f"{o.kind} '{o.text}' {o.subtext}"
            out.append(f"  {o.id} {fmt_tc(o.start)}-{fmt_tc(o.start + o.duration)} {what} {'full' if o.full_frame and o.kind == 'media' else ''} {o.label}")
    if tl.audio:
        out.append("AUDIO:")
        for a in tl.audio:
            out.append(f"  {a.id} {a.role} {a.media} @{fmt_tc(a.start)} src {fmt_tc(a.src_in)} dur {a.duration or 'auto'} "
                       f"{a.gain_db:+.1f}dB duck={a.duck} fades {a.fade_in}/{a.fade_out}")
    return "\n".join(out)


def source_words(p: Project, mid: str, a: float, b: float) -> list[dict]:
    t = p.cache_get(mid, "transcript")
    if not t:
        return []
    return [w for w in t["words"] if w["start"] >= a - 0.05 and w["end"] <= b + 0.05]


def source_words_text(p: Project, mid: str, a: float, b: float) -> str:
    return " ".join(w["w"] for w in source_words(p, mid, a, b))


def timeline_words(p: Project, tl: Timeline) -> list[dict]:
    """A-roll words mapped to timeline time (used for captions + reviewing the cut as text)."""
    out = []
    for c, l in zip(tl.clips, tl.layout()):
        if c.mute:
            continue
        for w in source_words(p, c.media, c.src_in, c.src_out):
            ts = l["start"] + (w["start"] - c.src_in) / c.speed
            te = l["start"] + (w["end"] - c.src_in) / c.speed
            word = tl.captions.corrections.get(w["w"].strip(" ,.?!").lower(), None)
            text = w["w"] if word is None else re.sub(r"[\w']+", word, w["w"], count=1)
            out.append({"w": text, "start": round(ts, 3), "end": round(te, 3), "clip": c.id})
    return out


# ===================================================================== editing ops
def _insert(tl: Timeline, clip: Clip, position: int | None) -> Clip:
    if position is None or position >= len(tl.clips):
        tl.clips.append(clip)
    else:
        tl.clips.insert(max(0, position), clip)
    return clip


def _check_range(p: Project, mid: str, a: float, b: float) -> tuple[float, float]:
    m = p.media(mid)
    dur = m["duration"]
    if m["kind"] == "image":
        return a, b
    a, b = max(0.0, a), min(dur, b)
    if b - a < 0.04:
        raise EditorError(f"empty range {a}-{b} for {mid} (duration {dur:.2f})")
    return round(a, 3), round(b, 3)


def add_clip(p: Project, tl: Timeline, media: str, src_in: float, src_out: float, position: int | None = None,
             transition: str = "cut", transition_duration: float = 0.0, **kw) -> Clip:
    a, b = _check_range(p, media, src_in, src_out)
    c = Clip(media=media, src_in=a, src_out=b, transition=Transition(type=transition, duration=transition_duration), **kw)
    return _insert(tl, c, position)


def add_words(p: Project, tl: Timeline, media: str, word_start: int, word_end: int, position: int | None = None,
              pad: float = 0.08, **kw) -> Clip:
    """Text-based editing: add the source span covering transcript words [word_start..word_end]."""
    a, b = analyze.words_range(p, media, word_start, word_end, pad)
    return add_clip(p, tl, media, a, b, position, **kw)


def add_phrase(p: Project, tl: Timeline, media: str, phrase_start: str, phrase_end: str | None = None,
               occurrence: int = 0, position: int | None = None, **kw) -> Clip:
    """Add the span from the first word of `phrase_start` to the last word of `phrase_end` (or of phrase_start)."""
    h1 = analyze.find_phrase(p, media, phrase_start)
    if len(h1) <= occurrence:
        raise EditorError(f"phrase not found in {media}: '{phrase_start}'")
    ws = h1[occurrence]["word_start"]
    we = h1[occurrence]["word_end"]
    if phrase_end:
        h2 = [h for h in analyze.find_phrase(p, media, phrase_end) if h["word_start"] >= ws]
        if not h2:
            raise EditorError(f"end phrase not found after start in {media}: '{phrase_end}'")
        we = h2[0]["word_end"]
    return add_words(p, tl, media, ws, we, position, **kw)


def add_shot(p: Project, tl: Timeline, media: str, shot_index: int, head_trim: float = 0.0, tail_trim: float = 0.0,
             max_duration: float | None = None, position: int | None = None, **kw) -> Clip:
    sc = analyze.scenes(p, media)
    s = next((x for x in sc if x["index"] == shot_index), None)
    if not s:
        raise EditorError(f"{media} has no shot #{shot_index} (0-{len(sc) - 1})")
    a, b = s["start"] + head_trim, s["end"] - tail_trim
    if max_duration and b - a > max_duration:
        mid = (a + b) / 2
        a, b = mid - max_duration / 2, mid + max_duration / 2
    return add_clip(p, tl, media, a, b, position, **kw)


def remove(tl: Timeline, item_id: str) -> None:
    for coll in (tl.clips, tl.overlays, tl.audio):
        for i, x in enumerate(coll):
            if x.id == item_id:
                del coll[i]
                return
    raise EditorError(f"no item '{item_id}'")


def move(tl: Timeline, clip_id: str, new_index: int) -> None:
    c = tl.clips.pop(tl.clip_index(clip_id))
    tl.clips.insert(max(0, min(new_index, len(tl.clips))), c)


def trim(p: Project, tl: Timeline, clip_id: str, head: float = 0.0, tail: float = 0.0) -> Clip:
    """Positive head trims frames off the start; negative extends. Same for tail at the end."""
    c = tl.clip(clip_id)
    a, b = _check_range(p, c.media, c.src_in + head, c.src_out - tail)
    c.src_in, c.src_out = a, b
    return c


def split(tl: Timeline, clip_id: str, at_src: float) -> tuple[Clip, Clip]:
    c = tl.clip(clip_id)
    if not (c.src_in < at_src < c.src_out):
        raise EditorError(f"split point {at_src} outside clip {c.src_in}-{c.src_out}")
    i = tl.clip_index(clip_id)
    second = c.model_copy(deep=True)
    second.id = _id("c")
    second.src_in = at_src
    second.transition = Transition()
    c.src_out = at_src
    tl.clips.insert(i + 1, second)
    return c, second


def set_transition(tl: Timeline, clip_id: str, type: str = "dissolve", duration: float = 0.5) -> None:
    if type != "cut" and type not in XFADE_TYPES:
        raise EditorError(f"unknown transition {type}. Options: cut, {', '.join(XFADE_TYPES)}")
    tl.clip(clip_id).transition = Transition(type=type, duration=duration if type != "cut" else 0.0)


def tighten(p: Project, tl: Timeline, clip_id: str, max_gap: float = 0.35, pad: float = 0.1,
            remove_fillers: bool = True, aggressive_fillers: bool = False) -> list[str]:
    """Jump-cut a talking clip: split on pauses > max_gap and drop filler words, using word timings."""
    c = tl.clip(clip_id)
    words = source_words(p, c.media, c.src_in, c.src_out)
    if not words:
        raise EditorError("clip has no transcript words — run transcribe first")
    bag = analyze.FILLERS if aggressive_fillers else analyze.HARD_FILLERS
    keep = [w for w in words if not (remove_fillers and re.sub(r"[^\w' ]", "", w["w"].lower()).strip() in bag)]
    if not keep:
        return []
    spans: list[list[float]] = []
    for w in keep:
        if spans and w["start"] - spans[-1][1] <= max_gap and w["i"] == spans[-1][2] + 1:
            spans[-1][1] = w["end"]; spans[-1][2] = w["i"]
        elif spans and w["start"] - spans[-1][1] <= max_gap and not remove_fillers:
            spans[-1][1] = w["end"]; spans[-1][2] = w["i"]
        else:
            spans.append([w["start"], w["end"], w["i"]])
    idx = tl.clip_index(clip_id)
    base = tl.clips.pop(idx)
    new_ids = []
    for k, (a, b, _) in enumerate(spans):
        a2, b2 = max(base.src_in, a - pad), min(base.src_out, b + pad)
        if new_ids:  # avoid overlap with previous span
            prev = tl.clips[idx + k - 1]
            a2 = max(a2, prev.src_out)
        if b2 - a2 < 0.12:
            continue
        nc = base.model_copy(deep=True)
        nc.id = _id("c"); nc.src_in = round(a2, 3); nc.src_out = round(b2, 3)
        nc.transition = base.transition if k == 0 else Transition()
        tl.clips.insert(idx + len(new_ids), nc)
        new_ids.append(nc.id)
    return new_ids


def beat_montage(p: Project, tl: Timeline, shots: list[dict], music: str, beats_per_cut: int | list[int] = 2,
                 music_start: float = 0.0, start_on_downbeat: bool = True, add_music: bool = True,
                 music_gain_db: float = 0.0) -> list[str]:
    """Cut a list of shots ({media, start?, end?, shot?}) to the music's beat grid.

    beats_per_cut can be a list to vary rhythm (e.g. [4,4,2,2,1,1,2]). Each shot is centred on its
    most interesting portion (middle) and trimmed to the beat interval.
    """
    a = analyze.audio(p, music)
    beats = [b for b in a.get("beats", []) if b >= music_start]
    if start_on_downbeat and a.get("downbeats"):
        first = next((d for d in a["downbeats"] if d >= music_start), None)
        if first is not None:
            beats = [b for b in beats if b >= first]
    if len(beats) < 2:
        raise EditorError("not enough beats detected in music")
    pattern = beats_per_cut if isinstance(beats_per_cut, list) else [beats_per_cut]
    ids, bi, k = [], 0, 0
    offset = beats[0]
    t_lay = tl.duration if tl.clips else 0.0
    for s in shots:
        n = pattern[k % len(pattern)]; k += 1
        if bi + n >= len(beats):
            break
        dur = beats[bi + n] - beats[bi]
        mid = s["media"]
        if "shot" in s:
            sc = next(x for x in analyze.scenes(p, mid) if x["index"] == s["shot"])
            a0, b0 = sc["start"], sc["end"]
        else:
            m = p.media(mid)
            a0, b0 = s.get("start", 0.0), s.get("end", m["duration"])
        centre = (a0 + b0) / 2
        src_in = max(a0, centre - dur / 2)
        src_out = src_in + dur
        if src_out > b0 + 1e-3:  # shot too short: slow it down slightly or take what we have
            src_in, src_out = a0, b0
        c = add_clip(p, tl, mid, src_in, src_out, reframe=s.get("reframe", "fill"), mute=s.get("mute", True))
        # snap exactly to the beat interval via speed when shot is short
        c.speed = round(max(0.5, min(2.0, (c.src_out - c.src_in) / dur)), 4) if abs((c.src_out - c.src_in) - dur) > 0.02 else 1.0
        ids.append(c.id)
        bi += n
    if add_music:
        tl.audio.append(AudioClip(media=music, start=t_lay, src_in=offset, duration=round(beats[bi] - offset + 0.0, 3),
                                  gain_db=music_gain_db, fade_out=1.0, duck=False, role="music"))
    return ids


def l_cut(p: Project, tl: Timeline, clip_id: str, seconds: float) -> Overlay:
    """Split edit: clip_id's PICTURE continues `seconds` over the start of the next clip (whose sound is heard).
    (A J-cut into clip X is an l_cut on the clip before X.) Uses source handles after the clip's out-point."""
    i = tl.clip_index(clip_id)
    if i + 1 >= len(tl.clips):
        raise EditorError("l_cut needs a following clip")
    c, lay = tl.clips[i], tl.layout()
    m = p.media(c.media)
    if m["kind"] == "video" and c.src_out + seconds * c.speed > m["duration"]:
        raise EditorError(f"not enough handle after {clip_id} ({m['duration'] - c.src_out:.2f}s available)")
    seconds = min(seconds, lay[i + 1]["duration"] - 0.1)
    o = Overlay(kind="media", media=c.media, src_in=c.src_out, start=lay[i + 1]["start"] + lay[i + 1]["overlap"],
                duration=round(seconds, 3), speed=c.speed, reframe=c.reframe if c.reframe != "manual" else "fill",
                label=f"L-cut {clip_id}→{tl.clips[i + 1].id}")
    tl.overlays.append(o)
    return o


def j_cut(p: Project, tl: Timeline, clip_id: str, seconds: float) -> Overlay:
    """Split edit: clip_id's SOUND starts `seconds` before its picture (previous clip's picture holds over it)."""
    i = tl.clip_index(clip_id)
    if i == 0:
        raise EditorError("j_cut needs a previous clip; for the first clip use freeze_patch / cover with B-roll")
    return l_cut(p, tl, tl.clips[i - 1].id, seconds)


def freeze_patch(p: Project, tl: Timeline, clip_id: str, seconds: float, from_head: bool = True,
                 at_src: float | None = None) -> Overlay:
    """Hide a flash frame / bad head or tail of a clip with a freeze frame (keeps the audio intact).
    at_src defaults to the first clean source frame after `seconds` (head) or before the tail."""
    i = tl.clip_index(clip_id)
    c, l = tl.clips[i], tl.layout()[i]
    if from_head:
        src = at_src if at_src is not None else c.src_in + seconds * c.speed + 0.02
        start = l["start"]
    else:
        src = at_src if at_src is not None else c.src_out - seconds * c.speed - 0.06
        start = l["end"] - seconds
    o = Overlay(kind="media", media=c.media, src_in=round(src, 3), start=round(start, 3), duration=round(seconds, 3),
                freeze=True, reframe=c.reframe if c.reframe != "manual" else "fill", label=f"freeze patch {clip_id}")
    tl.overlays.append(o)
    return o


def add_overlay(p: Project, tl: Timeline, **kw) -> Overlay:
    o = Overlay(**kw)
    if o.kind == "media":
        if not o.media:
            raise EditorError("media overlay needs `media`")
        p.media(o.media)
    tl.overlays.append(o)
    return o


def add_audio(p: Project, tl: Timeline, **kw) -> AudioClip:
    a = AudioClip(**kw)
    p.media(a.media)
    tl.audio.append(a)
    return a


def update(tl: Timeline, item_id: str, fields: dict) -> dict:
    """Generic field patch for any clip/overlay/audio item (or 'timeline', 'captions', 'grade')."""
    if item_id == "timeline":
        target = tl
    elif item_id == "captions":
        target = tl.captions
    elif item_id == "grade":
        target = tl.grade
    else:
        target = next((x for coll in (tl.clips, tl.overlays, tl.audio) for x in coll if x.id == item_id), None)
        if target is None:
            raise EditorError(f"no item '{item_id}'")
    for k, v in fields.items():
        if not hasattr(target, k):
            raise EditorError(f"{type(target).__name__} has no field '{k}'")
        cur = getattr(target, k)
        if isinstance(cur, BaseModel) and isinstance(v, dict):
            v = type(cur).model_validate({**cur.model_dump(), **v})
        elif k == "grade" and isinstance(v, dict):
            v = Grade.model_validate(v)
        elif k == "transition" and isinstance(v, dict):
            v = Transition.model_validate(v)
        setattr(target, k, v)
    return target.model_dump() if isinstance(target, BaseModel) else {}


def fit_to_duration(tl: Timeline, target: float, protect: list[str] | None = None) -> float:
    """Proportionally trim clip tails (except protected ids) to hit a target duration (ads: 15/30/60)."""
    protect = set(protect or [])
    cur = tl.duration
    excess = cur - target
    if excess <= 0:
        return cur
    trimmable = [c for c in tl.clips if c.id not in protect]
    pool = sum(max(0, c.duration - 0.6) for c in trimmable)
    if pool <= 0:
        raise EditorError("nothing left to trim")
    ratio = min(1.0, excess / pool)
    for c in trimmable:
        cut = max(0, c.duration - 0.6) * ratio * c.speed
        c.src_out = round(c.src_out - cut, 3)
    return tl.duration
