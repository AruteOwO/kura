"""
Trailers for Kura: AniList gives a YouTube (or Dailymotion) id. Two ways to play one:
  - "web": YouTube's embedded player in Kura's built-in browser view (no extra download);
  - "mpv": Kura's own video engine, with yt-dlp finding the stream (cleaner picture, no YouTube UI).
This module manages yt-dlp (download, update, version) and turns a trailer into stream
addresses, cached for a few hours so a second hover starts at once. No Qt in here.
"""

import json
import os
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request

import anime_organizer as org

TOOLS_DIR = org.SCRIPT_DIR / "tools"
YTDLP = TOOLS_DIR / "yt-dlp.exe"
YTDLP_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
CACHE_FILE = org.SCRIPT_DIR / "trailer_streams.json"
# 720p is plenty for a preview; AV1 decodes slowly on many PCs, so it's avoided
FORMAT = "bestvideo[height<=720][vcodec!*=av01]+bestaudio/best[height<=720]/best"
_lock = threading.Lock()
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def key(trailer):
    return f"{trailer.get('site')}:{trailer.get('id')}"


def watch_url(trailer):
    tid, site = trailer["id"], trailer.get("site")
    if site == "youtube":
        return f"https://www.youtube.com/watch?v={tid}"
    return f"https://www.dailymotion.com/video/{tid}"


def embed_url(trailer, muted=False, controls=False, loop=True):
    """YouTube's embedded player for previews: no controls, no related videos, loops."""
    tid, site = trailer["id"], trailer.get("site")
    if site == "youtube":
        q = {"autoplay": 1, "rel": 0, "modestbranding": 1, "iv_load_policy": 3, "playsinline": 1,
             "controls": int(controls), "mute": int(muted), "disablekb": int(not controls)}
        if loop:
            q.update(loop=1, playlist=tid)
        return f"https://www.youtube-nocookie.com/embed/{tid}?{urllib.parse.urlencode(q)}"
    return f"https://www.dailymotion.com/embed/video/{tid}?autoplay=1&mute={int(muted)}&controls={int(controls)}"


# ---------------------------------------------------------------- yt-dlp

def installed():
    return YTDLP.exists()


_VERSION = {}   # {(size, mtime): version}: asking yt-dlp takes ~1.2 s (Settings asked on every open)


def version():
    if not installed():
        return None
    try:
        st = YTDLP.stat()
        key = (st.st_size, st.st_mtime)
        if key in _VERSION:
            return _VERSION[key]
        r = subprocess.run([str(YTDLP), "--version"], capture_output=True, text=True, timeout=30,
                           creationflags=_NO_WINDOW)
        _VERSION.clear()
        _VERSION[key] = r.stdout.strip() or None
        return _VERSION[key]
    except (OSError, subprocess.SubprocessError):
        return None


def install():
    """Download the latest official yt-dlp.exe (from yt-dlp's GitHub releases). Returns its version."""
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = YTDLP.with_suffix(".download")
    req = urllib.request.Request(YTDLP_URL, headers={"User-Agent": org.USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as f:
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
    if tmp.stat().st_size < 1_000_000:
        tmp.unlink(missing_ok=True)
        raise OSError("the download looks broken (too small)")
    os.replace(tmp, YTDLP)
    return version()


def update():
    """yt-dlp updates itself in place. Returns (version, its message)."""
    r = subprocess.run([str(YTDLP), "-U"], capture_output=True, text=True, timeout=180, creationflags=_NO_WINDOW)
    return version(), (r.stdout or r.stderr).strip().splitlines()[-1:] or [""]


# ---------------------------------------------------------------- streams

def _load_cache():
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _expiry(url):
    m = re.search(r"[?&/]expire[=/](\d{9,})", url)
    return int(m.group(1)) - 600 if m else time.time() + 3 * 3600


def cached(trailer):
    """Stream addresses already looked up and still valid, or None."""
    with _lock:
        hit = _load_cache().get(key(trailer))
    if hit and hit.get("expires", 0) > time.time():
        return hit
    return None


def resolve(trailer):
    """{"video": url, "audio": url or None} for a trailer (yt-dlp, ~2-4 s; cached until it expires)."""
    hit = cached(trailer)
    if hit:
        return hit
    if not installed():
        raise OSError("yt-dlp isn't installed (Playback settings → Previews)")
    r = subprocess.run([str(YTDLP), "-f", FORMAT, "-g", "--no-playlist", "--no-warnings", watch_url(trailer)],
                       capture_output=True, text=True, timeout=60, creationflags=_NO_WINDOW)
    urls = [u for u in r.stdout.split() if u.startswith("http")]
    if not urls:
        err = (r.stderr or "").strip().splitlines()
        raise OSError(err[-1] if err else "yt-dlp found no video")
    out = {"video": urls[0], "audio": urls[1] if len(urls) > 1 else None,
           "expires": min(_expiry(u) for u in urls)}
    with _lock:
        cache = {k: v for k, v in _load_cache().items() if v.get("expires", 0) > time.time()}
        cache[key(trailer)] = out
        tmp = CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache), encoding="utf-8")
        os.replace(tmp, CACHE_FILE)
    return out
