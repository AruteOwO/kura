"""
Filler and recap episodes (no Qt in here). Sources, in order:
  - animefillerlist.com: the usual filler guide (canon / mixed / filler per episode), matched by title. Its
    pages often number a whole franchise as one run while AniList splits seasons, so a page is only used
    when its episode count fits the AniList entry (One Piece, Naruto, Bleach...: the shows with filler);
  - Jikan (MyAnimeList's public API, per MAL id): filler / recap flags. It was unreachable from the user's PC
    in Oct 2026, so it's the second choice.
Kept in filler_cache.json per AniList id, refetched after a week (long shows get new episodes).
`kind(mid, episode)` only reads the cache, so the player can ask any time.
"""

import html
import json
import os
import re
import threading
import time
import urllib.request

import anime_organizer as org

CACHE_FILE = org.SCRIPT_DIR / "filler_cache.json"
AFL = "https://www.animefillerlist.com"
JIKAN = "https://api.jikan.moe/v4/anime/{mal}/episodes?page={page}"
REFRESH = 7 * 86400
_lock = threading.Lock()
_cache = None
_busy = set()
_jikan_down = 0   # Jikan didn't answer: don't wait on it again for a while


def _load():
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _cache = {}
    return _cache


def _save():
    tmp = CACHE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(_cache), encoding="utf-8")
    os.replace(tmp, CACHE_FILE)


def kind(mid, episode):
    """"filler", "mixed", "recap" or None (also None while it isn't known yet)."""
    with _lock:
        entry = _load().get(str(mid)) or {}
    return (entry.get("eps") or {}).get(str(episode))


def episodes(mid):
    """{episode number: kind} known for a show."""
    with _lock:
        return {int(k): v for k, v in ((_load().get(str(mid)) or {}).get("eps") or {}).items()}


def fresh(mid):
    with _lock:
        entry = _load().get(str(mid))
    return bool(entry) and time.time() - entry.get("t", 0) < REFRESH


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": org.USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def _norm(text):
    return re.sub(r"[^a-z0-9]+", " ", html.unescape(text or "").lower()).strip()


def afl_shows():
    """{normalized name: slug} from animefillerlist's show list (kept 30 days). "English (Romaji)" names
    count under both."""
    with _lock:
        saved = _load().get("_afl_shows")
    if saved and time.time() - saved.get("t", 0) < 30 * 86400:
        return saved["shows"]
    page = _get(f"{AFL}/shows")
    shows, aside = {}, {}
    for slug, name in re.findall(r'<a href="/shows/([a-z0-9-]+)">([^<]+)</a>', page):
        name = html.unescape(name)
        m = re.match(r"^(.*?)\s*\((.+)\)\s*$", name)
        shows.setdefault(_norm(m.group(1) if m else name), slug)
        if m:   # the name in brackets only counts when no show has it as its own name
            aside.setdefault(_norm(m.group(2)), slug)   # ("One Pace (One Piece)" isn't One Piece)
    for key, slug in aside.items():
        shows.setdefault(key, slug)
    with _lock:
        _load()["_afl_shows"] = {"t": time.time(), "shows": shows}
        _save()
    return shows


def afl_episodes(slug):
    """{episode: "filler"/"mixed"} from a show's page (canon episodes are left out)."""
    page = _get(f"{AFL}/shows/{slug}")
    out, count = {}, 0
    for cls, num in re.findall(r'<tr class="([^"]*)"[^>]*>\s*<td class="Number">(\d+)</td>', page):
        count += 1
        first = cls.split()[0]
        if first == "filler":
            out[num] = "filler"
        elif first.startswith("mixed"):
            out[num] = "mixed"
    return out, count


def jikan_episodes(mal):
    eps, page = {}, 1
    while page <= 40:
        data = json.loads(_get(JIKAN.format(mal=mal, page=page), timeout=8))
        for e in data.get("data") or []:
            if e.get("filler"):
                eps[str(e["mal_id"])] = "filler"
            elif e.get("recap"):
                eps[str(e["mal_id"])] = "recap"
        if not (data.get("pagination") or {}).get("has_next_page"):
            break
        page += 1
        time.sleep(0.5)   # Jikan allows ~3 requests a second
    return eps


def fetch(mid, info):
    """info = {"titles": [...], "episodes": n or None, "mal": id or None}. Returns the episodes marked."""
    eps, source = {}, None
    try:
        shows = afl_shows()
        slug = next((shows[_norm(t)] for t in info.get("titles") or [] if _norm(t) in shows), None)
        if slug:
            found, count = afl_episodes(slug)
            total = info.get("episodes")
            # the page fits this AniList entry (not a franchise numbered as one run)
            if count and (not total or count <= total + 3):
                eps, source = found, f"animefillerlist:{slug}"
    except OSError:
        pass
    global _jikan_down
    if source is None and info.get("mal") and time.time() > _jikan_down:
        try:
            eps, source = jikan_episodes(info["mal"]), "jikan"
        except (OSError, ValueError):
            _jikan_down = time.time() + 1800
    with _lock:
        _load()[str(mid)] = {"t": time.time(), "source": source, "eps": eps}
        _save()
    return eps


def prefetch(mids, info_lookup, done=None):
    """Fetch in the background the shows not known yet (or older than a week). info_lookup(mid) -> info;
    done(mid) after each."""
    todo = [m for m in dict.fromkeys(mids) if m and not fresh(m) and m not in _busy]
    if not todo:
        return
    _busy.update(todo)

    def run():
        for mid in todo:
            try:
                fetch(mid, info_lookup(mid))
                if done:
                    done(mid)
            except Exception:
                pass
            finally:
                _busy.discard(mid)
    threading.Thread(target=run, daemon=True).start()
