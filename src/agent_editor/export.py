"""Interchange: timeline → OpenTimelineIO → FCPXML (Final Cut / Resolve), FCP7 XML (Premiere / Resolve), CMX EDL."""
from __future__ import annotations

from pathlib import Path

import opentimelineio as otio

from .core import Project
from .timeline import Timeline

FORMATS = {"otio": ("otio_json", ".otio"), "fcpxml": ("fcpx_xml", ".fcpxml"), "premiere": ("fcp_xml", ".xml"),
           "edl": ("cmx_3600", ".edl")}


def _rt(sec: float, rate: float) -> otio.opentime.RationalTime:
    return otio.opentime.RationalTime(round(sec * rate), rate)


def _range(a: float, dur: float, rate: float) -> otio.opentime.TimeRange:
    return otio.opentime.TimeRange(_rt(a, rate), _rt(dur, rate))


def seq_rate(fps: float) -> float:
    """Exact NLE rate: 29.97 → 30000/1001 etc. (Premiere writes these as timebase 30 + NTSC)."""
    for nominal in (24, 30, 60, 120):
        if abs(fps - nominal * 1000 / 1001) < 0.01:
            return nominal * 1000 / 1001
    return float(round(fps, 3))


_PATH_MAP: dict[str, str] = {}


def _media_path(m: dict) -> str:
    """Real source file, with optional prefix remapping (e.g. cloud path → path on the editor's Mac)."""
    path = m.get("source_path") or str(Path(m["path"]).resolve())
    for a, b in _PATH_MAP.items():
        if path.startswith(a):
            path = b + path[len(a):]
            break
    return path


def _ref(p: Project, mid: str, rate: float) -> otio.schema.ExternalReference:
    m = p.media(mid)
    path = _media_path(m)
    url = Path(path).as_uri() if path.startswith("/") else "file:///" + path.replace("\\", "/")
    dur = 3600.0 if m.get("kind") == "image" else m["duration"]  # stills can be held indefinitely
    return otio.schema.ExternalReference(target_url=url, available_range=_range(0, dur, rate))


def to_otio(p: Project, tl: Timeline) -> otio.schema.Timeline:
    rate = seq_rate(tl.fps)
    ot = otio.schema.Timeline(name=tl.name, global_start_time=_rt(3600, rate))  # 01:00:00:00 like a real sequence
    ot.metadata["agent_editor"] = {"width": tl.width, "height": tl.height, "loudness_target": tl.loudness_target}
    v1 = otio.schema.Track(name="V1 A-roll", kind=otio.schema.TrackKind.Video)
    a1 = otio.schema.Track(name="A1 Dialogue", kind=otio.schema.TrackKind.Audio)
    lay = tl.layout()
    for i, (c, l) in enumerate(zip(tl.clips, lay)):
        m = p.media(c.media)
        mrate = rate
        head = l["overlap"] / 2 if i > 0 else 0.0
        nxt = lay[i + 1]["overlap"] / 2 if i + 1 < len(lay) else 0.0
        src_in = c.src_in + head * c.speed
        dur = c.duration - head - nxt
        if i > 0 and l["overlap"] > 0:
            half = otio.opentime.RationalTime(int(l["overlap"] / 2 * mrate), mrate)  # floor at media rate: never exceed handles
            tr = otio.schema.Transition(name=c.transition.type, transition_type=otio.schema.TransitionTypes.SMPTE_Dissolve,
                                        in_offset=half, out_offset=half)
            v1.append(tr)
            a1.append(otio.schema.Transition(transition_type=otio.schema.TransitionTypes.SMPTE_Dissolve, in_offset=half, out_offset=half))
        for track, kind in ((v1, "v"), (a1, "a")):
            clip = otio.schema.Clip(name=c.label or f"{c.media}_{i:03d}", media_reference=_ref(p, c.media, rate),
                                    source_range=otio.opentime.TimeRange(_rt(src_in, mrate), _rt(dur * c.speed, mrate)))
            clip.metadata["agent_editor"] = {"id": c.id, "reframe": c.reframe, "speed": c.speed, "notes": c.notes}
            if c.speed != 1.0:
                clip.effects.append(otio.schema.LinearTimeWarp(time_scalar=c.speed))
            if kind == "a" and (c.mute or not m.get("has_audio")):
                track.append(otio.schema.Gap(source_range=_range(0, dur, rate)))
            else:
                track.append(clip)
    ot.tracks.append(v1)

    def placed_track(name, kind, items):
        tr = otio.schema.Track(name=name, kind=kind)
        t = 0.0
        for start, dur, item in sorted(items, key=lambda x: x[0]):
            if start > t + 1e-3:
                tr.append(otio.schema.Gap(source_range=_range(0, start - t, rate)))
                t = start
            if start < t - 1e-3:  # overlap → would need another track; trim
                dur -= t - start
                if dur <= 0:
                    continue
            tr.append(item(dur))
            t += dur
        return tr

    ov = [o for o in tl.overlays if o.kind == "media"]
    if ov:
        def mk(o):
            def f(d):
                mrate = rate
                return otio.schema.Clip(name=o.label or o.media, media_reference=_ref(p, o.media, rate),
                                        source_range=otio.opentime.TimeRange(_rt(o.src_in, mrate), _rt(d * o.speed, mrate)))
            return f
        # greedy lane allocation: overlapping overlays go on separate tracks (V2, V3, ...) instead of being trimmed
        lanes: list[list] = []
        for o in sorted(ov, key=lambda o: o.start):
            for lane in lanes:
                if lane[-1].start + lane[-1].duration <= o.start + 1e-3:
                    lane.append(o); break
            else:
                lanes.append([o])
        for n, lane in enumerate(lanes):
            ot.tracks.append(placed_track(f"V{n + 2} Overlay", otio.schema.TrackKind.Video, [(o.start, o.duration, mk(o)) for o in lane]))
    ot.tracks.append(a1)
    for role in ("music", "vo", "sfx", "ambience", "dialogue"):
        items = [a for a in tl.audio if a.role == role]
        if not items:
            continue
        def mka(a):
            def f(d):
                return otio.schema.Clip(name=a.label or a.media, media_reference=_ref(p, a.media, rate),
                                        source_range=_range(a.src_in, d, rate))
            return f
        rows = []
        for a in items:
            src_left = p.media(a.media)["duration"] - a.src_in
            rows.append((a.start, min(a.duration or src_left, src_left, max(0.05, tl.duration - a.start)), mka(a)))
        ot.tracks.append(placed_track(f"A {role}", otio.schema.TrackKind.Audio, rows))
    # titles / text as markers so editors see them in the NLE
    for o in tl.overlays:
        if o.kind != "media":
            mk_ = otio.schema.Marker(name=f"{o.kind}: {o.text} {o.subtext}".strip(), marked_range=_range(o.start, o.duration, rate),
                                     color=otio.schema.MarkerColor.YELLOW)
            v1.markers.append(mk_)
    return ot


def _premiere_format(path: Path, tl: Timeline) -> None:
    """Write the sequence frame size into FCP7 XML so Premiere creates a WxH sequence (not its 1920x1080 default)."""
    import xml.etree.ElementTree as ET
    tree = ET.parse(path)
    r = seq_rate(tl.fps)
    tb, ntsc = (round(r * 1001 / 1000), "TRUE") if abs(r - round(r)) > 1e-3 else (int(round(r)), "FALSE")
    for video in tree.getroot().iter("video"):
        parent_seq = video  # only the sequence-level <video> (direct child of sequence/media)
        if video.find("track") is None:
            continue
        for old in video.findall("format"):
            video.remove(old)
        fmt = ET.Element("format")
        sc = ET.SubElement(fmt, "samplecharacteristics")
        rate = ET.SubElement(sc, "rate")
        ET.SubElement(rate, "timebase").text = str(tb)
        ET.SubElement(rate, "ntsc").text = ntsc
        ET.SubElement(sc, "width").text = str(tl.width)
        ET.SubElement(sc, "height").text = str(tl.height)
        ET.SubElement(sc, "anamorphic").text = "FALSE"
        ET.SubElement(sc, "pixelaspectratio").text = "square"
        ET.SubElement(sc, "fielddominance").text = "none"
        video.insert(0, fmt)
    tree.write(path, encoding="UTF-8", xml_declaration=True)


def export(p: Project, tl: Timeline, formats: list[str] | None = None,
           path_map: dict[str, str] | None = None) -> dict[str, str]:
    """formats: otio, fcpxml, premiere, edl. path_map rewrites media path prefixes, e.g.
    {"/home/user/agent-editor/projects/x/media": "/Users/me/Movies/x"} when footage lives elsewhere on the NLE machine."""
    formats = formats or ["premiere", "otio", "edl", "fcpxml"]
    _PATH_MAP.clear(); _PATH_MAP.update(path_map or {})
    ot = to_otio(p, tl)
    out = {}
    from .timeline import timeline_words
    words = timeline_words(p, tl)
    if words:  # caption sidecar Premiere imports as a caption track
        from .captions import srt
        out["captions_srt"] = str(srt(words, p.root / "exports" / f"{tl.name}_v{tl.version}.srt"))
    for f in formats:
        adapter, ext = FORMATS[f]
        dest = p.root / "exports" / f"{tl.name}_v{tl.version}{ext}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            if f == "edl":  # EDL supports one video track only
                edl_tl = otio.schema.Timeline(name=tl.name, global_start_time=ot.global_start_time)
                edl_tl.tracks.append(ot.video_tracks()[0].deepcopy())
                try:
                    otio.adapters.write_to_file(edl_tl, str(dest), adapter_name=adapter)
                except Exception:
                    # CMX needs source handles under every dissolve; fall back to cuts rather than fail
                    edl_tl = otio.schema.Timeline(name=tl.name, global_start_time=ot.global_start_time)
                    edl_tl.tracks.append(ot.video_tracks()[0].deepcopy())
                    trk = edl_tl.tracks[0]
                    for item in [x for x in trk if isinstance(x, otio.schema.Transition)]:
                        trk.remove(item)
                    otio.adapters.write_to_file(edl_tl, str(dest), adapter_name=adapter)
                    out[f + "_note"] = "dissolves without source handles exported as cuts"
            else:
                otio.adapters.write_to_file(ot, str(dest), adapter_name=adapter)
                if f == "premiere":
                    _premiere_format(dest, tl)
            out[f] = str(dest)
        except Exception as e:  # keep going; report per-format failures
            out[f] = f"ERROR: {e}"
    return out
