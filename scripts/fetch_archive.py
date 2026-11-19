#!/usr/bin/env python3
"""Search open video archives for each beat of a storyboard and download usable clips into a folder.

Pixabay is searched too when PIXABAY_API_KEY is set (export it in your shell; never commit it).

Sources (licence-filtered — only material that allows commercial reuse with attribution, or public domain):
  * Wikimedia Commons video (CC0, PD, CC BY, CC BY-SA, FAL)
  * Internet Archive "movies" with a Creative Commons / public-domain licence URL

It never touches rights-pending sources (Getty, AP, YouTube uploads); those stay in the storyboard as links to license.

Usage:
  python3 scripts/fetch_archive.py plans/tourism_archive.json ~/Desktop/"Editor Agent"/QIA_Tourism_Archive
  python3 scripts/fetch_archive.py PLAN OUTDIR --max-per-beat 3 --max-mb 400 --dry-run

Output:  OUTDIR/<beat>/<clip files>   +   OUTDIR/credits.csv   +   OUTDIR/report.md
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

UA = {"User-Agent": "AgentEditorArchive/0.2 (https://github.com/keyframprod-ui/agent-editor; low-volume CC media research)"}
OK_LICENSES = re.compile(r"(pixabay content license|pexels license|cc0|public domain|pdm|cc by(-sa)?( \d\.\d)?\b|free art|fal\b|creativecommons\.org/(licenses/(by|by-sa)/|publicdomain))", re.I)
BAD_LICENSES = re.compile(r"(-nc|-nd|noncommercial|noderivs|non-commercial)", re.I)


def get_json(url: str, tries: int = 4, wait: float = 10.0):
    for a in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
                return json.load(r)
        except Exception as e:  # 429 / transient: back off politely
            print(f"   … retry {a + 1}/{tries}: {e}", file=sys.stderr)
            time.sleep(wait * (a + 1))
    return {}


def strip(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def commons_search(query: str, limit: int = 12) -> list[dict]:
    url = "https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode({
        "action": "query", "generator": "search", "gsrsearch": f"{query} filetype:video", "gsrnamespace": 6,
        "gsrlimit": limit, "prop": "videoinfo", "viprop": "url|size|extmetadata|derivatives", "format": "json"})
    d = get_json(url)
    out = []
    for p in (d.get("query", {}).get("pages", {}) or {}).values():
        vi = (p.get("videoinfo") or [{}])[0]
        em = vi.get("extmetadata", {})
        lic = strip(em.get("LicenseShortName", {}).get("value", ""))
        ders = {x.get("transcodekey", ""): x["src"] for x in vi.get("derivatives", [])}
        best = next((ders[k] for k in ("1080p.vp9.webm", "1080p.webm", "720p.vp9.webm", "720p.webm") if k in ders), vi.get("url"))
        out.append({
            "source": "Wikimedia Commons", "title": p["title"].removeprefix("File:"), "license": lic,
            "creator": strip(em.get("Artist", {}).get("value", ""))[:120], "date": strip(em.get("DateTimeOriginal", {}).get("value", ""))[:40],
            "duration": vi.get("duration") or 0, "width": vi.get("width") or 0, "height": vi.get("height") or 0,
            "page": vi.get("descriptionurl"), "download": best, "size": vi.get("size") or 0,
            "description": strip(em.get("ImageDescription", {}).get("value", ""))[:300],
        })
    return out


def commons_files(titles: list[str]) -> list[dict]:
    """Exact Commons files named in the plan (clips already vetted by archive research)."""
    if not titles:
        return []
    url = "https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode({
        "action": "query", "titles": "|".join("File:" + t.removeprefix("File:") for t in titles),
        "prop": "videoinfo", "viprop": "url|size|extmetadata|derivatives", "format": "json"})
    d = get_json(url)
    out = []
    for p in (d.get("query", {}).get("pages", {}) or {}).values():
        vi = (p.get("videoinfo") or [{}])[0]
        if not vi:
            print(f"   ! not found on Commons: {p.get('title')}")
            continue
        em = vi.get("extmetadata", {})
        ders = {x.get("transcodekey", ""): x["src"] for x in vi.get("derivatives", [])}
        best = next((ders[k] for k in ("1080p.vp9.webm", "1080p.webm", "720p.vp9.webm", "720p.webm") if k in ders), vi.get("url"))
        out.append({"source": "Wikimedia Commons", "title": p["title"].removeprefix("File:"),
                    "license": strip(em.get("LicenseShortName", {}).get("value", "")),
                    "creator": strip(em.get("Artist", {}).get("value", ""))[:120],
                    "date": strip(em.get("DateTimeOriginal", {}).get("value", ""))[:40],
                    "duration": vi.get("duration") or 0, "width": vi.get("width") or 0, "height": vi.get("height") or 0,
                    "page": vi.get("descriptionurl"), "download": best, "size": vi.get("size") or 0, "description": "", "vetted": True})
    return out


def ia_search(query: str, limit: int = 10) -> list[dict]:
    q = f'({query}) AND mediatype:movies AND (licenseurl:*creativecommons.org/licenses/by* OR licenseurl:*publicdomain*)'
    url = "https://archive.org/advancedsearch.php?" + urllib.parse.urlencode(
        {"q": q, "fl[]": ["identifier", "title", "licenseurl", "creator", "date"], "rows": limit, "output": "json"}, doseq=True)
    d = get_json(url)
    out = []
    for doc in d.get("response", {}).get("docs", []):
        ident = doc["identifier"]
        meta = get_json(f"https://archive.org/metadata/{ident}", tries=2)
        files = [f for f in meta.get("files", []) if f.get("name", "").lower().endswith((".mp4", ".mov", ".webm", ".mkv"))]
        if not files:
            continue
        f = max(files, key=lambda f: int(f.get("size", 0) or 0) if int(f.get("size", 0) or 0) < 800e6 else 0)
        out.append({
            "source": "Internet Archive", "title": doc.get("title", ident), "license": doc.get("licenseurl", ""),
            "creator": ", ".join(doc["creator"]) if isinstance(doc.get("creator"), list) else doc.get("creator", ""),
            "date": str(doc.get("date", ""))[:10], "duration": float(f.get("length", 0) or 0), "width": int(f.get("width", 0) or 0),
            "height": int(f.get("height", 0) or 0), "page": f"https://archive.org/details/{ident}",
            "download": f"https://archive.org/download/{ident}/{urllib.parse.quote(f['name'])}", "size": int(f.get("size", 0) or 0),
            "description": "",
        })
        time.sleep(1)
    return out


def pixabay_search(query: str, limit: int = 10) -> list[dict]:
    """Pixabay videos (Pixabay Content License: free commercial use, no attribution required). Needs PIXABAY_API_KEY."""
    import os
    key = os.environ.get("PIXABAY_API_KEY")
    if not key:
        return []
    url = "https://pixabay.com/api/videos/?" + urllib.parse.urlencode({"key": key, "q": query[:100], "per_page": max(3, limit), "safesearch": "true"})
    d = get_json(url, tries=2, wait=5)
    out = []
    for h in d.get("hits", []):
        v = h.get("videos", {})
        best = next((v[k] for k in ("large", "medium", "small") if v.get(k, {}).get("url")), None)
        if not best:
            continue
        out.append({"source": "Pixabay", "title": f"pixabay_{h['id']}_{(h.get('tags') or '').replace(', ', '_')[:50]}",
                    "license": "Pixabay Content License", "creator": h.get("user", ""), "date": "",
                    "duration": h.get("duration") or 0, "width": best.get("width") or 0, "height": best.get("height") or 0,
                    "page": h.get("pageURL"), "download": best["url"], "size": best.get("size") or 0,
                    "description": h.get("tags", ""), "ext": ".mp4"})
    return out


def usable(c: dict, beat: dict) -> tuple[bool, str]:
    lic = c["license"] or ""
    if not OK_LICENSES.search(lic) or BAD_LICENSES.search(lic):
        return False, f"licence not usable ({lic or 'unknown'})"
    if c["duration"] and c["duration"] < 2:
        return False, "too short"
    blob = f"{c['title']} {c['description']}".lower()
    if beat.get("must") and not any(m.lower() in blob for m in beat["must"]):
        return False, "not about the subject"
    if any(x.lower() in blob for x in beat.get("exclude", [])):
        return False, "excluded subject"
    return True, ""


def download(url: str, dest: Path, max_bytes: float) -> bool:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as r:
            size = int(r.headers.get("Content-Length") or 0)
            if size and size > max_bytes:
                print(f"   skip (too large: {size / 1e6:.0f} MB)")
                return False
            with open(dest, "wb") as f:
                while chunk := r.read(1 << 20):
                    f.write(chunk)
        return True
    except Exception as e:
        print(f"   download failed: {e}")
        dest.unlink(missing_ok=True)
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("plan"); ap.add_argument("outdir")
    ap.add_argument("--max-per-beat", type=int, default=3)
    ap.add_argument("--max-mb", type=float, default=400)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--sources", default="commons,ia,pixabay")
    a = ap.parse_args()
    plan = json.load(open(a.plan))
    out = Path(a.outdir).expanduser(); out.mkdir(parents=True, exist_ok=True)
    rows, report = [], [f"# Archive search · {plan.get('title', '')}", ""]
    seen: set[str] = set()
    for beat in plan["beats"]:
        print(f"\n== {beat['id']} {beat['label']}")
        report.append(f"## {beat['id']} · {beat['label']} ({beat.get('time', '')})")
        cands = commons_files(beat.get("files", []))
        for q in beat["queries"]:
            if "commons" in a.sources:
                cands += commons_search(q); time.sleep(3)
            if "ia" in a.sources:
                cands += ia_search(q); time.sleep(2)
            if "pixabay" in a.sources:
                cands += pixabay_search(q); time.sleep(1)
        picked = 0
        for c in cands:
            if c["download"] in seen or picked >= a.max_per_beat:
                continue
            ok, why = (True, "") if c.get("vetted") and OK_LICENSES.search(c["license"] or "") else usable(c, beat)
            if not ok:
                continue
            seen.add(c["download"])
            ext = c.get("ext") or Path(urllib.parse.urlparse(c["download"]).path).suffix or ".webm"
            name = re.sub(r"[^\w\-]+", "_", Path(c["title"]).stem)[:70] + ext
            dest = out / beat["id"] / name
            dest.parent.mkdir(exist_ok=True)
            sa = " ⚠ share-alike: legal sign-off" if re.search(r"(by-sa|free art|fal\b)", c["license"] or "", re.I) else ""
            print(f"   + {c['title'][:70]}  [{c['license']}]  {c['width']}x{c['height']} {c['duration']:.0f}s{sa}")
            if not a.dry_run and not dest.exists():
                if not download(c["download"], dest, a.max_mb * 1e6):
                    continue
                time.sleep(4)
            picked += 1
            rows.append({"beat": beat["id"], "file": str(dest.relative_to(out)), **{k: c[k] for k in ("title", "creator", "license", "date", "source", "page", "duration", "width", "height")}})
            report.append(f"- `{dest.relative_to(out)}` — {c['title']} · {c['creator'] or 'unknown'} · {c['license']} · {c['date']} · {c['page']}")
        if picked == 0:
            report.append(f"- **Nothing usable found.** License from the storyboard links: {beat.get('fallback', '—')}")
        report.append("")
    with open(out / "credits.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["beat"]); w.writeheader(); w.writerows(rows)
    report += ["", "Check every clip's date against the beat: never use later material as earlier archive.",
               "CC BY / BY-SA / FAL require on-screen or end-credit attribution; BY-SA/FAL are share-alike."]
    (out / "report.md").write_text("\n".join(report))
    print(f"\nDone: {len(rows)} clips → {out}\nSee report.md and credits.csv")


if __name__ == "__main__":
    main()
