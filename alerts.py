"""
Sequel alerts (no Qt in here): AniList's relations for the shows you completed or are watching, 50 shows per
request, looking for SEQUEL entries that aren't on your list. Checked at most once a day; the first check only
remembers what's there (no flood of old sequels), later ones report what's new. Kept in alerts_cache.json.
"""

import json
import os
import time

import anime_organizer as org

CACHE_FILE = org.SCRIPT_DIR / "alerts_cache.json"
EVERY = 20 * 3600
NODE = ("id type format status isAdult season seasonYear startDate { year month day } episodes title { romaji english } "
        "coverImage { large color } nextAiringEpisode { episode airingAt } siteUrl")
QUERY = ("query ($ids: [Int], $page: Int) { Page(page: $page, perPage: 50) { media(id_in: $ids) { id "
         "relations { edges { relationType node { " + NODE + " } } } } } }")


def load():
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(data):
    tmp = CACHE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, CACHE_FILE)


def due():
    return time.time() - (load().get("t") or 0) > EVERY


def check(entries, progress=None):
    """entries = your AniList list {media id: entry}. Returns (sequels, new, started): sequels = [{"from": id,
    "from_title": str, "media": node}] for sequels not on your list; new = the ones not seen before; started =
    announced ones that began airing since the last check."""
    sources = [mid for mid, e in entries.items() if e.get("status") in ("COMPLETED", "CURRENT", "REPEATING")]
    found, seen_ids = [], set()
    for start in range(0, len(sources), 50):
        chunk = sources[start:start + 50]
        data = org.anilist(QUERY, {"ids": chunk, "page": 1}) or {}
        for m in ((data.get("Page") or {}).get("media") or []):
            src = entries.get(m["id"]) or {}
            src_title = ((src.get("media") or {}).get("title") or {})
            for e in ((m.get("relations") or {}).get("edges") or []):
                node = e.get("node") or {}
                if e.get("relationType") != "SEQUEL" or node.get("type") != "ANIME" or not node.get("id"):
                    continue
                if node["id"] in entries or node["id"] in seen_ids:
                    continue   # on your list already (or found through another season)
                seen_ids.add(node["id"])
                found.append({"from": m["id"], "from_title": src_title.get("english") or src_title.get("romaji")
                              or "", "media": node})
        if progress:
            progress(min(len(sources), start + 50), len(sources))
    old = load()
    known = set(old.get("seen") or [])
    first = not old.get("t")
    new = [] if first else [s for s in found if s["media"]["id"] not in known]
    # announced before, airing now
    before = {str(s["media"]["id"]): s["media"].get("status") for s in old.get("sequels") or []}
    started = [s for s in found if s["media"].get("status") == "RELEASING"
               and before.get(str(s["media"]["id"])) == "NOT_YET_RELEASED"]
    _save({"t": time.time(), "sequels": found, "seen": sorted(known | seen_ids)})
    return found, new, started
