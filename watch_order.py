"""
Watch order for a franchise (no Qt in here): AniList's relations, followed outward from a show (prequels,
sequels, side stories, parent stories; spin-offs one step away), in release order. Chronological order isn't
something AniList records, so the guide shows release order and marks the main story (the prequel/sequel run).
Kept in watch_order_cache.json for two weeks.
"""

import json
import os
import time

import anime_organizer as org

CACHE_FILE = org.SCRIPT_DIR / "watch_order_cache.json"
KEEP = 14 * 86400
FOLLOW = {"PREQUEL", "SEQUEL", "SIDE_STORY", "PARENT", "SUMMARY"}
ONE_STEP = {"SPIN_OFF", "ALTERNATIVE"}
MAX_ENTRIES = 45
FIELDS = ("id type format status episodes seasonYear startDate { year month day } title { romaji english } "
          "coverImage { medium color }")
QUERY = ("query ($id: Int) { Media(id: $id) { " + FIELDS + " relations { edges { relationType node { " + FIELDS +
         " } } } } }")
NAMES = {"TV": "TV", "TV_SHORT": "TV short", "MOVIE": "Movie", "OVA": "OVA", "ONA": "ONA", "SPECIAL": "Special",
         "MUSIC": "Music video"}


def _load():
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(cache):
    tmp = CACHE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache), encoding="utf-8")
    os.replace(tmp, CACHE_FILE)


def _date(m):
    d = m.get("startDate") or {}
    return (d.get("year") or m.get("seasonYear") or 9999, d.get("month") or 12, d.get("day") or 31)


def franchise(mid, refresh=False, progress=None):
    """[{id, title, format, year, episodes, status, cover, color, relation, main}] in release order.
    One AniList request per entry the first time (rate-limited), then from the cache."""
    cache = _load()
    key = str(mid)
    hit = cache.get(key)
    if hit and not refresh and time.time() - hit.get("t", 0) < KEEP:
        return hit["entries"]
    nodes, rel, edges = {}, {mid: "This show"}, {}
    queue, seen = [mid], {mid}
    while queue and len(nodes) < MAX_ENTRIES:
        cur = queue.pop(0)
        data = (org.anilist(QUERY, {"id": cur}) or {}).get("Media")
        if not data:
            continue
        nodes[cur] = data
        if progress:
            progress(len(nodes))
        for e in (data.get("relations") or {}).get("edges") or []:
            node, kind = e.get("node") or {}, e.get("relationType")
            if node.get("type") != "ANIME" or not node.get("id"):
                continue
            edges.setdefault(cur, []).append((kind, node["id"]))
            if node["id"] in seen:
                continue
            if kind in FOLLOW or (kind in ONE_STEP and cur == mid):
                seen.add(node["id"])
                rel.setdefault(node["id"], kind)
                nodes.setdefault(node["id"], node)   # known now; its own relations only if it's followed further
                if kind in FOLLOW:
                    queue.append(node["id"])
    # the main story: walk prequels back from the show, then sequels forward
    main, cur = [mid], mid
    for _ in range(30):
        prev = next((t for k, t in edges.get(cur, []) if k == "PREQUEL" and t in nodes and t not in main), None)
        if prev is None:
            break
        main.insert(0, prev)
        cur = prev
    cur = mid
    for _ in range(30):
        nxt = next((t for k, t in edges.get(cur, []) if k == "SEQUEL" and t in nodes and t not in main), None)
        if nxt is None:
            break
        main.append(nxt)
        cur = nxt
    out = []
    for i, m in nodes.items():
        t = m.get("title") or {}
        out.append({"id": i, "title": t.get("english") or t.get("romaji") or "?", "romaji": t.get("romaji"),
                    "format": NAMES.get(m.get("format"), m.get("format") or "?"), "year": _date(m)[0],
                    "date": list(_date(m)), "episodes": m.get("episodes"), "status": m.get("status"),
                    "cover": (m.get("coverImage") or {}).get("medium"),
                    "color": (m.get("coverImage") or {}).get("color"),
                    "relation": rel.get(i, ""), "main": i in main})
    out.sort(key=lambda e: tuple(e["date"]))
    cache[key] = {"t": time.time(), "entries": out}
    _save(cache)
    return out
