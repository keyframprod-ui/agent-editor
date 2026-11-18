"""Project layout, media registry and FFmpeg helpers.

A *project* is a directory:

    <project>/
      project.json          media registry + settings
      media/                ingested source files (or symlinks)
      cache/<media_id>/     analysis results (probe, scenes, transcript, audio, faces, frames)
      timelines/<name>.json editable timelines (see timeline.py)
      renders/              outputs
      exports/              OTIO / FCPXML / XML / EDL interchange
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".mxf", ".avi", ".webm", ".m4v", ".mts", ".m2ts", ".r3d", ".braw"}
AUDIO_EXT = {".wav", ".mp3", ".aac", ".m4a", ".flac", ".ogg", ".aif", ".aiff", ".opus"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".exr"}


class EditorError(RuntimeError):
    pass


def run(cmd: list[str], *, quiet: bool = True, check: bool = True) -> subprocess.CompletedProcess:
    """Run a subprocess, raising EditorError with stderr tail on failure."""
    p = subprocess.run(cmd, capture_output=True, text=True)
    if check and p.returncode != 0:
        tail = "\n".join(p.stderr.strip().splitlines()[-25:])
        raise EditorError(f"command failed ({p.returncode}): {' '.join(cmd[:6])} ...\n{tail}")
    return p


def ffmpeg(args: list[str]) -> subprocess.CompletedProcess:
    return run(["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error", *args])


def probe(path: str | Path) -> dict[str, Any]:
    """Summarised ffprobe info: duration, fps, size, codecs, audio layout, rotation, timecode."""
    p = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)])
    data = json.loads(p.stdout)
    fmt = data.get("format", {})
    info: dict[str, Any] = {
        "path": str(path),
        "duration": float(fmt.get("duration", 0) or 0),
        "size_bytes": int(fmt.get("size", 0) or 0),
        "container": fmt.get("format_name"),
        "has_video": False,
        "has_audio": False,
    }
    for s in data.get("streams", []):
        if s["codec_type"] == "video" and not info["has_video"] and s.get("disposition", {}).get("attached_pic", 0) == 0:
            num, den = (s.get("avg_frame_rate") or s.get("r_frame_rate") or "0/1").split("/")
            fps = float(num) / float(den) if float(den) else 0.0
            rot = 0
            for sd in s.get("side_data_list", []) or []:
                if "rotation" in sd:
                    rot = int(sd["rotation"])
            rot = int(s.get("tags", {}).get("rotate", rot))
            w, h = s.get("width", 0), s.get("height", 0)
            if abs(rot) in (90, 270):
                w, h = h, w
            info.update(
                has_video=True, width=w, height=h, fps=round(fps, 3), vcodec=s.get("codec_name"),
                pix_fmt=s.get("pix_fmt"), rotation=rot, bit_depth=s.get("bits_per_raw_sample"),
                color_transfer=s.get("color_transfer"), color_primaries=s.get("color_primaries"),
            )
            if not info["duration"]:
                info["duration"] = float(s.get("duration", 0) or 0)
        elif s["codec_type"] == "audio" and not info["has_audio"]:
            info.update(has_audio=True, acodec=s.get("codec_name"), sample_rate=int(s.get("sample_rate", 0) or 0),
                        channels=s.get("channels"))
    tc = fmt.get("tags", {}).get("timecode") or next(
        (s.get("tags", {}).get("timecode") for s in data.get("streams", []) if s.get("tags", {}).get("timecode")), None)
    if tc:
        info["timecode"] = tc
    ext = Path(path).suffix.lower()
    info["kind"] = "video" if info["has_video"] and ext not in IMAGE_EXT else ("image" if ext in IMAGE_EXT else "audio")
    return info


def slug(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").lower()
    return s[:40] or "media"


@dataclass
class Project:
    root: Path

    # ---------- lifecycle ----------
    @classmethod
    def open(cls, root: str | Path, create: bool = True) -> "Project":
        root = Path(root).expanduser().resolve()
        pj = root / "project.json"
        if not pj.exists():
            if not create:
                raise EditorError(f"no project at {root}")
            for d in ("media", "cache", "timelines", "renders", "exports"):
                (root / d).mkdir(parents=True, exist_ok=True)
            pj.write_text(json.dumps({"name": root.name, "media": {}, "settings": {}}, indent=2))
        return cls(root)

    @property
    def data(self) -> dict[str, Any]:
        return json.loads((self.root / "project.json").read_text())

    def save(self, data: dict[str, Any]) -> None:
        tmp = self.root / "project.json.tmp"
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.root / "project.json")

    # ---------- media ----------
    def ingest(self, src: str, media_id: str | None = None, copy: bool = False, tags: list[str] | None = None) -> dict:
        """Register a file (local path or http(s) URL). Returns media record."""
        dest_dir = self.root / "media"
        if re.match(r"^https?://", src):
            name = Path(src.split("?")[0]).name or "download.mp4"
            dest = dest_dir / name
            if not dest.exists():
                req = urllib.request.Request(src, headers={"User-Agent": "agent-editor/0.1"})
                with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
                    shutil.copyfileobj(r, f)
            path = dest
        else:
            path = Path(src).expanduser().resolve()
            if not path.exists():
                raise EditorError(f"file not found: {src}")
            if copy or not str(path).startswith(str(self.root)):
                dest = dest_dir / path.name
                if not dest.exists():
                    if copy:
                        shutil.copy2(path, dest)
                    else:
                        os.symlink(path, dest)
                path = dest
        info = probe(path)
        data = self.data
        mid = media_id or slug(path.stem)
        base, n = mid, 2
        while mid in data["media"] and data["media"][mid]["path"] != str(path):
            mid, n = f"{base}_{n}", n + 1
        rec = {"id": mid, "path": str(path), "tags": tags or [], **{k: v for k, v in info.items() if k != "path"}}
        data["media"][mid] = rec
        self.save(data)
        self.cache_dir(mid).mkdir(parents=True, exist_ok=True)
        (self.cache_dir(mid) / "probe.json").write_text(json.dumps(info, indent=2))
        return rec

    def media(self, mid: str) -> dict:
        m = self.data["media"].get(mid)
        if not m:
            raise EditorError(f"unknown media id '{mid}'. Known: {', '.join(self.data['media']) or '(none)'}")
        return m

    def media_path(self, mid: str) -> str:
        return self.media(mid)["path"]

    def cache_dir(self, mid: str) -> Path:
        return self.root / "cache" / mid

    def cache_get(self, mid: str, key: str) -> Any | None:
        f = self.cache_dir(mid) / f"{key}.json"
        return json.loads(f.read_text()) if f.exists() else None

    def cache_put(self, mid: str, key: str, value: Any) -> None:
        d = self.cache_dir(mid)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{key}.json").write_text(json.dumps(value, indent=2))

    def fingerprint(self) -> str:
        return hashlib.sha1(json.dumps(sorted(self.data["media"])).encode()).hexdigest()[:8]


def fmt_tc(seconds: float, fps: float = 0) -> str:
    """HH:MM:SS.mmm (or HH:MM:SS:FF when fps given)."""
    seconds = max(0.0, seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if fps:
        return f"{int(h):02d}:{int(m):02d}:{int(s):02d}:{int(round((s % 1) * fps)) % int(round(fps)):02d}"
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"
