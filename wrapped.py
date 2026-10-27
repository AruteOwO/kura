"""
Year in review ("Wrapped") for Kura (no Qt in here). What was actually watched that year comes from:
  - your Crunchyroll history (Tools → Crunchyroll import): every episode with the date it was played and its
    length; AniList's own dates aren't enough (a list import gave Boruto, watched in 2017-2018, a 2026 start);
  - Kura's player (kura_watch.json: time watched per day, episodes finished);
  - shows you completed on AniList that year and didn't watch on Crunchyroll (torrents, Jellyfin...): counted
    whole, at their episode length.
Without a Crunchyroll history it falls back to AniList alone (completed that year + shows started that year).
"""

import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
PERSONAS = {
    "Action": ("Adrenaline Junkie", "Fights, chases and big finishes."),
    "Adventure": ("Trailblazer", "Always off to somewhere new."),
    "Comedy": ("Comedy Connoisseur", "In it for the laughs."),
    "Drama": ("Feels Seeker", "The more it hurts, the better."),
    "Ecchi": ("Fan-Service Aficionado", "You know what you like."),
    "Fantasy": ("Isekai Wanderer", "Swords, magic and other worlds."),
    "Horror": ("Fearless", "Lights off, volume up."),
    "Mahou Shoujo": ("Magical Girl Devotee", "Transformation sequences, every time."),
    "Mecha": ("Pilot", "Giant robots, giant feelings."),
    "Music": ("Idol Fan", "Front row for every live."),
    "Mystery": ("Detective", "Always one clue ahead."),
    "Psychological": ("Mind Bender", "Shows that play with your head."),
    "Romance": ("Hopeless Romantic", "Will they, won't they: they'd better."),
    "Sci-Fi": ("Future Gazer", "Starships, AIs and what comes next."),
    "Slice of Life": ("Cozy Watcher", "Tea, quiet days and good company."),
    "Sports": ("Team Player", "One more match."),
    "Supernatural": ("Spirit Chaser", "Ghosts, curses and things that go bump."),
    "Thriller": ("Edge-of-Seat", "Never sure what happens next."),
}


def _y(d):
    return (d or {}).get("year")


def _played(x):
    """A Crunchyroll history entry's play time (local)."""
    t = x.get("played") or x.get("first_played")
    try:
        return datetime.fromisoformat(str(t).replace("Z", "+00:00")).astimezone() if t else None
    except ValueError:
        return None


def years_with_data(entries, cr_episodes=None):
    ys = {_y(e.get("completedAt")) for e in entries.values()}
    ys |= {p.year for p in (_played(x) for x in (cr_episodes or {}).values()) if p}
    return sorted((y for y in ys if y), reverse=True)


def title(m):
    t = (m or {}).get("title") or {}
    return t.get("english") or t.get("romaji") or "?"


def compute(year, entries, score_format="POINT_10", watchlog=None, history=None, cr_episodes=None, cr_map=None):
    """Everything the Year in review shows, for one year."""
    completed = [e for e in entries.values() if e.get("media") and _y(e.get("completedAt")) == year
                 and e.get("status") in ("COMPLETED", "REPEATING")]
    per_mid = Counter()          # episodes watched this year, per AniList show
    per_day, per_month = Counter(), [0] * 12
    episodes, minutes = 0, 0.0
    series = Counter()           # Crunchyroll series title -> episodes this year
    source = "anilist"

    # 1. Crunchyroll: what was really played this year
    cr = [x for x in (cr_episodes or {}).values() if x.get("watched") and (_played(x) or datetime.min).year == year]
    if cr_episodes:
        source = "crunchyroll"
        for x in cr:
            p = _played(x)
            episodes += 1
            minutes += (x.get("duration") or 1440) / 60
            per_day[p.date().isoformat()] += 1
            per_month[p.month - 1] += 1
            series[x.get("series_title") or "?"] += 1
            hit = (cr_map or {}).get(x.get("season_id")) or (cr_map or {}).get(f"series:{x.get('series_id')}") or {}
            mid = hit.get("mid") if isinstance(hit, dict) else hit
            if mid:
                per_mid[mid] += 1
    cr_mids = set(per_mid)

    # 2. Kura's player
    kura = {"seconds": 0.0, "days": 0, "best_day": None, "finished": 0, "torrents": 0}
    for day, sec in ((watchlog or {}).get("days") or {}).items():
        if day.startswith(str(year)):
            kura["seconds"] += sec
            kura["days"] += 1
            if not kura["best_day"] or sec > kura["best_day"][1]:
                kura["best_day"] = (day, sec)
    for t in ((watchlog or {}).get("finished") or {}).values():
        d = datetime.fromtimestamp(t)
        if d.year == year:
            kura["finished"] += 1
            episodes += 1
            per_day[d.date().isoformat()] += 1
            per_month[d.month - 1] += 1
    minutes += kura["seconds"] / 60
    kura["torrents"] = sum(1 for h in (history or {}).values()
                           if h.get("t") and datetime.fromtimestamp(h["t"]).year == year)

    # 3. AniList: shows completed this year that weren't watched on Crunchyroll
    def eps_of(e):
        return (e.get("media") or {}).get("episodes") or e.get("progress") or 0
    for e in completed:
        mid = e["media"]["id"]
        if mid in cr_mids:
            continue
        n = eps_of(e)
        per_mid[mid] += n
        episodes += n
        minutes += n * (e["media"].get("duration") or 24)
        if not cr_episodes:
            mo = (e.get("completedAt") or {}).get("month")
            if mo:
                per_month[mo - 1] += n
    if not cr_episodes:   # no real play dates: shows started this year count the episodes you're on
        for e in entries.values():
            if e.get("media") and e.get("status") in ("CURRENT", "PAUSED", "DROPPED") and \
                    _y(e.get("startedAt")) == year and (e.get("progress") or 0):
                per_mid[e["media"]["id"]] += e["progress"]
                episodes += e["progress"]
                minutes += e["progress"] * (e["media"].get("duration") or 24)

    genres, studios = Counter(), Counter()
    for mid, n in per_mid.items():
        m = (entries.get(mid) or {}).get("media") or {}
        for g in m.get("genres") or []:
            genres[g] += n
        for s in ((m.get("studios") or {}).get("nodes") or []):
            # AniList marks the real animation studios; "main" studios can also be a broadcaster or distributor
            # (Nippon Television, Crunchyroll). Entries synced before Kura asked have no mark: Kura's list skips those
            if s.get("name") and s.get("isAnimationStudio") is not False:
                studios[s["name"]] += n
    gtotal = sum(genres.values()) or 1

    scored = sorted([e for e in completed if e.get("score")], key=lambda e: (-e["score"], -eps_of(e)))
    top = scored + sorted([e for e in completed if not e.get("score")], key=lambda e: -eps_of(e))
    if len(top) < 5:   # not many finished: the shows you watched most this year
        have = {e["media"]["id"] for e in top}
        for mid, _ in per_mid.most_common():
            e = entries.get(mid)
            if e and e.get("media") and mid not in have:
                top.append(e)
                have.add(mid)
            if len(top) >= 5:
                break

    def when(e):
        d = e.get("completedAt") or {}
        return (d.get("month") or 0, d.get("day") or 0)
    dated = sorted((e for e in completed if (e.get("completedAt") or {}).get("month")), key=when)
    busiest = max(range(12), key=lambda i: per_month[i]) if any(per_month) else None
    best_day = max(per_day.items(), key=lambda kv: kv[1]) if per_day else None
    scored_all = [e["score"] for e in completed if e.get("score")]
    top_genre = genres.most_common(1)[0][0] if genres else None
    persona = PERSONAS.get(top_genre, ("Anime Explorer", "A bit of everything."))
    return {
        "year": year, "source": source, "completed": len(completed), "episodes": episodes,
        # shows watched: the AniList ones, or every Crunchyroll series when more of those aren't linked
        "shows": max(len(set(per_mid) | {e["media"]["id"] for e in completed}), len(series)),
        "series_watched": len(series), "minutes": minutes, "hours": round(minutes / 60),
        "days": round(minutes / 1440, 1), "watch_days": len(per_day),
        "top": [{"id": e["media"]["id"], "title": title(e["media"]), "score": e.get("score"),
                 "cover": ((e["media"].get("coverImage") or {}).get("extraLarge")),
                 "color": ((e["media"].get("coverImage") or {}).get("color")), "episodes": eps_of(e)}
                for e in top[:5]],
        "most_watched": series.most_common(3),
        "genres": [(g, n, round(100 * n / gtotal)) for g, n in genres.most_common(6)],
        "studios": studios.most_common(5), "months": per_month,
        "busiest": (MONTHS[busiest], per_month[busiest]) if busiest is not None else None,
        "best_day": (datetime.fromisoformat(best_day[0]).strftime("%d %b"), best_day[1]) if best_day else None,
        "first": title(dated[0]["media"]) if dated else None, "last": title(dated[-1]["media"]) if dated else None,
        "movies": sum(1 for e in completed if (e.get("media") or {}).get("format") == "MOVIE"),
        "seasonal": sum(1 for mid in set(per_mid) | {e["media"]["id"] for e in completed}
                        if ((entries.get(mid) or {}).get("media") or {}).get("seasonYear") == year),
        "average": (sum(scored_all) / len(scored_all)) if scored_all else None, "score_format": score_format,
        "persona": (top_genre, *persona), "kura": kura, "made": time.time(),
    }


def score_text(score, fmt):
    if score is None:
        return "–"
    if fmt == "POINT_100":
        return f"{score:.0f}"
    return f"{score:.1f}".rstrip("0").rstrip(".")
