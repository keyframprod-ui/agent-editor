"""Timeline → FFmpeg renderer.

Pipeline (each stage cached / inspectable):
  1. segments  — every A-roll clip rendered to a normalised intermediate (size, fps, reframe,
                 speed, per-clip correction grade, stereo 48k PCM). Cached by content hash, so
                 re-renders after an edit only re-encode changed clips.
  2. join      — segments joined with cuts (concat) or xfade/acrossfade transitions.
  3. composite — B-roll / PiP overlays, global look, global fades, captions + titles (ASS),
                 music / VO / SFX mixed with sidechain ducking under dialogue.
  4. loudness  — two-pass EBU R128 normalisation to the timeline's target, video stream copied.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path

from . import analyze, captions
from .core import EditorError, Project, ffmpeg, run
from .timeline import Clip, Grade, Overlay, Timeline, timeline_words

LOOKS: dict[str, str] = {
    "teal_orange": "colorbalance=rs=-0.06:gs=-0.01:bs=0.09:rm=0.02:bm=-0.02:rh=0.08:gh=0.02:bh=-0.09,eq=contrast=1.06:saturation=1.08",
    "warm_film": "colortemperature=temperature=7000:mix=0.6,curves=all='0/0.03 0.25/0.22 0.75/0.8 1/0.97',eq=saturation=0.92",
    "kodak_2383": "curves=r='0/0.02 0.3/0.26 0.7/0.76 1/0.98':g='0/0.02 0.3/0.27 0.7/0.73 1/0.96':b='0/0.06 0.3/0.28 0.7/0.66 1/0.9',eq=saturation=0.95:contrast=1.04",
    "bleach_bypass": "eq=saturation=0.5:contrast=1.28:gamma=0.95",
    "noir": "hue=s=0,eq=contrast=1.35:gamma=0.92,vignette=PI/4.5",
    "cool_thriller": "colortemperature=temperature=5200:mix=0.7,colorbalance=bs=0.06:gs=0.02,eq=saturation=0.82:contrast=1.08",
    "matte": "curves=all='0/0.07 0.5/0.5 1/0.94',eq=saturation=0.9",
    "vibrant_social": "eq=saturation=1.22:contrast=1.06,unsharp=5:5:0.4",
    "vintage": "curves=preset=vintage,eq=saturation=0.85",
    "day_for_night": "colortemperature=temperature=4200,eq=brightness=-0.18:saturation=0.55:gamma=0.8,vignette=PI/4",
    "neutral": "null",
}

QUALITY = {
    #           video args                                                             audio args
    "preview": (["-c:v", "libx264", "-preset", "ultrafast", "-crf", "28", "-pix_fmt", "yuv420p"], ["-c:a", "aac", "-b:a", "128k"], ".mp4"),
    "final":   (["-c:v", "libx264", "-preset", "medium", "-crf", "17", "-profile:v", "high", "-pix_fmt", "yuv420p",
                 "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"], ["-c:a", "aac", "-b:a", "320k"], ".mp4"),
    "hevc":    (["-c:v", "libx265", "-preset", "medium", "-crf", "20", "-tag:v", "hvc1", "-pix_fmt", "yuv420p10le"], ["-c:a", "aac", "-b:a", "256k"], ".mp4"),
    "master":  (["-c:v", "prores_ks", "-profile:v", "3", "-pix_fmt", "yuv422p10le", "-vendor", "apl0"], ["-c:a", "pcm_s24le"], ".mov"),
    "web":     (["-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0", "-row-mt", "1"], ["-c:a", "libopus", "-b:a", "160k"], ".webm"),
}


def _even(x: float) -> int:
    return max(2, int(round(x / 2)) * 2)


def grade_filters(g: Grade | None) -> list[str]:
    if g is None:
        return []
    f: list[str] = []
    if g.temperature:
        f.append(f"colortemperature=temperature={int(g.temperature)}")
    if g.exposure or g.contrast != 1 or g.saturation != 1 or g.gamma != 1:
        f.append(f"eq=brightness={g.exposure:.3f}:contrast={g.contrast:.3f}:saturation={g.saturation:.3f}:gamma={g.gamma:.3f}")
    if g.lut:
        if not Path(g.lut).exists():
            raise EditorError(f"LUT not found: {g.lut}")
        lut = g.lut.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
        if g.lut_mix < 1:
            f.append(f"split[lo][lg];[lg]lut3d=file='{lut}'[lgd];[lo][lgd]blend=all_expr='A*{1 - g.lut_mix:.3f}+B*{g.lut_mix:.3f}'")
        else:
            f.append(f"lut3d=file='{lut}'")
    if g.look:
        if g.look not in LOOKS:
            raise EditorError(f"unknown look '{g.look}'. Options: {', '.join(LOOKS)}")
        f.append(LOOKS[g.look])
    if g.sharpen:
        f.append(f"unsharp=5:5:{g.sharpen * 1.2:.2f}")
    if g.vignette:
        f.append(f"vignette=angle={0.2 + g.vignette * 0.6:.3f}")
    if g.grain:
        f.append(f"noise=c0s={int(4 + g.grain * 16)}:c0f=t+u")
    return f


def _piecewise(points: list[tuple[float, float]], var: str = "t") -> str:
    """Piecewise-linear ffmpeg expression through (t, v) points."""
    if len(points) == 1:
        return f"{points[0][1]:.4f}"
    # thin to <= 40 keyframes
    if len(points) > 40:
        step = len(points) / 40
        points = [points[int(i * step)] for i in range(40)] + [points[-1]]
    expr = f"{points[-1][1]:.4f}"
    for (t0, v0), (t1, v1) in reversed(list(zip(points, points[1:]))):
        if t1 <= t0:
            continue
        seg = f"({v0:.4f}+({v1:.4f}-{v0:.4f})*({var}-{t0:.3f})/{t1 - t0:.3f})"
        expr = f"if(lt({var},{t1:.3f}),{seg},{expr})"
    return f"if(lt({var},{points[0][0]:.3f}),{points[0][1]:.4f},{expr})"


def frame_filter(W: int, H: int, mode: str, cx: float | None = None, cy: float | None = None,
                 track: list[tuple[float, float]] | None = None, label_in: str = "", label_out: str = "") -> str:
    """Fit source into W×H. Returns a filter chain (may contain labelled sub-graph for blur_fill)."""
    cx = 0.5 if cx is None else cx
    cy = 0.5 if cy is None else cy
    if mode == "fit":
        return f"scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black"
    if mode == "blur_fill":
        return (f"split=2[bf_a][bf_b];[bf_a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
                f"scale={W//8}:{H//8},gblur=sigma=6,scale={W}:{H},eq=brightness=-0.07:saturation=0.85[bf_bg];"
                f"[bf_b]scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos[bf_fg];[bf_bg][bf_fg]overlay=(W-w)/2:(H-h)/2")
    if mode == "auto" and track:
        xexpr = _piecewise(track)
        return (f"scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,"
                f"crop={W}:{H}:x='max(0,min(iw-{W},({xexpr})*iw-{W}/2))':y='(ih-{H})*{cy:.3f}'")
    return (f"scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={W}:{H}:x='max(0,min(iw-{W},{cx:.4f}*iw-{W}/2))':y='max(0,min(ih-{H},{cy:.4f}*ih-{H}/2))'")


def _atempo(speed: float) -> list[str]:
    out, s = [], speed
    while s > 2.0:
        out.append("atempo=2.0"); s /= 2.0
    while s < 0.5:
        out.append("atempo=0.5"); s /= 0.5
    if abs(s - 1) > 1e-3:
        out.append(f"atempo={s:.5f}")
    return out


# ============================================================================ stage 1: segments
def _seg_key(p: Project, tl: Timeline, c: Clip, scale: float, quality: str) -> str:
    m = p.media(c.media)
    d = c.model_dump(exclude={"id", "transition", "label", "notes"})
    blob = json.dumps([d, tl.width, tl.height, tl.fps, scale, quality == "preview", m["path"], m.get("size_bytes")], sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:16]


def render_segment(p: Project, tl: Timeline, c: Clip, scale: float = 1.0, quality: str = "final") -> Path:
    W, H = _even(tl.width * scale), _even(tl.height * scale)
    out = p.root / "cache" / "_segments" / f"{_seg_key(p, tl, c, scale, quality)}.mkv"
    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    m = p.media(c.media)
    dur = c.duration
    src_len = c.src_out - c.src_in
    args: list[str] = []
    if m["kind"] == "image":
        args += ["-loop", "1", "-framerate", str(tl.fps), "-t", f"{dur + 0.1:.3f}", "-i", m["path"]]
    else:
        args += ["-ss", f"{c.src_in:.3f}", "-t", f"{src_len:.3f}", "-i", m["path"]]
    use_src_audio = m.get("has_audio") and not c.mute and m["kind"] != "image"
    if not use_src_audio:
        args += ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]

    vf = ["setpts=(PTS-STARTPTS)/%.5f" % c.speed if m["kind"] != "image" else "setpts=PTS-STARTPTS"]
    track = None
    if c.reframe == "auto" and m["kind"] == "video":
        tr = analyze.subject_track(p, c.media, c.src_in, c.src_out)
        track = [(t / c.speed, x) for t, x in tr]
    mode = {"manual": "fill"}.get(c.reframe, c.reframe)
    vf.append(frame_filter(W, H, mode, c.reframe_x, c.reframe_y, track))
    z0, z1 = c.zoom, c.zoom_to
    if m["kind"] == "image" and z1 is None and z0 == 1.0:
        z1 = 1.08  # gentle Ken Burns on stills by default
    if z1 is not None and abs(z1 - z0) > 1e-3:
        n = max(1, int(dur * tl.fps))
        vf.append(f"scale={W*2}:{H*2}:flags=lanczos,zoompan=z='{z0}+({z1}-{z0})*on/{n}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
                  f":d=1:s={W}x{H}:fps={tl.fps}")
    elif abs(z0 - 1.0) > 1e-3:
        vf.append(f"crop=iw/{z0}:ih/{z0},scale={W}:{H}:flags=lanczos")
    vf += grade_filters(c.grade)
    vf += [f"fps={tl.fps}", "format=yuv420p", "setsar=1", f"tpad=stop_mode=clone:stop_duration=1", f"trim=duration={dur:.4f}", "setpts=PTS-STARTPTS"]
    vchain = ",".join(vf)

    if use_src_audio:
        af = ["asetpts=PTS-STARTPTS", *_atempo(c.speed), f"volume={c.gain_db:.2f}dB",
              "aresample=48000", "aformat=sample_fmts=fltp:channel_layouts=stereo",
              "afade=t=in:d=0.012", f"afade=t=out:st={max(0, dur - 0.012):.4f}:d=0.012",  # de-click at edit points
              f"apad=whole_dur={dur:.4f}", f"atrim=duration={dur:.4f}"]
        achain = f"[0:a]{','.join(af)}[a]"
    else:
        achain = f"[1:a]atrim=duration={dur:.4f}[a]"
    v_args, _, _ = QUALITY["preview" if quality == "preview" else "final"]
    venc = ["-c:v", "libx264", "-preset", "ultrafast" if quality == "preview" else "veryfast",
            "-crf", "24" if quality == "preview" else "12", "-pix_fmt", "yuv420p", "-g", str(int(tl.fps))]
    ffmpeg(args + ["-filter_complex", f"[0:v]{vchain}[v];{achain}", "-map", "[v]", "-map", "[a]",
                   *venc, "-c:a", "pcm_s16le", "-ar", "48000", str(out)])
    return out


# ============================================================================ stage 2: join
def join(p: Project, tl: Timeline, segs: list[Path], out: Path) -> Path:
    lay = tl.layout()
    if len(segs) == 1:
        shutil.copy(segs[0], out)
        return out
    if all(c.transition.type == "cut" or i == 0 for i, c in enumerate(tl.clips)):
        lst = out.with_suffix(".txt")
        lst.write_text("".join(f"file '{s}'\n" for s in segs))
        ffmpeg(["-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(out)])
        return out
    args: list[str] = []
    for s in segs:
        args += ["-i", str(s)]
    fc: list[str] = []
    for i in range(len(segs)):
        fc.append(f"[{i}:v]settb=AVTB,setpts=PTS-STARTPTS[v{i}];[{i}:a]asetpts=PTS-STARTPTS[a{i}]")
    acc_v, acc_a, acc_d = "v0", "a0", lay[0]["duration"]
    for i in range(1, len(segs)):
        c = tl.clips[i]
        d = lay[i]["overlap"]
        nv, na = f"vj{i}", f"aj{i}"
        if c.transition.type == "cut" or d <= 0:
            fc.append(f"[{acc_v}][{acc_a}][v{i}][a{i}]concat=n=2:v=1:a=1[{nv}][{na}]")
            acc_d += lay[i]["duration"]
        else:
            off = acc_d - d
            fc.append(f"[{acc_v}][v{i}]xfade=transition={c.transition.type}:duration={d:.3f}:offset={off:.3f}[{nv}]")
            fc.append(f"[{acc_a}][a{i}]acrossfade=d={d:.3f}:c1=tri:c2=tri[{na}]")
            acc_d += lay[i]["duration"] - d
        acc_v, acc_a = nv, na
    ffmpeg(args + ["-filter_complex", ";".join(fc), "-map", f"[{acc_v}]", "-map", f"[{acc_a}]",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "12", "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le", str(out)])
    return out


# ============================================================================ stage 3: composite
def composite(p: Project, tl: Timeline, base: Path, out: Path, scale: float, quality: str, burn_captions: bool = True) -> Path:
    W, H = _even(tl.width * scale), _even(tl.height * scale)
    total = tl.duration
    args = ["-i", str(base)]
    fc: list[str] = []
    n_in = 1
    v = "0:v"
    # base may be shorter than overlays → pad
    fc.append(f"[{v}]tpad=stop_mode=add:stop_duration={max(0.0, total):.3f}:color=black,trim=duration={total:.3f}[vb]")
    v = "vb"
    ov_audio: list[str] = []
    for k, o in enumerate([o for o in tl.overlays if o.kind == "media"]):
        m = p.media(o.media)
        if m["kind"] == "image":
            args += ["-loop", "1", "-framerate", str(tl.fps), "-t", f"{o.duration:.3f}", "-i", m["path"]]
        else:
            args += ["-ss", f"{o.src_in:.3f}", "-t", f"{(0.2 if o.freeze else o.duration * o.speed):.3f}", "-i", m["path"]]
        idx = n_in; n_in += 1
        if o.full_frame:
            ow, oh = W, H
        else:
            ow = _even(W * o.scale)
            oh = _even(ow * (m.get("height", 9) / max(1, m.get("width", 16))))
        track = None
        if o.reframe == "auto" and m["kind"] == "video":
            track = [(t / o.speed, x) for t, x in analyze.subject_track(p, o.media, o.src_in, o.src_in + o.duration * o.speed)]
        head = (f"trim=end_frame=1,loop=loop={int(o.duration * tl.fps) + 2}:size=1:start=0,setpts=N/{tl.fps}/TB"
                if o.freeze and m["kind"] == "video" else
                (f"setpts=(PTS-STARTPTS)/{o.speed:.4f}" if m["kind"] != "image" else "setpts=PTS-STARTPTS"))
        chain = [head,
                 frame_filter(ow, oh, o.reframe if o.full_frame else "fill", track=track), f"fps={tl.fps}", "format=yuva420p"]
        if o.fade_in:
            chain.append(f"fade=t=in:st=0:d={o.fade_in:.3f}:alpha=1")
        if o.fade_out:
            chain.append(f"fade=t=out:st={max(0, o.duration - o.fade_out):.3f}:d={o.fade_out:.3f}:alpha=1")
        if o.opacity < 1:
            chain.append(f"colorchannelmixer=aa={o.opacity:.3f}")
        chain.append(f"setpts=PTS+{o.start:.3f}/TB")
        fc.append(f"[{idx}:v]{','.join(chain)}[ov{k}]")
        x = f"{(W - ow) / 2:.0f}" if o.full_frame else f"{max(0, min(W - ow, o.x * W - ow / 2)):.0f}"
        y = f"{(H - oh) / 2:.0f}" if o.full_frame else f"{max(0, min(H - oh, o.y * H - oh / 2)):.0f}"
        fc.append(f"[{v}][ov{k}]overlay=x={x}:y={y}:eof_action=pass:enable='between(t,{o.start:.3f},{o.start + o.duration:.3f})'[vo{k}]")
        v = f"vo{k}"
        if o.audio_gain_db is not None and m.get("has_audio"):
            fc.append(f"[{idx}:a]asetpts=PTS-STARTPTS,{','.join(_atempo(o.speed)) + ',' if _atempo(o.speed) else ''}"
                      f"aresample=48000,aformat=channel_layouts=stereo,volume={o.audio_gain_db:.2f}dB,"
                      f"afade=t=in:d={max(0.02, o.fade_in):.3f},afade=t=out:st={max(0, o.duration - max(0.02, o.fade_out)):.3f}:d={max(0.02, o.fade_out):.3f},"
                      f"adelay={int(o.start * 1000)}:all=1[oa{k}]")
            ov_audio.append(f"oa{k}")
    # global look, fades, captions
    post = grade_filters(tl.grade)
    if tl.fade_in:
        post.append(f"fade=t=in:st=0:d={tl.fade_in:.3f}")
    if tl.fade_out:
        post.append(f"fade=t=out:st={max(0, total - tl.fade_out):.3f}:d={tl.fade_out:.3f}")
    words = timeline_words(p, tl) if tl.captions.enabled else []
    has_text = burn_captions and (words or any(o.kind != "media" for o in tl.overlays))
    if has_text:
        ass = out.with_suffix(".ass")
        captions.build_ass(tl, words, ass)  # authored at timeline res; libass scales PlayRes → render size
        post.append(f"subtitles='{str(ass).replace(':', chr(92) + ':')}'")
    post.append("format=yuv420p")
    fc.append(f"[{v}]{','.join(post)}[vout]")

    # audio
    n_duck = sum(1 for a in tl.audio if a.duck)
    base_a = f"[0:a]volume={tl.dialogue_gain_db:.2f}dB,apad=whole_dur={total:.3f},atrim=duration={total:.3f}"
    if n_duck:
        fc.append(base_a + f",asplit={1 + n_duck}" + "".join(f"[dl{i}]" for i in range(1 + n_duck)))
    else:
        fc.append(base_a + "[dl0]")
    mix = ["dl0"] + ov_audio
    sc_i = 1
    for k, a in enumerate(tl.audio):
        m = p.media(a.media)
        src_left = m["duration"] - a.src_in
        dur = min(a.duration or src_left, src_left, max(0.05, total - a.start))
        args += ["-ss", f"{a.src_in:.3f}", "-t", f"{dur:.3f}", "-i", m["path"]]
        idx = n_in; n_in += 1
        chain = ["asetpts=PTS-STARTPTS", "aresample=48000", "aformat=sample_fmts=fltp:channel_layouts=stereo", f"volume={a.gain_db:.2f}dB"]
        if a.fade_in:
            chain.append(f"afade=t=in:d={a.fade_in:.3f}")
        fo = a.fade_out or 0.03
        chain.append(f"afade=t=out:st={max(0, dur - fo):.3f}:d={fo:.3f}")
        chain.append(f"adelay={int(a.start * 1000)}:all=1")
        fc.append(f"[{idx}:a]{','.join(chain)}[mx{k}]")
        lab = f"mx{k}"
        if a.duck:
            ratio = max(2.0, min(20.0, tl.duck_db / 1.2))
            fc.append(f"[{lab}][dl{sc_i}]sidechaincompress=threshold=0.025:ratio={ratio:.1f}:attack=25:release=450:makeup=1[dk{k}]")
            lab = f"dk{k}"; sc_i += 1
        mix.append(lab)
    fc.append(f"{''.join(f'[{x}]' for x in mix)}amix=inputs={len(mix)}:normalize=0:duration=first,"
              f"atrim=duration={total:.3f},alimiter=limit=0.95:level=disabled[aout]")
    ffmpeg(args + ["-filter_complex", ";".join(fc), "-map", "[vout]", "-map", "[aout]",
                   *QUALITY[quality][0], "-c:a", "pcm_s24le", "-t", f"{total:.3f}", str(out)])
    return out


# ============================================================================ stage 4: loudness
def measure_loudness(path: Path) -> dict:
    r = run(["ffmpeg", "-hide_banner", "-nostdin", "-i", str(path), "-vn", "-af", "loudnorm=print_format=json", "-f", "null", "-"], check=False)
    e = r.stderr
    return json.loads(e[e.rfind("{"): e.rfind("}") + 1])


def finalize_audio(src: Path, out: Path, target: float, quality: str) -> dict:
    ms = measure_loudness(src)
    tp = -1.0 if target > -20 else -2.0
    if float(ms["input_i"]) < -70:  # silent
        af = "anull"
    else:
        af = (f"loudnorm=I={target}:TP={tp}:LRA=11:measured_I={ms['input_i']}:measured_TP={ms['input_tp']}:"
              f"measured_LRA={ms['input_lra']}:measured_thresh={ms['input_thresh']}:offset={ms['target_offset']}:linear=true,aresample=48000")
    ffmpeg(["-i", str(src), "-map", "0:v", "-map", "0:a", "-c:v", "copy", "-af", af, *QUALITY[quality][1],
            *(["-movflags", "+faststart"] if out.suffix == ".mp4" else []), str(out)])
    after = measure_loudness(out)
    return {"before_lufs": float(ms["input_i"]), "after_lufs": float(after["input_i"]), "true_peak": float(after["input_tp"])}


# ============================================================================ entry point
def render(p: Project, tl: Timeline, quality: str = "preview", out: str | None = None, scale: float | None = None,
           start: float | None = None, end: float | None = None, burn_captions: bool = True) -> dict:
    """Render a timeline. quality: preview (half-res, fast) | final (H.264) | hevc | master (ProRes HQ) | web (VP9)."""
    if not tl.clips and not tl.overlays:
        raise EditorError("timeline is empty")
    if quality not in QUALITY:
        raise EditorError(f"quality must be one of {list(QUALITY)}")
    scale = scale if scale is not None else (0.5 if quality == "preview" else 1.0)
    work = p.root / "cache" / "_render" / tl.name
    work.mkdir(parents=True, exist_ok=True)
    rtl = tl
    if start is not None or end is not None:  # render a section (for quick review)
        rtl = section(tl, start or 0.0, end or tl.duration)
    segs = [render_segment(p, rtl, c, scale, quality) for c in rtl.clips]
    joined = join(p, rtl, segs, work / "joined.mkv") if segs else None
    if joined is None:
        joined = work / "black.mkv"
        ffmpeg(["-f", "lavfi", "-i", f"color=c=black:s={_even(rtl.width*scale)}x{_even(rtl.height*scale)}:r={rtl.fps}:d={rtl.duration}",
                "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo", "-t", str(rtl.duration), "-c:v", "libx264", "-c:a", "pcm_s16le", str(joined)])
    ext = QUALITY[quality][2]
    comp = composite(p, rtl, joined, work / f"composite_{quality}{'.mov' if quality == 'master' else '.mkv'}", scale, quality, burn_captions)
    dest = Path(out) if out else p.root / "renders" / f"{tl.name}_v{tl.version}_{quality}{ext}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    loud = finalize_audio(comp, dest, tl.loudness_target, quality)
    if tl.captions.enabled or tl.height <= tl.width:
        words = timeline_words(p, rtl)
        if words:
            captions.srt(words, dest.with_suffix(".srt"))
    return {"path": str(dest), "duration": round(rtl.duration, 3), "size": f"{_even(rtl.width*scale)}x{_even(rtl.height*scale)}",
            "quality": quality, "loudness": loud, "segments_cached": len(segs)}


def section(tl: Timeline, start: float, end: float) -> Timeline:
    """Sub-timeline covering [start, end] of the A-roll (overlays/audio shifted accordingly)."""
    s = tl.model_copy(deep=True)
    lay = tl.layout()
    keep = []
    for c, l in zip(s.clips, lay):
        if l["end"] <= start or l["start"] >= end:
            continue
        if l["start"] < start:
            c.src_in += (start - l["start"]) * c.speed
        if l["end"] > end:
            c.src_out -= (l["end"] - end) * c.speed
        keep.append(c)
    if keep:
        keep[0].transition.type, keep[0].transition.duration = "cut", 0
    s.clips = keep
    s.overlays = [o.model_copy(update={"start": o.start - start}) for o in s.overlays if o.start < end and o.start + o.duration > start]
    for o in s.overlays:
        if o.start < 0:
            o.src_in -= o.start * o.speed if o.kind == "media" else 0
            o.duration += o.start; o.start = 0
    s.audio = []
    for a in tl.audio:
        a2 = a.model_copy(deep=True)
        if a2.start < start:
            a2.src_in += start - a2.start
            if a2.duration:
                a2.duration -= start - a2.start
            a2.start = 0
        else:
            a2.start -= start
        if a2.start < end - start and (a2.duration is None or a2.duration > 0):
            s.audio.append(a2)
    return s
