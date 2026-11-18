"""Fast engine tests on synthetic media (no network, no Whisper)."""
import subprocess

import pytest

from agent_editor import captions, export, render, review, timeline as T
from agent_editor.core import Project, probe


def _mk(path, color, freq, dur=6, size="1280x720"):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc2=s={size}:r=25:d={dur}",
                    "-f", "lavfi", "-i", f"sine=f={freq}:d={dur}", "-vf", f"drawbox=c={color}@0.5:t=fill",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(path)], check=True)


@pytest.fixture(scope="module")
def proj(tmp_path_factory):
    d = tmp_path_factory.mktemp("media")
    _mk(d / "a.mp4", "red", 440)
    _mk(d / "b.mp4", "blue", 660)
    p = Project.open(tmp_path_factory.mktemp("proj"))
    p.ingest(str(d / "a.mp4"), "a")
    p.ingest(str(d / "b.mp4"), "b")
    # fake transcript so captions / timeline_words work without Whisper
    words = [{"i": i, "w": w, "start": 0.5 + i * 0.4, "end": 0.8 + i * 0.4, "p": 1, "seg": 0}
             for i, w in enumerate("this is a test of the caption system right here".split())]
    p.cache_put("a", "transcript", {"language": "en", "segments": [{"id": 0, "start": 0.5, "end": 4.5, "text": "", "words": list(range(len(words)))}],
                                    "words": words, "text": " ".join(w["w"] for w in words)})
    return p


def test_layout_with_transition(proj):
    tl = T.new(proj, "t1", "youtube")
    T.add_clip(proj, tl, "a", 0, 4)
    T.add_clip(proj, tl, "b", 1, 5, transition="dissolve", transition_duration=1.0)
    lay = tl.layout()
    assert lay[1]["start"] == pytest.approx(3.0)
    assert tl.duration == pytest.approx(7.0)


def test_render_cuts_and_dissolve(proj):
    tl = T.new(proj, "t2", "youtube")
    T.add_clip(proj, tl, "a", 0, 3)
    T.add_clip(proj, tl, "b", 0, 3, transition="dissolve", transition_duration=0.5)
    T.add_clip(proj, tl, "a", 3, 5)
    tl.captions.enabled = True
    T.add_overlay(proj, tl, kind="title", text="Hello", start=0, duration=2)
    T.add_overlay(proj, tl, kind="media", media="b", src_in=4, start=1, duration=1.5, full_frame=False, scale=0.3)
    T.add_audio(proj, tl, media="b", start=0, gain_db=-12)
    T.save(proj, tl)
    r = render.render(proj, tl, "preview")
    info = probe(r["path"])
    assert info["duration"] == pytest.approx(tl.duration, abs=0.15)
    assert abs(r["loudness"]["after_lufs"] - tl.loudness_target) < 1.5
    assert (info["width"], info["height"]) == (960, 540)


def test_vertical_reframe_modes(proj):
    tl = T.new(proj, "t3", "vertical")
    for mode in ("fill", "fit", "blur_fill"):
        T.add_clip(proj, tl, "a", 0, 1, reframe=mode)
    r = render.render(proj, tl, "preview")
    info = probe(r["path"])
    assert info["height"] > info["width"]


def test_tighten_and_captions(proj, tmp_path):
    tl = T.new(proj, "t4", "vertical")
    c = T.add_clip(proj, tl, "a", 0, 5)
    ids = T.tighten(proj, tl, c.id, max_gap=0.05)
    assert len(ids) >= 2
    words = T.timeline_words(proj, tl)
    ass = captions.build_ass(tl, words, tmp_path / "c.ass")
    assert "Dialogue" in ass.read_text()


def test_split_edit_freeze_and_undo(proj):
    tl = T.new(proj, "t5", "youtube")
    a = T.add_clip(proj, tl, "a", 0, 2)
    T.add_clip(proj, tl, "b", 0, 2)
    T.l_cut(proj, tl, a.id, 0.5)
    T.freeze_patch(proj, tl, a.id, 0.3)
    T.save(proj, tl, "edits")
    assert len(tl.overlays) == 2
    render.render(proj, tl, "preview")
    T.remove(tl, tl.overlays[0].id)
    T.save(proj, tl, "remove")
    back = T.undo(proj, "t5")
    assert len(back.overlays) == 2


def test_beat_montage_and_fit(proj):
    proj.cache_put("b", "audio", {"beats": [i * 0.5 for i in range(12)], "downbeats": [0, 2, 4], "tempo_bpm": 120})
    tl = T.new(proj, "t6", "square")
    T.beat_montage(proj, tl, [{"media": "a"}, {"media": "b"}, {"media": "a", "start": 2, "end": 5}], "b", [2, 2, 4])
    assert tl.duration == pytest.approx(4.0, abs=0.05)
    T.fit_to_duration(tl, 3.0)
    assert tl.duration == pytest.approx(3.0, abs=0.1)


def test_export_and_review(proj):
    tl = T.load(proj, "t2")
    out = export.export(proj, tl, ["otio", "premiere", "edl"])
    assert all(not v.startswith("ERROR") for k, v in out.items() if not k.endswith("_note")), out
    r = render.render(proj, tl, "preview")
    q = review.qc(r["path"], tl.duration, tl.loudness_target)
    assert q["resolution"] == "960x540"
    assert review.sheet(proj, tl, r["path"]).exists()
