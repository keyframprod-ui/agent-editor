"""QC + self-review: the agent "watches" its own render.

* contact sheets of the render (every N seconds or at every cut)
* technical QC: black frames, frozen frames, silence gaps, clipping, loudness, duration
* pacing stats: shot length distribution, cuts per minute, words per minute
* an HTML review page (player + sheet + EDL + QC) for humans
"""
from __future__ import annotations

import html
import json
import re
import statistics
from pathlib import Path

from . import analyze
from .core import Project, fmt_tc, probe, run
from .timeline import Timeline, describe, timeline_words


def render_frames(path: str, times: list[float], out_dir: Path, width: int = 480) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    frames = []
    for t in times:
        f = out_dir / f"t{t:09.3f}.jpg"
        if not f.exists():
            run(["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", path,
                 "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "4", str(f)])
        frames.append(f)
    return frames


def sheet(p: Project, tl: Timeline, render_path: str, mode: str = "cuts", every: float = 2.0, max_frames: int = 30) -> Path:
    """Contact sheet of a render: one frame per A-roll clip ('cuts') or every N seconds ('interval')."""
    info = probe(render_path)
    if mode == "cuts" and tl.clips:
        times = [min(info["duration"] - 0.05, (l["start"] + l["end"]) / 2) for l in tl.layout()]
    else:
        n = int(info["duration"] // every) + 1
        times = [min(info["duration"] - 0.05, i * every + 0.05) for i in range(n)]
    if len(times) > max_frames:
        step = len(times) / max_frames
        times = [times[int(i * step)] for i in range(max_frames)]
    out_dir = p.root / "cache" / "_review" / Path(render_path).stem
    frames = render_frames(render_path, times, out_dir, 400 if info["width"] >= info["height"] else 270)
    cols = 6 if info["height"] > info["width"] else 4
    return analyze.contact_sheet(frames, [fmt_tc(t) for t in times], out_dir / "sheet.jpg", cols=cols,
                                 thumb_w=400 if info["width"] >= info["height"] else 220)


def qc(path: str, expected_duration: float | None = None, loudness_target: float | None = None) -> dict:
    info = probe(path)
    r = run(["ffmpeg", "-hide_banner", "-nostdin", "-i", path, "-vf", "blackdetect=d=0.25:pix_th=0.06,freezedetect=n=0.002:d=1.5",
             "-af", "silencedetect=n=-50dB:d=1.2,astats=metadata=0:reset=0", "-f", "null", "-"], check=False)
    e = r.stderr
    blacks = [(float(a), float(b)) for a, b in re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", e)]
    freezes = [float(x) for x in re.findall(r"freeze_start: ([\d.]+)", e)]
    silences = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", e)]
    peaks = [float(x) for x in re.findall(r"Peak level dB: ([-\d.]+)", e)]
    from .render import measure_loudness
    ln = measure_loudness(Path(path))
    issues = []
    for a, b in blacks:
        if a > 0.3 and b < info["duration"] - 0.3:
            issues.append(f"black frames {fmt_tc(a)}–{fmt_tc(b)} (possible gap / flash frame)")
    for f in freezes:
        issues.append(f"frozen picture from {fmt_tc(f)} (≥1.5s) — check for held frame / short source")
    for s in silences:
        if 0.5 < s < info["duration"] - 1.5:
            issues.append(f"audio silence from {fmt_tc(s)} (≥1.2s) — intended?")
    if peaks and max(peaks) > -0.5:
        issues.append(f"audio peak {max(peaks):.1f} dBFS — near clipping")
    I = float(ln["input_i"])
    if loudness_target is not None and abs(I - loudness_target) > 1.0:
        issues.append(f"loudness {I:.1f} LUFS vs target {loudness_target}")
    if expected_duration and abs(info["duration"] - expected_duration) > 0.25:
        issues.append(f"duration {info['duration']:.2f}s vs timeline {expected_duration:.2f}s")
    return {"path": path, "duration": info["duration"], "resolution": f"{info.get('width')}x{info.get('height')}",
            "fps": info.get("fps"), "loudness_lufs": I, "true_peak_dbtp": float(ln["input_tp"]),
            "issues": issues or ["none found"]}


def pacing(p: Project, tl: Timeline) -> dict:
    lay = tl.layout()
    durs = [l["duration"] for l in lay]
    words = timeline_words(p, tl)
    total = tl.duration or 1
    cuts = len(lay) - 1 + sum(1 for o in tl.overlays if o.kind == "media") * 2
    res = {"duration": total, "clips": len(lay), "cuts_per_minute": round(cuts / total * 60, 1),
           "words_per_minute": round(len(words) / total * 60, 1) if words else 0}
    if durs:
        res.update(avg_shot=round(statistics.mean(durs), 2), median_shot=round(statistics.median(durs), 2),
                   longest=round(max(durs), 2), shortest=round(min(durs), 2))
    # hook check (first 3 s) for social
    if words:
        first = [w["w"] for w in words if w["start"] < 3.0]
        res["first_3s_words"] = " ".join(first)
        res["time_to_first_word"] = round(words[0]["start"], 2)
    flash = flash_shots(p, tl)
    if flash:
        res["flash_shots"] = flash
    long_static = [l for l in lay if l["duration"] > 6 and not any(o.start < l["end"] and o.start + o.duration > l["start"] for o in tl.overlays)]
    if long_static:
        res["long_uncovered_clips"] = [f"{tl.clips[l['index']].id} ({l['duration']:.1f}s @ {fmt_tc(l['start'])})" for l in long_static]
    return res


def flash_shots(p: Project, tl: Timeline, edge: float = 0.6) -> list[str]:
    """Clips that contain a source camera change within `edge` s of their head/tail (a flash of another shot)."""
    out = []
    lay = {l["id"]: l for l in tl.layout()}

    def covered(t0: float, t1: float) -> bool:
        return any(o.kind == "media" and o.full_frame and o.start <= t0 + 0.02 and o.start + o.duration >= t1 - 0.02 for o in tl.overlays)

    for c in tl.clips:
        sc = p.cache_get(c.media, "scenes") or []
        L = lay[c.id]
        for s in sc[1:]:
            b = s["start"]
            if c.src_in < b < c.src_out:
                if b - c.src_in < edge and not covered(L["start"], L["start"] + (b - c.src_in) / c.speed):
                    out.append(f"{c.id}: source cut {b - c.src_in:.2f}s after head — trim_clip head={b - c.src_in + 0.02:.2f}")
                elif c.src_out - b < edge and not covered(L["end"] - (c.src_out - b) / c.speed, L["end"]):
                    out.append(f"{c.id}: source cut {c.src_out - b:.2f}s before tail — trim_clip tail={c.src_out - b + 0.02:.2f}")
    return out


def review_page(p: Project, tl: Timeline, render_path: str) -> Path:
    """Self-contained HTML review page next to the render."""
    sh = sheet(p, tl, render_path)
    q = qc(render_path, tl.duration, tl.loudness_target)
    pc = pacing(p, tl)
    rp = Path(render_path)
    out = rp.with_suffix(".review.html")
    import shutil
    sheet_copy = rp.with_suffix(".sheet.jpg")
    shutil.copy(sh, sheet_copy)
    out.write_text(f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>{html.escape(tl.name)} v{tl.version}</title>
<style>body{{font:15px/1.5 Inter,system-ui,sans-serif;background:#111;color:#eee;margin:0;padding:16px;max-width:1100px;margin:auto}}
video{{width:100%;max-height:70vh;background:#000;border-radius:8px}}img{{width:100%;border-radius:8px}}
pre{{background:#1b1b1f;padding:12px;border-radius:8px;overflow:auto;font-size:12px}}h1{{font-size:20px}}h2{{font-size:16px;color:#aaa}}
.kv span{{display:inline-block;background:#222;padding:3px 8px;border-radius:6px;margin:2px}}</style>
<h1>{html.escape(tl.name)} — v{tl.version}</h1>
<video src="{html.escape(rp.name)}" controls playsinline><track kind=captions src="{html.escape(rp.with_suffix('.srt').name)}"></video>
<h2>Pacing</h2><div class=kv>{''.join(f'<span>{html.escape(str(k))}: {html.escape(str(v))}</span>' for k, v in pc.items())}</div>
<h2>QC</h2><ul>{''.join(f'<li>{html.escape(i)}</li>' for i in q['issues'])}</ul>
<div class=kv><span>{q['resolution']} @ {q['fps']}</span><span>{q['loudness_lufs']:.1f} LUFS</span><span>TP {q['true_peak_dbtp']:.1f} dBTP</span><span>{q['duration']:.2f}s</span></div>
<h2>Shots</h2><img src="{html.escape(sheet_copy.name)}">
<h2>Edit decision list</h2><pre>{html.escape(describe(p, tl))}</pre>""")
    return out
