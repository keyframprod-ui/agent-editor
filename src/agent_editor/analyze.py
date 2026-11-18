"""Footage understanding: scenes, transcripts, audio, faces, frames, AI shot logging, search.

Everything is cached per media id in <project>/cache/<id>/<key>.json, so repeated
calls are free and the agent can analyse lazily.
"""
from __future__ import annotations

import base64
import json
import math
import os
import re
from pathlib import Path
from typing import Any

import numpy as np

from .core import EditorError, Project, ffmpeg, fmt_tc, run

os.environ.setdefault("HF_HUB_DISABLE_XET", "1")  # xet CAS host is not on the network allowlist

FILLERS = {"um", "uh", "erm", "er", "ah", "hmm", "mm", "uhm", "umm", "like", "you know", "i mean", "sort of", "kind of"}
HARD_FILLERS = {"um", "uh", "erm", "er", "ah", "hmm", "mm", "uhm", "umm"}


# ----------------------------------------------------------------------------- scenes
def scenes(p: Project, mid: str, threshold: float = 27.0, min_len: float = 0.6, force: bool = False) -> list[dict]:
    """Shot boundaries via PySceneDetect (content detector + adaptive fallback)."""
    if not force and (c := p.cache_get(mid, "scenes")) is not None:
        return c
    m = p.media(mid)
    if m["kind"] != "video":
        return []
    from scenedetect import AdaptiveDetector, ContentDetector, SceneManager, open_video

    video = open_video(m["path"])
    fps = video.frame_rate
    sm = SceneManager()
    sm.add_detector(ContentDetector(threshold=threshold, min_scene_len=max(1, int(min_len * fps))))
    sm.auto_downscale = True
    sm.detect_scenes(video, show_progress=False)
    lst = sm.get_scene_list()
    if not lst:
        out = [{"index": 0, "start": 0.0, "end": round(m["duration"], 3)}]
    else:
        out = [{"index": i, "start": round(a.get_seconds(), 3), "end": round(b.get_seconds(), 3)} for i, (a, b) in enumerate(lst)]
    for s in out:
        s["duration"] = round(s["end"] - s["start"], 3)
    p.cache_put(mid, "scenes", out)
    return out


# ----------------------------------------------------------------------------- transcription
_WHISPER: dict[str, Any] = {}


def _whisper(model: str):
    if model not in _WHISPER:
        from faster_whisper import WhisperModel

        _WHISPER[model] = WhisperModel(model, device="cpu", compute_type="int8", cpu_threads=os.cpu_count() or 4)
    return _WHISPER[model]


def transcribe(p: Project, mid: str, model: str | None = None, language: str | None = None, force: bool = False) -> dict:
    """Word-timestamped transcript with Whisper (local, CPU). Model: small (fast) | large-v3-turbo (best)."""
    if not force and (c := p.cache_get(mid, "transcript")) is not None:
        return c
    m = p.media(mid)
    if not m.get("has_audio"):
        out = {"language": None, "segments": [], "words": [], "text": ""}
        p.cache_put(mid, "transcript", out)
        return out
    model = model or os.environ.get("AE_WHISPER_MODEL", "large-v3-turbo")
    wav = p.cache_dir(mid) / "audio16k.wav"
    if not wav.exists():
        ffmpeg(["-i", m["path"], "-vn", "-ac", "1", "-ar", "16000", str(wav)])
    import soundfile as sf

    samples, _sr = sf.read(str(wav), dtype="float32")  # pass samples directly (avoids PyAV version issues)
    segs, info = _whisper(model).transcribe(
        samples, language=language, word_timestamps=True, vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 400}, condition_on_previous_text=False,
    )
    segments, words = [], []
    for s in segs:
        seg = {"id": len(segments), "start": round(s.start, 3), "end": round(s.end, 3), "text": s.text.strip(), "words": []}
        for w in s.words or []:
            wd = {"i": len(words), "w": w.word.strip(), "start": round(w.start, 3), "end": round(w.end, 3), "p": round(w.probability, 2), "seg": seg["id"]}
            words.append(wd)
            seg["words"].append(wd["i"])
        segments.append(seg)
    out = {"language": info.language, "model": model, "segments": segments, "words": words,
           "text": " ".join(s["text"] for s in segments)}
    p.cache_put(mid, "transcript", out)
    return out


def transcript_text(p: Project, mid: str, with_times: bool = True) -> str:
    t = transcribe(p, mid)
    if not with_times:
        return t["text"]
    return "\n".join(f"[{fmt_tc(s['start'])}–{fmt_tc(s['end'])}] (words {s['words'][0] if s['words'] else '-'}–{s['words'][-1] if s['words'] else '-'}) {s['text']}" for s in t["segments"])


def find_phrase(p: Project, mid: str, phrase: str) -> list[dict]:
    """Locate a phrase in a transcript (case/punct-insensitive). Returns word-index ranges with times."""
    t = transcribe(p, mid)
    norm = lambda s: re.sub(r"[^\w']+", " ", s.lower()).split()
    target = norm(phrase)
    toks = [norm(w["w"]) for w in t["words"]]
    flat = [(i, tok) for i, ts in enumerate(toks) for tok in ts]
    hits = []
    for k in range(len(flat) - len(target) + 1):
        if [x[1] for x in flat[k:k + len(target)]] == target:
            a, b = flat[k][0], flat[k + len(target) - 1][0]
            hits.append({"word_start": a, "word_end": b, "start": t["words"][a]["start"], "end": t["words"][b]["end"],
                         "context": " ".join(w["w"] for w in t["words"][max(0, a - 8):b + 9])})
    return hits


def words_range(p: Project, mid: str, word_start: int, word_end: int, pad: float = 0.08) -> tuple[float, float]:
    t = transcribe(p, mid)
    ws = t["words"]
    if not (0 <= word_start <= word_end < len(ws)):
        raise EditorError(f"word range {word_start}-{word_end} out of bounds (0-{len(ws) - 1})")
    a = max(0.0, ws[word_start]["start"] - pad)
    b = ws[word_end]["end"] + pad
    # don't bleed into neighbouring words
    if word_start > 0:
        a = max(a, (ws[word_start - 1]["end"] + ws[word_start]["start"]) / 2)
    if word_end + 1 < len(ws):
        b = min(b, (ws[word_end]["end"] + ws[word_end + 1]["start"]) / 2)
    return round(a, 3), round(b, 3)


def fillers(p: Project, mid: str, aggressive: bool = False) -> list[dict]:
    t = transcribe(p, mid)
    bag = FILLERS if aggressive else HARD_FILLERS
    out = []
    for w in t["words"]:
        if re.sub(r"[^\w' ]", "", w["w"].lower()).strip() in bag:
            out.append(w)
    return out


# ----------------------------------------------------------------------------- audio
def audio(p: Project, mid: str, force: bool = False) -> dict:
    """Loudness (EBU R128), silences, tempo + beat grid, energy curve."""
    if not force and (c := p.cache_get(mid, "audio")) is not None:
        return c
    m = p.media(mid)
    if not m.get("has_audio"):
        return {}
    out: dict[str, Any] = {}
    r = run(["ffmpeg", "-hide_banner", "-nostdin", "-i", m["path"], "-vn", "-af",
             "loudnorm=print_format=json,silencedetect=n=-38dB:d=0.35", "-f", "null", "-"], check=False)
    err = r.stderr
    j = err[err.rfind("{"): err.rfind("}") + 1]
    try:
        ln = json.loads(j)
        out["loudness"] = {"integrated_lufs": float(ln["input_i"]), "true_peak_dbtp": float(ln["input_tp"]),
                           "lra": float(ln["input_lra"]), "threshold": float(ln["input_thresh"])}
    except Exception:
        out["loudness"] = None
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", err)]
    ends = [(float(a), float(b)) for a, b in re.findall(r"silence_end: ([\d.]+) \| silence_duration: ([\d.]+)", err)]
    sil = []
    for i, s in enumerate(starts):
        e = ends[i][0] if i < len(ends) else m["duration"]
        sil.append({"start": round(s, 3), "end": round(e, 3)})
    out["silences"] = sil

    import librosa

    wav = p.cache_dir(mid) / "audio22k.wav"
    if not wav.exists():
        ffmpeg(["-i", m["path"], "-vn", "-ac", "1", "-ar", "22050", str(wav)])
    y, sr = librosa.load(str(wav), sr=22050, mono=True)
    if len(y) > sr:
        tempo, beats = librosa.beat.beat_track(y=y, sr=sr, units="time")
        onset_env = librosa.onset.onset_strength(y=y, sr=sr)
        tempo = float(np.atleast_1d(tempo)[0])
        beats = np.asarray(beats, dtype=float)
        if len(beats) >= 8:
            iv = np.diff(beats)
            if iv.std() / (iv.mean() + 1e-9) < 0.15:  # steady tempo → fine-search period & phase on the onset envelope
                hop = 128
                env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
                low = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop, fmax=180, n_mels=32)
                env = env / (env.max() + 1e-9) + 2.0 * low / (low.max() + 1e-9)  # kick/bass carries the beat
                fr = sr / hop
                T0 = float(np.median(iv))
                best = (-1.0, T0, beats[0])
                for T in np.linspace(T0 * 0.94, T0 * 1.06, 241):
                    for ph in np.linspace(0, T, 48, endpoint=False):
                        g = np.arange(ph, len(y) / sr, T)
                        sc = env[np.clip((g * fr).astype(int), 0, len(env) - 1)].mean()
                        if sc > best[0]:
                            best = (sc, T, ph)
                _, T, ph = best
                beats = np.arange(ph, len(y) / sr - 0.05, T)
                tempo = 60.0 / T
                out["grid"] = "fitted"
        out["tempo_bpm"] = round(tempo, 1)
        out["beats"] = [round(float(b), 3) for b in beats]
        # downbeats: crude — strongest of each group of 4
        if len(beats) >= 8:
            bf = librosa.time_to_frames(beats, sr=sr)
            strengths = onset_env[np.clip(bf, 0, len(onset_env) - 1)]
            phase = int(np.argmax([strengths[k::4].mean() for k in range(4)]))
            out["downbeats"] = out["beats"][phase::4]
        rms = librosa.feature.rms(y=y, hop_length=sr // 2)[0]
        out["energy_per_0.5s"] = [round(float(v), 4) for v in rms / (rms.max() + 1e-9)]
    p.cache_put(mid, "audio", out)
    return out


# ----------------------------------------------------------------------------- frames & faces
def frame_at(p: Project, mid: str, t: float, width: int = 640) -> Path:
    out = p.cache_dir(mid) / "frames" / f"{t:010.3f}_{width}.jpg"
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        ffmpeg(["-ss", f"{t:.3f}", "-i", p.media_path(mid), "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "3", str(out)])
    return out


def contact_sheet(images: list[Path], labels: list[str], out: Path, cols: int = 4, thumb_w: int = 400) -> Path:
    from PIL import Image, ImageDraw, ImageFont

    if not images:
        raise EditorError("no frames for contact sheet")
    ims = [Image.open(i).convert("RGB") for i in images]
    th = int(thumb_w * ims[0].height / ims[0].width)
    rows = math.ceil(len(ims) / cols)
    pad, lab = 6, 22
    sheet = Image.new("RGB", (cols * (thumb_w + pad) + pad, rows * (th + lab + pad) + pad), (18, 18, 20))
    d = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
    except Exception:
        font = ImageFont.load_default()
    for k, (im, l) in enumerate(zip(ims, labels)):
        r, c = divmod(k, cols)
        x, y = pad + c * (thumb_w + pad), pad + r * (th + lab + pad)
        sheet.paste(im.resize((thumb_w, th)), (x, y + lab))
        d.text((x + 2, y + 3), l, fill=(235, 235, 235), font=font)
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, quality=85)
    return out


def scene_sheet(p: Project, mid: str, max_frames: int = 24) -> Path:
    """Contact sheet with one mid-frame per shot (labelled with shot # and timecode)."""
    sc = scenes(p, mid)
    if len(sc) > max_frames:
        idx = np.linspace(0, len(sc) - 1, max_frames).round().astype(int)
        sc = [sc[i] for i in idx]
    frames = [frame_at(p, mid, (s["start"] + s["end"]) / 2, 400) for s in sc]
    labels = [f"#{s['index']}  {fmt_tc(s['start'])}  ({s['duration']:.1f}s)" for s in sc]
    return contact_sheet(frames, labels, p.cache_dir(mid) / "scene_sheet.jpg")


_YUNET = None


def _face_detector():
    """YuNet (OpenCV DNN) face detector — model pulled once from the Hugging Face hub."""
    global _YUNET
    if _YUNET is None:
        import cv2
        from huggingface_hub import hf_hub_download

        model = hf_hub_download("opencv/face_detection_yunet", "face_detection_yunet_2023mar.onnx")
        _YUNET = cv2.FaceDetectorYN.create(model, "", (320, 320), 0.75, 0.3, 50)
    return _YUNET


def faces(p: Project, mid: str, step: float = 0.5, force: bool = False) -> list[dict]:
    """Face track sampled every `step` s: [{t, faces:[{x,y,w,h,score}]}] normalised 0-1, largest first."""
    if not force and (c := p.cache_get(mid, "faces")) is not None:
        return c
    import cv2

    m = p.media(mid)
    det = _face_detector()
    cap = cv2.VideoCapture(m["path"])
    native_fps = cap.get(cv2.CAP_PROP_FPS) or m.get("fps") or 25
    every = max(1, int(round(step * native_fps)))
    out, k = [], 0
    while True:
        ok = cap.grab()
        if not ok:
            break
        if k % every == 0:
            ok, fr = cap.retrieve()
            if not ok:
                break
            h, w = fr.shape[:2]
            sw = 640
            sh = int(h * sw / w)
            small = cv2.resize(fr, (sw, sh))
            det.setInputSize((sw, sh))
            _, res = det.detect(small)
            fl = []
            for r in (res if res is not None else []):
                x, y, fw, fh = r[:4]
                fl.append({"x": round(float(x) / sw, 4), "y": round(float(y) / sh, 4), "w": round(float(fw) / sw, 4),
                           "h": round(float(fh) / sh, 4), "score": round(float(r[-1]), 3)})
            fl.sort(key=lambda f: -f["w"] * f["h"])
            out.append({"t": round(k / native_fps, 3), "faces": fl[:4]})
        k += 1
    cap.release()
    p.cache_put(mid, "faces", out)
    return out


def subject_track(p: Project, mid: str, start: float, end: float, smooth: float = 1.5) -> list[tuple[float, float]]:
    """Smoothed horizontal subject centre (0-1) over [start,end] → [(t_rel, cx)]. Falls back to 0.5."""
    fs = [f for f in faces(p, mid) if start - 0.5 <= f["t"] <= end + 0.5]
    pts = []
    last = 0.5
    for f in fs:
        if f["faces"]:
            big = f["faces"][0]
            last = big["x"] + big["w"] / 2
        pts.append((f["t"], last))
    if not pts:
        return [(0.0, 0.5), (end - start, 0.5)]
    ts = np.array([q[0] for q in pts]); xs = np.array([q[1] for q in pts])
    # gaussian smoothing for a calm virtual camera
    if len(xs) > 2:
        step = max(1e-3, float(np.median(np.diff(ts))))
        sigma = smooth / step
        k = np.arange(-3 * int(sigma + 1), 3 * int(sigma + 1) + 1)
        ker = np.exp(-(k ** 2) / (2 * sigma ** 2)); ker /= ker.sum()
        xs = np.convolve(np.pad(xs, len(k) // 2, mode="edge"), ker, mode="valid")[: len(ts)]
    return [(round(float(t - start), 3), round(float(x), 4)) for t, x in zip(ts, xs) if start - 0.01 <= t <= end + 0.01] or [(0.0, float(xs[0]))]


# ----------------------------------------------------------------------------- AI shot logging
SHOT_LOG_PROMPT = """You are a first assistant editor logging footage. For EACH numbered frame (each is the middle of one shot), return JSON:
{"shots":[{"index":<shot #>,"shot_size":"ECU|CU|MCU|MS|MWS|WS|EWS|insert","angle":"eye|high|low|overhead|dutch|pov","movement_guess":"static|pan|tilt|handheld|dolly|drone|unknown",
"subjects":"who/what","action":"what happens","setting":"where, time of day","mood":"...","quality_issues":"focus/exposure/shake/none","b_roll_value":1-5,"hero_value":1-5,"tags":["..."]}]}
Be concise and concrete. Output JSON only."""


def describe_shots(p: Project, mid: str, model: str | None = None, force: bool = False) -> dict:
    """AI shot log using Claude vision on per-shot keyframes. Requires ANTHROPIC_API_KEY.

    Inside Claude Code without a key, call `scene_sheet` and look at the image yourself instead.
    """
    if not force and (c := p.cache_get(mid, "shotlog")) is not None:
        return c
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise EditorError("ANTHROPIC_API_KEY not set — view scene_sheet() image and log shots yourself, then save with save_shot_log().")
    import anthropic

    sc = scenes(p, mid)
    client = anthropic.Anthropic()
    model = model or os.environ.get("AE_VISION_MODEL", "claude-sonnet-5-5")
    shots: list[dict] = []
    for b in range(0, len(sc), 16):
        batch = sc[b:b + 16]
        content: list[dict] = []
        for s in batch:
            img = frame_at(p, mid, (s["start"] + s["end"]) / 2, 768)
            content.append({"type": "text", "text": f"Shot #{s['index']} ({fmt_tc(s['start'])}, {s['duration']:.1f}s):"})
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(img.read_bytes()).decode()}})
        content.append({"type": "text", "text": SHOT_LOG_PROMPT})
        msg = client.messages.create(model=model, max_tokens=4000, messages=[{"role": "user", "content": content}])
        txt = "".join(c.text for c in msg.content if c.type == "text")
        txt = txt[txt.find("{"): txt.rfind("}") + 1]
        try:
            shots.extend(json.loads(txt)["shots"])
        except Exception:
            pass
    by = {s["index"]: s for s in sc}
    for s in shots:
        if s.get("index") in by:
            s.update({k: by[s["index"]][k] for k in ("start", "end", "duration")})
    log = {"model": model, "shots": shots}
    p.cache_put(mid, "shotlog", log)
    return log


def save_shot_log(p: Project, mid: str, shots: list[dict]) -> dict:
    """Store a shot log written by the agent itself (same schema as describe_shots)."""
    sc = {s["index"]: s for s in scenes(p, mid)}
    for s in shots:
        if s.get("index") in sc:
            s.update({k: sc[s["index"]][k] for k in ("start", "end", "duration")})
    log = {"model": "agent", "shots": shots}
    p.cache_put(mid, "shotlog", log)
    return log


# ----------------------------------------------------------------------------- overview & search
def analyze_all(p: Project, mid: str, transcribe_audio: bool = True, do_faces: bool = False) -> dict:
    m = p.media(mid)
    res: dict[str, Any] = {"media": mid, "kind": m["kind"], "duration": m["duration"]}
    if m["kind"] == "video":
        res["shots"] = len(scenes(p, mid))
        if do_faces:
            res["face_samples_with_faces"] = sum(1 for f in faces(p, mid) if f["faces"])
    if m.get("has_audio"):
        a = audio(p, mid)
        res["loudness"] = a.get("loudness")
        res["tempo_bpm"] = a.get("tempo_bpm")
        if transcribe_audio:
            t = transcribe(p, mid)
            res["language"] = t["language"]
            res["words"] = len(t["words"])
            res["transcript_preview"] = t["text"][:400]
    return res


def overview(p: Project) -> str:
    """Compact text bin listing — what an editor sees in the browser."""
    lines = []
    for mid, m in p.data["media"].items():
        bits = [f"{mid}: {m['kind']} {m['duration']:.1f}s"]
        if m["kind"] == "video":
            bits.append(f"{m.get('width')}x{m.get('height')}@{m.get('fps')}")
        if m.get("tags"):
            bits.append("tags=" + ",".join(m["tags"]))
        sc = p.cache_get(mid, "scenes")
        if sc:
            bits.append(f"{len(sc)} shots")
        a = p.cache_get(mid, "audio")
        if a and a.get("tempo_bpm"):
            bits.append(f"{a['tempo_bpm']}bpm")
        t = p.cache_get(mid, "transcript")
        if t and t["words"]:
            bits.append(f'"{t["text"][:90]}…"')
        lines.append("  ".join(bits))
    return "\n".join(lines) or "(no media — ingest some)"


def search(p: Project, query: str, limit: int = 20) -> list[dict]:
    """Keyword search across transcripts and shot logs."""
    q = [w for w in re.sub(r"[^\w ]", " ", query.lower()).split() if w]
    hits = []
    for mid in p.data["media"]:
        t = p.cache_get(mid, "transcript")
        if t:
            for s in t["segments"]:
                txt = s["text"].lower()
                sc = sum(txt.count(w) for w in q)
                if sc:
                    hits.append({"media": mid, "type": "dialogue", "start": s["start"], "end": s["end"], "text": s["text"], "score": sc})
        log = p.cache_get(mid, "shotlog")
        if log:
            for s in log["shots"]:
                blob = json.dumps(s).lower()
                sc = sum(blob.count(w) for w in q)
                if sc:
                    hits.append({"media": mid, "type": "shot", "start": s.get("start"), "end": s.get("end"),
                                 "text": f"{s.get('shot_size')} {s.get('subjects')} — {s.get('action')}", "score": sc})
    hits.sort(key=lambda h: -h["score"])
    return hits[:limit]
