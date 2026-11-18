"""ASS subtitle generation: social captions (word-pop / karaoke), film subtitles, titles, lower thirds."""
from __future__ import annotations

import re
from pathlib import Path

from .timeline import Captions, Overlay, Timeline


def _ass_time(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600); m = int(t % 3600 // 60); s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _color(hex_: str, alpha: int = 0) -> str:
    h = hex_.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", "\\N")


def group_words(words: list[dict], max_words: int, max_chars: int, max_gap: float = 0.6) -> list[list[dict]]:
    groups: list[list[dict]] = []
    cur: list[dict] = []
    for w in words:
        txt = " ".join(x["w"] for x in cur + [w])
        brk = bool(cur) and (len(cur) >= max_words or len(txt) > max_chars or w["start"] - cur[-1]["end"] > max_gap
                             or w["clip"] != cur[-1]["clip"])
        if brk:
            groups.append(cur); cur = []
        cur.append(w)
        if re.search(r"[.?!]$", w["w"]) and len(cur) >= 2:
            groups.append(cur); cur = []
    if cur:
        groups.append(cur)
    return groups


def build_ass(tl: Timeline, words: list[dict], out: Path) -> Path:
    W, H = tl.width, tl.height
    cap: Captions = tl.captions
    vertical = H > W
    base = min(W, H)
    size = cap.size or int(base * (0.088 if vertical else 0.052))
    if cap.style == "film":
        size = cap.size or int(H * 0.042)
    margin_v = {"bottom": int(H * 0.06), "lower_third": int(H * (0.26 if vertical else 0.12)), "center": 0, "top": int(H * 0.08)}[cap.position]
    align = {"bottom": 2, "lower_third": 2, "center": 5, "top": 8}[cap.position]
    font = cap.font
    bold_font = "Inter Display Black" if font == "Inter" and cap.style in ("bold_pop", "karaoke") else font
    outline = max(2, size // 8)
    styles = [
        f"Style: Cap,{bold_font},{size},{_color(cap.color)},{_color(cap.highlight)},{_color('#000000')},{_color('#000000', 0x60)},"
        f"{-1 if cap.style != 'film' else 0},0,0,0,100,100,0,0,1,{outline if cap.style != 'minimal' else 1},{outline // 2 if cap.style != 'film' else 1},{align},{int(W*0.08)},{int(W*0.08)},{margin_v},1",
        f"Style: Title,Inter Display Black,{int(base*0.09)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,0,{max(2, base//300)},5,60,60,0,1",
        f"Style: TitleSub,Inter,{int(base*0.04)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,0,0,0,0,100,100,2,0,1,0,2,5,60,60,0,1",
        f"Style: LowerName,Inter Display ExtraBold,{int(base*0.045)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,3,{int(base*0.012)},0,1,{int(W*0.06)},60,{int(H*0.16)},1",
        f"Style: LowerRole,Inter,{int(base*0.03)},&H00E6E6E6,&H00FFFFFF,&H00000000,&H96000000,0,0,0,0,100,100,0,0,3,{int(base*0.01)},0,1,{int(W*0.06)},60,{int(H*0.115)},1",
        f"Style: Text,Inter Display ExtraBold,{int(base*0.06)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,{max(2, base//360)},{max(1, base//540)},5,60,60,0,1",
    ]
    ev: list[str] = []

    def dlg(a: float, b: float, style: str, text: str, layer: int = 0):
        ev.append(f"Dialogue: {layer},{_ass_time(a)},{_ass_time(b)},{style},,0,0,0,,{text}")

    # ---------- captions
    if cap.enabled and words:
        hl = _color(cap.highlight).replace("&H00", "&H")
        groups = group_words(words, cap.max_words if cap.style != "film" else 12, cap.max_chars if cap.style != "film" else 84)
        for gi, g in enumerate(groups):
            toks = [(_esc(w["w"].upper() if cap.uppercase else w["w"])) for w in g]
            a, b = g[0]["start"], g[-1]["end"] + 0.12
            if gi + 1 < len(groups):  # never overlap the next caption (libass would stack them)
                b = min(b, groups[gi + 1][0]["start"] - 0.01)
            if cap.style in ("film", "clean", "minimal"):
                txt = " ".join(toks)
                if cap.style == "film" and len(txt) > 42:  # two balanced lines
                    mid = len(toks) // 2
                    txt = " ".join(toks[:mid]) + "\\N" + " ".join(toks[mid:])
                dlg(a, b, "Cap", txt)
            elif cap.style == "karaoke":
                parts = []
                for w, t in zip(g, toks):
                    parts.append(f"{{\\kf{max(1, int((w['end'] - w['start']) * 100))}}}{t} ")
                dlg(a, b, "Cap", "".join(parts).strip())
            else:  # bold_pop: one event per active word, active word highlighted & popped
                for k, w in enumerate(g):
                    ws = w["start"] if k else a
                    we = g[k + 1]["start"] if k + 1 < len(g) else b
                    if we <= ws:
                        continue
                    seg = []
                    for j, t in enumerate(toks):
                        if j == k:
                            seg.append(f"{{\\c{hl}&}}{t}{{\\r}}")
                        else:
                            seg.append(t)
                    pop = "{\\fad(40,0)}" if k == 0 else ""
                    dlg(ws, we, "Cap", pop + " ".join(seg))

    # ---------- text overlays (titles, lower thirds, free text)
    for o in tl.overlays:
        if o.kind == "media":
            continue
        a, b = o.start, o.start + o.duration
        fad = f"{{\\fad({int(o.fade_in*1000) or 250},{int(o.fade_out*1000) or 250})}}"
        st = o.style or {}
        pos = ""
        if "x" in st or "y" in st or o.kind == "text":
            pos = f"{{\\an5\\pos({int(st.get('x', o.x) * W)},{int(st.get('y', o.y) * H)})}}"
        col = f"{{\\c{_color(st['color']).replace('&H00', '&H')}&}}" if st.get("color") else ""
        sz = f"{{\\fs{int(st['size'])}}}" if st.get("size") else ""
        if o.kind == "title":
            yoff = int(H * 0.5)
            dlg(a, b, "Title", f"{fad}{{\\an5\\pos({W//2},{yoff - (int(base*0.03) if o.subtext else 0)})\\blur0.6}}{col}{sz}{_esc(o.text)}", 2)
            if o.subtext:
                dlg(a + 0.15, b, "TitleSub", f"{fad}{{\\an5\\pos({W//2},{yoff + int(base*0.06)})}}{_esc(o.subtext)}", 2)
        elif o.kind == "lower_third":
            slide = f"{{\\move({int(W*0.06) - 40},{H - int(H*0.16)},{int(W*0.06)},{H - int(H*0.16)},0,250)}}"
            dlg(a, b, "LowerName", f"{fad}{{\\an1}}{slide}{_esc(o.text)}", 2)
            if o.subtext:
                dlg(a + 0.1, b, "LowerRole", f"{fad}{{\\an1}}{_esc(o.subtext)}", 2)
        else:
            dlg(a, b, "Text", f"{fad}{pos}{col}{sz}{_esc(o.text)}", 2)

    out.write_text(
        "[Script Info]\nScriptType: v4.00+\nWrapStyle: 0\nScaledBorderAndShadow: yes\n"
        f"PlayResX: {W}\nPlayResY: {H}\nYCbCr Matrix: TV.709\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        + "\n".join(styles)
        + "\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        + "\n".join(ev) + "\n"
    )
    return out


def srt(words: list[dict], out: Path, max_words: int = 10, max_chars: int = 84) -> Path:
    """Sidecar SRT (for YouTube upload / accessibility)."""
    def ts(t):
        h = int(t // 3600); m = int(t % 3600 // 60); s = t % 60
        return f"{h:02d}:{m:02d}:{int(s):02d},{int((s % 1) * 1000):03d}"
    lines = []
    for i, g in enumerate(group_words(words, max_words, max_chars), 1):
        lines += [str(i), f"{ts(g[0]['start'])} --> {ts(g[-1]['end'] + 0.1)}", " ".join(w["w"] for w in g), ""]
    out.write_text("\n".join(lines))
    return out
