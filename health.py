"""
Library health check (no Qt in here): reads every episode with ffprobe (four at a time; results kept per file in
health_cache.json, so later checks only read new or changed files) and reports:
  broken      - unreadable, no video, or much shorter than the season's other episodes (incomplete download?)
  old_version - an older release next to its v2 (safe to remove)
  duplicate   - the same episode twice from different releases
  resolution  - a season mixing resolutions (e.g. three 720p episodes among 1080p ones)
  no_subs / no_english / picture_subs - subtitles missing, not English, or pictures only
  unmatched   - episodes Kura couldn't place in an AniList season
"""

import json
import os
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from statistics import median

import anime_organizer as org
import subtitles as subs

CACHE_FILE = org.SCRIPT_DIR / "health_cache.json"
SUB_EXTS = {".ass", ".ssa", ".srt", ".vtt", ".sup"}
ENGLISH = {"eng", "en", "english"}
SPECIAL_FLAGS = {"SP", "SPECIAL", "OVA", "OAD", "NCOP", "NCED", "OP", "ED", "PV", "CM", "MOVIE"}
SKIP_FOLDERS = {"extras", org.SPINOFF_FOLDER.lower(), org.SIDE_STORY_FOLDER.lower()}
# folders of openings/endings, promos and bonus clips: no subtitles expected, never "a season"
EXTRA_FOLDER = re.compile(r"^(extras?|bonus|op|ed|op ?& ?(ed|ep)|creditless|nc(op|ed)?|menus?|cm|pv|trailers?)\b", re.I)
SEASON_FOLDER = re.compile(r"^season (\d+)\b", re.I)
TIERS = (360, 480, 576, 720, 1080, 1440, 2160)
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_lock = threading.Lock()

KINDS = [  # (kind, title, what to do)
    ("broken", "Broken or incomplete files", "Can't be read, has no video, or is much shorter than the rest"),
    ("old_version", "Older versions next to a v2", "The group re-released these; the older file can go"),
    ("duplicate", "The same episode twice", "Two releases of one episode; keep the one you prefer"),
    ("unmatched", "Episodes not matched to a season", "Kura can't tell which AniList season these belong to"),
    ("resolution", "Mixed resolutions", "A season with some episodes in a lower resolution"),
    ("picture_subs", "Picture subtitles only", "Can be recoloured, or turned into text, in Subtitle colours"),
    ("no_english", "No English subtitles", "Subtitles, but none marked English"),
    ("no_subs", "No subtitles", "No subtitle track and no subtitle file next to it"),
]


def _load_cache():
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_cache(cache):
    tmp = CACHE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache), encoding="utf-8")
    os.replace(tmp, CACHE_FILE)


def probe(path):
    """{ok, error, duration, height, subs: [[codec, lang, title]]}"""
    try:
        r = subprocess.run([str(subs.FFPROBE), "-v", "error", "-show_entries",
                            "stream=codec_type,codec_name,height:stream_tags=language,title:format=duration",
                            "-of", "json", str(path)], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=60, creationflags=_NO_WINDOW)
        data = json.loads(r.stdout or "{}")
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        return {"ok": False, "error": str(e)[:200]}
    streams = data.get("streams") or []
    video = [s for s in streams if s.get("codec_type") == "video" and s.get("codec_name") not in ("mjpeg", "png")]
    try:
        duration = float((data.get("format") or {}).get("duration") or 0)
    except ValueError:
        duration = 0.0
    if r.returncode != 0 and not streams:
        return {"ok": False, "error": (r.stderr or "").strip().splitlines()[-1:][0][:200] if r.stderr else "unreadable"}
    return {"ok": bool(video), "error": "" if video else "no video track", "duration": duration,
            "height": (video[0].get("height") if video else None),
            "subs": [[s.get("codec_name"), (s.get("tags") or {}).get("language"), (s.get("tags") or {}).get("title")]
                     for s in streams if s.get("codec_type") == "subtitle"]}


def _version(name):
    m = re.search(r"(?:\d|\b)v(\d)\b", name, re.I)
    return int(m.group(1)) if m else 1


def _episode(name):
    title, season, flags, ep = org.parse(name)[:4]
    if {x.upper() for x in flags or ()} & SPECIAL_FLAGS:
        return None
    try:
        n = float(str(ep).split("-")[0]) if ep else None
    except ValueError:
        return None
    if n is None or n != int(n):   # "07.5" is a special, not episode 7
        return None
    return int(n)


def videos(folder):
    """Every episode file of a show, by season folder (Extras and spin-off links left out)."""
    out = {}
    folder = Path(folder)
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d.lower() not in SKIP_FOLDERS and not d.startswith(".")
                   and not org.is_link(Path(root) / d)]
        vids = [str(Path(root) / f) for f in files if Path(f).suffix.lower() in org.VIDEO_EXTS]
        if vids:
            out[str(Path(root))] = sorted(vids)
    return out


def scan(shows, progress=None, cancelled=None):
    """shows: [{"folder", "name", "mapped": set of files Kura placed in a season (None = unknown)}].
    Returns {"issues": [...], "checked": n files, "shows": n}."""
    cache = _load_cache()
    todo, every = [], []
    for show in shows:
        for season, files in videos(show["folder"]).items():
            for f in files:
                every.append(f)
                try:
                    st = os.stat(f)
                except OSError:
                    continue
                c = cache.get(f)
                if not c or c.get("size") != st.st_size or c.get("mtime") != int(st.st_mtime):
                    todo.append((f, st.st_size, int(st.st_mtime)))
    done = [0]

    def work(item):
        f, size, mtime = item
        if cancelled and cancelled():
            return
        info = probe(f)
        with _lock:
            cache[f] = {"size": size, "mtime": mtime, "info": info}
            done[0] += 1
            if progress:
                progress(done[0], len(todo))
            if done[0] % 100 == 0:
                _save_cache(cache)

    if todo and subs.ffmpeg_installed():
        with ThreadPoolExecutor(4) as ex:   # measured: 4 at once ≈ 0.1 s per file on the user's I: drive
            list(ex.map(work, todo))
        _save_cache(cache)
    if cancelled and cancelled():
        return None
    issues = []
    for show in shows:
        issues += check_show(show, cache)
    alive = set(every)   # forget files that are gone
    for f in [f for f in cache if f not in alive and f.startswith(tuple(s["folder"] for s in shows))]:
        cache.pop(f, None)
    _save_cache(cache)
    return {"issues": issues, "checked": len(every), "probed": len(todo), "shows": len(shows),
            "ffmpeg": subs.ffmpeg_installed()}


def check_show(show, cache):
    out = []
    name, mapped = show["name"], show.get("mapped")

    def issue(kind, files, detail):
        out.append({"kind": kind, "show": name, "folder": show["folder"], "files": files, "detail": detail})

    for season, files in videos(show["folder"]).items():
        is_root = Path(season) == Path(show["folder"])
        name_only = Path(season).name
        if EXTRA_FOLDER.match(name_only):
            continue
        # the path inside the show, so a folder one level too deep shows ("Season 1 / Season 1")
        label = "Show folder" if is_root else " / ".join(Path(season).relative_to(show["folder"]).parts)
        nested = not is_root and len(Path(season).relative_to(show["folder"]).parts) > 1
        sm = None if nested else SEASON_FOLDER.match(name_only)
        regular = is_root or (sm and int(sm.group(1)) > 0)   # a real season (not Season 0 / Specials / OVAs)
        infos = {f: (cache.get(f) or {}).get("info") for f in files}
        durations = [i["duration"] for i in infos.values() if i and i.get("ok") and i.get("duration")]
        mid = median(durations) if len(durations) >= 3 else None
        # broken / incomplete
        for f, info in infos.items():
            if info is None:
                continue
            if not info.get("ok"):
                issue("broken", [f], f"{label} · {info.get('error') or 'unreadable'}")
            elif regular and mid and info.get("duration") and info["duration"] < mid * 0.5 and _episode(Path(f).name):
                issue("broken", [f], f"{label} · {int(info['duration'] // 60)} min, the others are "
                                     f"{int(mid // 60)} min (incomplete download?)")
        # duplicates / older versions
        by_ep = {}
        for f in files:
            ep = _episode(Path(f).name)
            if ep is not None:
                by_ep.setdefault(ep, []).append(f)
        for ep, group in sorted(by_ep.items()):
            if len(group) < 2:
                continue
            best = max(_version(Path(f).name) for f in group)
            older = [f for f in group if _version(Path(f).name) < best]
            if older and best > 1:
                issue("old_version", older, f"{label} · episode {ep}: v{best} is here too")
            if len(group) - len(older) > 1:
                issue("duplicate", [f for f in group if f not in older], f"{label} · episode {ep}")
        # mixed resolutions
        heights = {}
        for f, info in infos.items():
            if info and info.get("ok") and info.get("height") and _episode(Path(f).name):
                tier = min(TIERS, key=lambda t: abs(t - info["height"]))   # 1040p (cropped) counts as 1080p
                heights.setdefault(tier, []).append(f)
        if len(heights) > 1:
            common = max(heights, key=lambda h: len(heights[h]))
            for h, fs in sorted(heights.items()):
                if h < common:
                    issue("resolution", fs, f"{label} · {len(fs)} in {h}p, {len(heights[common])} in {common}p")
        # subtitles
        for f, info in infos.items():
            if not info or not info.get("ok") or _episode(Path(f).name) is None:
                continue   # openings/endings, promos: no subtitles needed
            stem = Path(f).stem
            external = any(p.suffix.lower() in SUB_EXTS for p in Path(f).parent.glob(glob_escape(stem) + "*"))
            tracks = info.get("subs") or []
            if not tracks and not external:
                issue("no_subs", [f], label)
            elif tracks and all(t[0] in subs.IMAGE_SUBS for t in tracks) and not external:
                issue("picture_subs", [f], label)
            elif tracks and not external and not any((t[1] or "und").lower() in ENGLISH | {"und"} for t in tracks):
                issue("no_english", [f], f"{label} · marked " + ", ".join(sorted({t[1] or '?' for t in tracks})) +
                      " (sometimes just mislabelled)")
        # not matched to a season
        deep = nested and SEASON_FOLDER.match(name_only)   # "Season 1 / Season 1": one folder too deep
        if mapped is not None and (regular or deep):
            placed = {_episode(Path(f).name) for f in files if f in mapped}
            # (a second copy of a placed episode is reported as a duplicate / older version instead)
            loose = [f for f in files if f not in mapped and _episode(Path(f).name) is not None
                     and _episode(Path(f).name) not in placed]
            if loose:
                hint = (f" · one folder too deep: move them into {Path(season).parent.name}" if deep and
                        SEASON_FOLDER.match(Path(season).parent.name) else "")
                issue("unmatched", loose, f"{label} · {len(loose)} episode{'s' if len(loose) != 1 else ''}{hint}")
    return _group(out)


def glob_escape(text):
    return re.sub(r"([\[\]*?])", r"[\1]", text)


def _group(items):
    """One row per show and kind for the per-file kinds (a whole season without subtitles is one row)."""
    merged, out = {}, []
    for it in items:
        if it["kind"] in ("no_subs", "picture_subs", "no_english"):
            key = (it["kind"], it["folder"], it["detail"])
            if key in merged:
                merged[key]["files"] += it["files"]
                continue
            merged[key] = it
        out.append(it)
    for it in out:
        if it["kind"] in ("no_subs", "picture_subs", "no_english"):
            n = len(it["files"])
            it["detail"] = f"{it['detail']} · {n} episode{'s' if n != 1 else ''}"
    return out
