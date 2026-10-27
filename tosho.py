"""
Subtitles from Anime Tosho (no Qt in here). Anime Tosho keeps the subtitle tracks of nearly every release on Nyaa,
so for "[Group] Show - 05.mkv" it usually has that exact release's subtitles: same timing, same styling.

The original animetosho.org stopped serving files in October 2026 (its storage server shut down); the community
successor "Anime Tosho NEW" at animetosho.net has the same JSON API with a direct `url` per attachment.

`find(path)` = {"exact": [...], "other": [...]}:
  - exact: a release holding a file of the same size (or the CRC in your file's name): the very same release;
  - other: other releases of the same episode (AniDB episode id), best guesses first: their timing may differ.
"""

import json
import lzma
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

import anime_organizer as org

FEED = "https://feed.animetosho.net/json"
SITE = "https://animetosho.net"
UA = {"User-Agent": "Kura/1.0 (personal anime library app)"}
CRC = re.compile(r"\[([0-9A-Fa-f]{8})\]")
WEB = re.compile(r"subsplease|erai|horriblesubs|\bcr\b|crunchyroll|web[- .]?(dl|rip)?|amzn|\bnf\b|netflix|hidive|"
                 r"toonshub|varyg|tsundere", re.I)
BD = re.compile(r"\bbd\b|blu-?ray|bdrip|remux", re.I)

_lock = threading.Lock()
_last = [0.0]
_releases = {}


def _get(params, timeout=20):
    """One API call, at most ~3 a second (a polite pace for a volunteer-run site)."""
    with _lock:
        wait = 0.35 - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
    url = FEED + "?" + urllib.parse.urlencode(params)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except (OSError, ValueError):
            if attempt == 2:
                raise
            time.sleep(1.5 * (attempt + 1))


def search(query):
    return _get({"q": query}) or []


def by_episode(eid):
    return _get({"eid": eid}) or []


def release(tid):
    if tid not in _releases:
        _releases[tid] = _get({"show": "torrent", "id": tid}) or {}
    return _releases[tid]


def subtitle_tracks(rel, file_pick=None):
    """The subtitle tracks of a release's file(s): [{url, lang, name, format, default, forced, file, size, crc}].
    file_pick(file dict) -> bool chooses the file(s) in a batch."""
    out = []
    for f in rel.get("files") or []:
        if file_pick and not file_pick(f):
            continue
        for a in f.get("attachments") or []:
            if a.get("type") != "subtitle" or not a.get("url"):
                continue
            info = a.get("info") or {}
            out.append({"url": a["url"], "lang": (info.get("language_code") or info.get("lang") or "und").lower(),
                        "name": info.get("name") or "", "format": (info.get("format") or info.get("codec") or "ass").lower(),
                        "default": bool(info.get("default")), "forced": bool(info.get("forced")),
                        "file": f.get("filename") or f.get("name") or "", "size": f.get("size"),
                        "crc": (f.get("crc32") or "").lower()})
    return out


def download(url):
    """A subtitle's text (Anime Tosho stores them xz-compressed)."""
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
        data = r.read()
    try:
        data = lzma.decompress(data)
    except lzma.LZMAError:
        pass   # already plain
    for enc in ("utf-8-sig", "utf-16"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("cp1252", errors="replace")


def ext_for(track):
    f = track["format"]
    return ".srt" if f in ("srt", "subrip") else ".vtt" if f in ("vtt", "webvtt") else ".ass"


def _episode(name):
    p = org.anitopy_parse(name) or {}
    ep = p.get("episode_number")
    if isinstance(ep, list):
        ep = ep[0]
    try:
        ep = float(ep) if ep is not None else None
    except (TypeError, ValueError):
        ep = None
    return p, ep


def _group(name):
    m = re.match(r"\s*\[([^\]]+)\]", name or "")
    return m.group(1) if m else ""


def _norm(t):
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def _season(parsed, folder=None):
    s = parsed.get("anime_season")
    if isinstance(s, list):
        s = s[0]
    try:
        return int(s)
    except (TypeError, ValueError):
        m = re.match(r"season\s*(\d+)", (folder or "").strip(), re.I)
        return int(m.group(1)) if m else 1


def find(path, progress=None, others=8):
    """Subtitle sources for one local episode. progress(text) says what it's doing."""
    path = Path(path)
    say = progress or (lambda _t: None)
    size = os.path.getsize(path) if path.exists() else None
    crc = (CRC.findall(path.name) or [""])[-1].lower()
    local, ep = _episode(path.name)
    title, season = _norm(local.get("anime_title")), _season(local, path.parent.name)
    group = (local.get("release_group") or _group(path.name)).lower()

    def same_file(f):
        return (size and f.get("size") == size) or (crc and (f.get("crc32") or "").lower() == crc)

    def mine(f):   # in a batch: the file of this episode
        _p, e = _episode(f.get("filename") or "")
        return same_file(f) or (ep is not None and e == ep)

    def same_show(name):   # same title and season (season 2 of a show is another AniDB entry)
        p, _e = _episode(name or "")
        return bool(title) and _norm(p.get("anime_title")) == title and _season(p) == season

    say("Searching Anime Tosho…")
    query = re.sub(r"\s+", " ", CRC.sub("", path.stem)).strip()
    results = search(query)
    exact, eid, aids = [], None, {}
    for r in results[:8]:
        if same_show(r.get("title")):
            if r.get("anidb_aid"):
                aids[r["anidb_aid"]] = aids.get(r["anidb_aid"], 0) + 1
            if not eid and r.get("anidb_eid") and _episode(r.get("title") or "")[1] == ep:
                eid = r["anidb_eid"]
        if size and r.get("total_size") and r["total_size"] < size:
            continue   # can't hold a file this big
        rel = release(r["id"])
        if any(same_file(f) for f in rel.get("files") or []):
            tracks = subtitle_tracks(rel, same_file)
            if tracks:
                exact.append({"id": r["id"], "title": r.get("title") or "", "same": True, "tracks": tracks,
                              "group": _group(r.get("title") or ""), "link": r.get("link") or ""})
    other = []
    if not exact and title and ep is not None:
        say("Looking at other releases of this episode…")
        if not aids:   # the show by its title in quotes (a loose search matches half the site)
            for r in search(f'"{local.get("anime_title")}"'):
                if r.get("anidb_aid") and same_show(r.get("title")):
                    aids[r["anidb_aid"]] = aids.get(r["anidb_aid"], 0) + 1
        aid = max(aids, key=aids.get) if aids else None
        rows = []
        if aid:
            rows = _get({"aid": aid, "q": f"{int(ep):02d}"}) or []
            rows += [r for r in (_get({"aid": aid}) or []) if r["id"] not in {x["id"] for x in rows}]
            if not eid:
                eid = next((r["anidb_eid"] for r in rows if r.get("anidb_eid")
                            and _episode(r.get("title") or "")[1] == ep), None)
        cands = by_episode(eid) if eid else []
        seen = {r["id"] for r in cands}
        cands += [r for r in rows if r["id"] not in seen and (r.get("num_files") or 1) > 1]   # batches
        web_local = bool(WEB.search(path.name)) or not BD.search(path.name)

        def rank(r):
            t = r.get("title") or ""
            g = _group(t).lower()
            return (0 if g and g == group else 1,
                    0 if bool(WEB.search(t)) == web_local and not (web_local and BD.search(t)) else 1,
                    (r.get("num_files") or 1) > 1,
                    -(r.get("seeders") or 0))
        for r in sorted(cands, key=rank):
            if len(other) >= others:
                break
            rel = release(r["id"])
            tracks = subtitle_tracks(rel, mine)
            if tracks:
                other.append({"id": r["id"], "title": r.get("title") or "", "same": False, "tracks": tracks,
                              "group": _group(r.get("title") or ""), "link": r.get("link") or "",
                              "same_group": bool(group) and _group(r.get("title") or "").lower() == group,
                              "batch": (r.get("num_files") or 1) > 1})
    return {"exact": exact, "other": other, "episode": ep, "eid": eid}


def best_track(cands, langs=("eng", "en")):
    """The track to suggest: English, the release's default first, full subtitles over signs-only."""
    best = None
    for ci, c in enumerate(cands):
        for ti, t in enumerate(c["tracks"]):
            if t["lang"] not in langs and not (t["lang"] == "und" and not best):
                continue
            signs = bool(re.search(r"sign|song|forced", t["name"], re.I)) or t["forced"]
            key = (ci, signs, not t["default"], ti)
            if best is None or key < best[0]:
                best = (key, ci, ti)
    return (best[1], best[2]) if best else None
