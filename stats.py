"""
Statistics for Kura's Statistics page. No Qt in here.

Two sources for the same numbers:
  - "anilist": AniList's own statistics for your profile (what anilist.co shows; AniList updates
    them now and then, so they can lag behind your list);
  - "live": worked out from your synced list (anilist_list.json), so they always match it.
    Tags only exist in AniList's statistics, so both sources use AniList's tags.
Both are normalized to one shape (see `normalize`). Also here: Kura's own numbers (library size,
time watched in Kura's player, downloads per month).
"""

import json
import os
import threading
import time
from datetime import date, datetime

import anime_organizer as org

STATS_FILE = org.SCRIPT_DIR / "anilist_stats.json"
WATCH_FILE = org.SCRIPT_DIR / "kura_watch.json"
MAX_AGE = 6 * 3600          # AniList's statistics are refetched after this (or with Refresh)
SCORE_MAX = {"POINT_100": 100, "POINT_10_DECIMAL": 10, "POINT_10": 10, "POINT_5": 5, "POINT_3": 3}
VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".m4v", ".webm", ".mov", ".wmv", ".ts", ".flv"}

_STAT_FIELDS = "count meanScore minutesWatched"
QUERY_STATS = ("query { Viewer { id name siteUrl createdAt bannerImage avatar { large } "
               "mediaListOptions { scoreFormat } "
               "stats { activityHistory { date amount level } } "
               "statistics { anime { count episodesWatched minutesWatched meanScore standardDeviation "
               f"genres(sort: COUNT_DESC) {{ genre {_STAT_FIELDS} }} "
               f"tags(limit: 40, sort: COUNT_DESC) {{ tag {{ name category }} {_STAT_FIELDS} }} "
               f"studios(limit: 30, sort: COUNT_DESC) {{ studio {{ name isAnimationStudio }} {_STAT_FIELDS} }} "
               "formats(sort: COUNT_DESC) { format count } "
               "statuses(sort: COUNT_DESC) { status count } "
               "scores(sort: MEAN_SCORE_DESC) { score count } "
               "releaseYears(sort: ID_DESC) { releaseYear count } "
               "startYears(sort: ID_DESC) { startYear count } } } } }")


# ---------------------------------------------------------------- AniList's statistics

def fetch(token):
    """AniList's statistics for the signed-in account; saved to anilist_stats.json."""
    from anilist_account import gql
    viewer = gql(QUERY_STATS, token=token).get("Viewer") or {}
    data = {"fetched": time.time(), "viewer": viewer}
    tmp = STATS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, STATS_FILE)
    return data


def load():
    try:
        return json.loads(STATS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def stale(saved):
    return not saved or time.time() - saved.get("fetched", 0) > MAX_AGE


def bucket(score, fmt):
    """A score in your format → 1..10 (for the score chart)."""
    top = SCORE_MAX.get(fmt, 10)
    return max(1, min(10, int(score / top * 10 + 0.5)))


def user_info(viewer):
    return {"id": viewer.get("id"), "name": viewer.get("name") or "", "url": viewer.get("siteUrl") or "",
            "avatar": (viewer.get("avatar") or {}).get("large"), "banner": viewer.get("bannerImage"),
            "since": viewer.get("createdAt")}


def activity(viewer):
    """[(date, amount)] of days you changed your list (AniList keeps a few months)."""
    hist = ((viewer.get("stats") or {}).get("activityHistory")) or []
    return [(date.fromtimestamp(h["date"]), h.get("amount") or 0) for h in hist if h.get("date")]


def _group(rows, key):
    out = []
    for r in rows or []:
        name = key(r)
        if name:
            out.append({"name": name, "count": r.get("count") or 0, "mean": r.get("meanScore") or 0,
                        "minutes": r.get("minutesWatched") or 0})
    return out


def normalize(viewer):
    """AniList's statistics → Kura's shape."""
    a = ((viewer.get("statistics") or {}).get("anime")) or {}
    fmt = ((viewer.get("mediaListOptions") or {}).get("scoreFormat")) or "POINT_10"
    scores = {}
    for s in a.get("scores") or []:
        if s.get("score"):
            b = bucket(s["score"], fmt)
            scores[b] = scores.get(b, 0) + (s.get("count") or 0)
    return {
        "count": a.get("count") or 0,
        "episodes": a.get("episodesWatched") or 0,
        "minutes": a.get("minutesWatched") or 0,
        "mean": a.get("meanScore") or 0,
        "statuses": {s["status"]: s.get("count") or 0 for s in a.get("statuses") or [] if s.get("status")},
        "formats": [(f["format"], f.get("count") or 0) for f in a.get("formats") or [] if f.get("format")],
        "genres": _group(a.get("genres"), lambda r: r.get("genre")),
        "tags": _group(a.get("tags"), lambda r: (r.get("tag") or {}).get("name")),
        "studios": _group([s for s in a.get("studios") or [] if (s.get("studio") or {}).get("isAnimationStudio", True)],
                          lambda r: (r.get("studio") or {}).get("name")),
        "scores": scores,
        "start_years": {y["startYear"]: y.get("count") or 0 for y in a.get("startYears") or [] if y.get("startYear")},
        "release_years": {y["releaseYear"]: y.get("count") or 0 for y in a.get("releaseYears") or []
                          if y.get("releaseYear")},
    }


# ---------------------------------------------------------------- live, from your list

def live(entries, fmt, anilist_stats=None):
    """The same numbers from your synced list. Genres/studios/minutes need the list's media to have
    genres/duration/studios (synced since Kura asks for them); until then AniList's are used.
    Planning entries count for the status chart and the total, not for what you've watched."""
    top = SCORE_MAX.get(fmt, 10)
    base = anilist_stats or {}
    statuses, formats, scores, start_years, release_years = {}, {}, {}, {}, {}
    genres, studios = {}, {}
    episodes = minutes = 0
    scored = []
    has_genres = any("genres" in (e.get("media") or {}) for e in entries.values())
    for e in entries.values():
        st = e.get("status")
        if not st:
            continue
        statuses[st] = statuses.get(st, 0) + 1
        if st == "PLANNING":
            continue
        m = e.get("media") or {}
        eps = (e.get("progress") or 0) + (e.get("repeat") or 0) * (m.get("episodes") or e.get("progress") or 0)
        mins = eps * (m.get("duration") or 24)
        episodes += eps
        minutes += mins
        if m.get("format"):
            formats[m["format"]] = formats.get(m["format"], 0) + 1
        score = e.get("score") or 0
        norm = score / top * 100 if score else 0
        if score:
            scored.append(norm)
            b = bucket(score, fmt)
            scores[b] = scores.get(b, 0) + 1
        y = (e.get("startedAt") or {}).get("year")
        if y:
            start_years[y] = start_years.get(y, 0) + 1
        if m.get("seasonYear"):
            release_years[m["seasonYear"]] = release_years.get(m["seasonYear"], 0) + 1
        for g in m.get("genres") or []:
            _add(genres, g, norm, mins)
        for s in ((m.get("studios") or {}).get("nodes") or []):
            if s.get("name"):
                _add(studios, s["name"], norm, mins)
    return {
        "count": sum(statuses.values()),
        "episodes": episodes,
        "minutes": minutes,
        "mean": sum(scored) / len(scored) if scored else 0,
        "statuses": statuses,
        "formats": sorted(formats.items(), key=lambda kv: -kv[1]),
        "genres": _finish(genres) if has_genres else base.get("genres", []),
        "tags": base.get("tags", []),
        "studios": _finish(studios) if has_genres else base.get("studios", []),
        "scores": scores,
        "start_years": start_years,
        "release_years": release_years,
        "partial": not has_genres,
    }


def _add(table, name, norm, mins):
    t = table.setdefault(name, {"name": name, "count": 0, "minutes": 0, "scores": []})
    t["count"] += 1
    t["minutes"] += mins
    if norm:
        t["scores"].append(norm)


def _finish(table):
    out = []
    for t in table.values():
        sc = t.pop("scores")
        out.append({**t, "mean": sum(sc) / len(sc) if sc else 0})
    return sorted(out, key=lambda r: (-r["count"], r["name"]))


# ---------------------------------------------------------------- Kura's own numbers

def library_sizes(show_dirs):
    """{show folder: bytes of video} (walks the folders; run it in a background thread)."""
    out = {}
    for d in show_dirs:
        total = 0
        for root, _dirs, files in os.walk(d):
            for f in files:
                if os.path.splitext(f)[1].lower() in VIDEO_EXTS:
                    try:
                        total += os.path.getsize(os.path.join(root, f))
                    except OSError:
                        pass
        out[str(d)] = total
    return out


def downloads_per_month(history, months=12):
    """[(date of the month's 1st, torrents sent)] for the last `months` months, from torrent_history.json."""
    today = date.today()
    keys = []
    y, m = today.year, today.month
    for _ in range(months):
        keys.append((y, m))
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    counts = {k: 0 for k in keys}
    for h in (history or {}).values():
        t = h.get("t")
        if not t:
            continue
        d = datetime.fromtimestamp(t)
        if (d.year, d.month) in counts:
            counts[(d.year, d.month)] += 1
    return [(date(y, m, 1), counts[(y, m)]) for y, m in reversed(keys)]


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


class WatchLog:
    """Time spent watching in Kura's player: fed by the player's progress (every ~2 s while playing).
    kura_watch.json = {seconds, days: {"2026-10-20": seconds}, finished: {file: time}}."""

    def __init__(self, path=None):
        self.path = path or WATCH_FILE
        self.lock = threading.Lock()
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}
        self.data.setdefault("seconds", 0.0)
        self.data.setdefault("days", {})
        self.data.setdefault("finished", {})
        self.last = None        # (file, wall time) of the last progress report
        self.dirty_since = 0

    def on_progress(self, info):
        now = time.time()
        path = info.get("path")
        if not path:
            return
        if not info.get("paused") and self.last and self.last[0] == path:
            gap = now - self.last[1]
            if 0 < gap < 6:   # longer gaps = paused, seeking or a stall: not counted
                day = date.today().isoformat()
                with self.lock:
                    self.data["seconds"] += gap
                    self.data["days"][day] = self.data["days"].get(day, 0) + gap
                self.dirty_since = self.dirty_since or now
        self.last = (path, now)
        dur, pos = info.get("duration") or 0, info.get("pos") or 0
        if dur and pos >= dur * 0.9 and path not in self.data["finished"]:
            with self.lock:
                self.data["finished"][path] = now
            self.save()
        elif self.dirty_since and now - self.dirty_since > 60:
            self.save()

    def save(self):
        with self.lock:
            text = json.dumps(self.data, ensure_ascii=False)
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, self.path)
            self.dirty_since = 0
        except OSError:
            pass

    @property
    def hours(self):
        return self.data["seconds"] / 3600

    @property
    def finished(self):
        return len(self.data["finished"])
