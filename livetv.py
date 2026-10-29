"""
Live TV for Kura (no Qt in here): channels made from your library, each with a day's schedule, so tuning in plays
whatever is "on" from the right second, like real TV.

- A TV day runs 04:00 → 04:00. Each day is generated once (when first needed) and saved in livetv_schedule.json,
  so the guide never changes under you; the line-ups reshuffle every day.
- 15 automatic channels: 6 always there (Kura Mix, New Arrivals, Catch-up, Comfort Rewatch, Marathon, Movies) and 9
  themed ones picked for the day from a pool (genres, studios, eras, late night, long runners...), only themes with
  enough to fill a day. Your own channels (livetv_channels.json) come after them, or on the number you pin.
- Shows keep their story order across days: a channel remembers where each show got to (cursors), and plays it in
  blocks of episodes ("episodes 3-4 tonight, 5-6 tomorrow"). Mix channels lean on the time of day.
- Between show blocks: a short break, a mix of the next show's real opening / ending (from its Extras), a trailer
  for a show you plan to watch (played from YouTube when yt-dlp is there), and an "Up next" card.
- Episode lengths come from the files (ffprobe, cached in livetv_durations.json).
"""

import hashlib
import json
import os
import random
import re
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

DAY_START = 4                 # the TV day starts at 04:00
BREAK_CARD = 8                # "Up next" card, seconds
OPENING_MAX = 135             # longest opening / ending used in a break
TRAILER_SLOT = 75             # seconds given to a trailer "commercial"
TRAILER_GAP = 120 * 60        # at most one trailer every 2 hours on a channel
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
EXTRA_RE = re.compile(r"\b(NC ?OP|NC ?ED|creditless|opening|ending|OP ?\d{0,2}|ED ?\d{0,2})\b", re.I)
EXTRA_DIRS = re.compile(r"^(extras?|ncop|nced|nc|creditless|bonus|specials?|openings & endings|previews|commercials|menus)$", re.I)
COMMERCIAL_MAX = 150          # longest commercial / preview used in a break
COMMERCIAL_BREAK = 180        # a break's commercials add up to at most this

FIXED = [   # (id, name, colour, description)
    ("mix", "Kura Mix", "#7c6cff", "A bit of everything, leaning on the time of day."),
    ("new", "New Arrivals", "#3ecf8e", "Episodes downloaded in the last two weeks."),
    ("catchup", "Catch-up", "#3db4f2", "The shows you're watching, from your next episode. Updates your list."),
    ("comfort", "Comfort Rewatch", "#f5b642", "Shows you finished and loved."),
    ("marathon", "Marathon", "#ff5d73", "Whole shows back to back, from episode 1; different ones every day."),
    ("movies", "Movie Channel", "#c084fc", "Films, back to back."),
]
LATE = {"Horror", "Thriller", "Psychological", "Ecchi", "Mystery"}
DAYPART = [   # (from hour, to hour, genres it leans on)
    (4, 10, {"Comedy", "Slice of Life", "Adventure", "Sports", "Music"}),
    (10, 17, set()),
    (17, 23, {"Action", "Drama", "Fantasy", "Sci-Fi", "Romance", "Mecha"}),
    (23, 28, LATE),
]
COLOURS = ["#7c6cff", "#3ecf8e", "#3db4f2", "#f5b642", "#ff5d73", "#c084fc", "#f47521", "#2dd4bf", "#e879f9",
           "#60a5fa", "#facc15", "#fb7185", "#a3e635", "#38bdf8", "#f97316"]


# ---------------------------------------------------------------- days

def day_of(ts=None):
    """The TV day (YYYY-MM-DD) a moment belongs to (before 04:00 = the day before)."""
    d = datetime.fromtimestamp(time.time() if ts is None else ts) - timedelta(hours=DAY_START)
    return d.strftime("%Y-%m-%d")


def day_start(day):
    return datetime.strptime(day, "%Y-%m-%d").replace(hour=DAY_START).timestamp()


def next_day(day):
    return (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")


def rng_for(*parts):
    return random.Random(int(hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:12], 16))


# ---------------------------------------------------------------- episode lengths

class Durations:
    """Seconds per file (ffprobe), cached by size + modified time."""

    def __init__(self, path, ffprobe):
        self.path, self.ffprobe = Path(path), ffprobe
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}
        self.lock = threading.Lock()

    def key(self, f):
        st = os.stat(f)
        return f"{st.st_size}:{int(st.st_mtime)}"

    def get(self, f):
        e = self.data.get(os.path.normcase(f))
        try:
            return e[1] if e and e[0] == self.key(f) else None
        except OSError:
            return None

    def probe(self, f):
        try:
            r = subprocess.run([str(self.ffprobe()), "-v", "error", "-show_entries", "format=duration", "-of",
                                "default=nw=1:nk=1", str(f)], capture_output=True, text=True, timeout=60,
                               creationflags=NO_WINDOW)
            d = float((r.stdout or "0").strip() or 0)
        except (OSError, ValueError, subprocess.SubprocessError):
            d = 0.0
        try:   # a file ffprobe can't read is remembered as 0 too (it isn't asked again until the file changes)
            with self.lock:
                self.data[os.path.normcase(f)] = [self.key(f), round(max(d, 0.0), 3)]
        except OSError:
            pass
        return d or None

    def fill(self, files, progress=None, cancelled=None, workers=4):
        """Probe what isn't known yet, a few at a time. Returns how many were probed."""
        todo = [f for f in files if self.get(f) is None]
        from concurrent.futures import ThreadPoolExecutor
        done = 0
        pool = ThreadPoolExecutor(workers)
        try:
            for _ in pool.map(self.probe, todo):
                done += 1
                if progress and done % 10 == 0:
                    progress(done, len(todo))
                if cancelled and cancelled():
                    break
        finally:   # stopped early: the ones not started yet are dropped, not waited for
            pool.shutdown(wait=True, cancel_futures=True)
        self.save()
        return done

    def save(self):
        with self.lock:
            tmp = self.path.with_suffix(".part")
            tmp.write_text(json.dumps(self.data), encoding="utf-8")
            os.replace(tmp, self.path)


def find_extras(folder, kind_of=None, skip=()):
    """A show folder's extras Live TV can air: openings / endings, and with kind_of (the organizer's extra_kind_of:
    Extras / Openings & Endings, Previews, Commercials...) previews and commercials too. Menus and bonus material
    never; files in `skip` (normcased paths: "not this one") neither."""
    out = []
    for f in Path(folder).rglob("*"):
        if f.suffix.lower() not in (".mkv", ".mp4", ".avi", ".webm") or not f.is_file():
            continue
        if os.path.normcase(str(f)) in skip:
            continue
        in_extras = any(EXTRA_DIRS.match(p) for p in f.relative_to(folder).parts[:-1])
        name = f.stem
        kind = kind_of(f, folder) if (kind_of and in_extras) else None
        if kind in ("menu", "bonus"):
            continue
        if kind in ("preview", "commercial"):
            out.append({"path": str(f), "kind": kind})
        elif kind == "op_ed" or in_extras and EXTRA_RE.search(name) or re.search(r"\bNC ?(OP|ED)|creditless", name, re.I):
            kind = "ending" if re.search(r"\bNC ?ED|\bED ?\d{0,2}\b|ending", name, re.I) else "opening"
            out.append({"path": str(f), "kind": kind})
    return out


# ---------------------------------------------------------------- channels

def _has(show, genres):
    return bool(set(show.get("genres") or []) & set(genres))


def theme_pool(shows):
    """Every themed channel that could run today: (id, name, filter)."""
    pool = []
    genres = {}
    for s in shows:
        for g in s.get("genres") or []:
            genres.setdefault(g, 0)
            genres[g] += 1
    for g in sorted(genres):
        pool.append((f"genre:{g}", f"{g} Channel" if g not in ("Slice of Life", "Sci-Fi") else
                     {"Slice of Life": "Slice of Life TV", "Sci-Fi": "Sci-Fi Zone"}[g], lambda s, g=g: g in (s.get("genres") or [])))
    studios = {}
    for s in shows:
        for st in s.get("studios") or []:
            studios.setdefault(st, 0)
            studios[st] += 1
    for st, n in studios.items():
        if n >= 4:
            pool.append((f"studio:{st}", f"{st} Presents", lambda s, st=st: st in (s.get("studios") or [])))
    for start, label in ((1990, "90s Classics"), (2000, "2000s Classics"), (2010, "2010s Hits"), (2020, "2020s Now")):
        pool.append((f"era:{start}", label, lambda s, a=start: a <= (s.get("year") or 0) < a + 10))
    pool += [
        ("late", "Late Night", lambda s: _has(s, LATE)),
        ("saturday", "Saturday Morning", lambda s: _has(s, {"Comedy", "Adventure"}) and not _has(s, LATE)),
        ("long", "Long Runners", lambda s: len(s["eps"]) >= 40),
        ("onecour", "One-Cour Wonders", lambda s: 10 <= len(s["eps"]) <= 14),
        ("shorts", "Shorts", lambda s: s.get("format") == "TV_SHORT"),
        ("gems", "Hidden Gems", lambda s: not s.get("status")),
        ("highscore", "Critics' Picks", lambda s: (s.get("score") or 0) >= 80),
        ("thisyear", "This Year", lambda s: (s.get("year") or 0) >= datetime.now().year - 1),
    ]
    return pool


def _enough(shows, need_shows=4, need_hours=8):
    return len(shows) >= need_shows and sum(e["dur"] for s in shows for e in s["eps"]) >= need_hours * 3600


def auto_channels(shows, day, want=15):
    """The day's automatic channels: [{id, number, name, colour, desc, shows: [keys], mode, track}]"""
    rng = rng_for("lineup", day)
    tv = [s for s in shows if s.get("format") != "MOVIE" and s["eps"]]
    now = day_start(day)
    chans = []

    def add(cid, name, colour, desc, members, mode="blocks", track=False, extra=None):
        chans.append({"id": cid, "number": len(chans) + 1, "name": name, "colour": colour, "desc": desc,
                      "shows": [s["key"] for s in members], "mode": mode, "track": track, "auto": True,
                      **(extra or {})})
    fixed = dict((f[0], f) for f in FIXED)
    add("mix", *fixed["mix"][1:], tv, mode="daypart")
    recent = [s for s in tv if any(now - e.get("mtime", 0) < 14 * 86400 for e in s["eps"])]
    add("new", *fixed["new"][1:], recent or tv, mode="new")
    watching = [s for s in tv if s.get("status") in ("CURRENT", "REPEATING")]
    add("catchup", *fixed["catchup"][1:], watching or tv, mode="catchup", track=True)
    comfort = [s for s in tv if s.get("status") == "COMPLETED" and ((s.get("my_score") or 0) >= 8 or
                                                                   (s.get("score") or 0) >= 78)]
    add("comfort", *fixed["comfort"][1:], comfort or [s for s in tv if s.get("status") == "COMPLETED"] or tv)
    long_enough = [s for s in tv if len(s["eps"]) >= 12] or tv
    picks = rng.sample(long_enough, min(6, len(long_enough))) if long_enough else []
    add("marathon", *fixed["marathon"][1:], picks, mode="marathon")
    movies = [s for s in shows if s.get("format") == "MOVIE" and s["eps"]]
    add("movies", *fixed["movies"][1:], movies or tv, mode="shuffle")
    pool = []
    for cid, name, fn in theme_pool(tv):
        members = [s for s in tv if fn(s)]
        if _enough(members):
            pool.append((cid, name, members))
    rng.shuffle(pool)
    used = set()
    for cid, name, members in pool:
        if len(chans) >= want:
            break
        sig = frozenset(s["key"] for s in members)
        if sig in used:
            continue
        used.add(sig)
        add(cid, name, COLOURS[len(chans) % len(COLOURS)], "", members,
            mode="daypart" if cid in ("late", "saturday") else "blocks")
    return chans


def custom_channel_shows(ch, shows):
    """The shows a channel you made covers (picked by hand, or by its rules)."""
    if ch.get("pick"):
        keys = {os.path.normcase(k) for k in ch["pick"]}
        return [s for s in shows if os.path.normcase(s["key"]) in keys]
    r = ch.get("rules") or {}
    out = []
    for s in shows:
        if not s["eps"]:
            continue
        if r.get("genres") and not set(r["genres"]) & set(s.get("genres") or []):
            continue
        if r.get("exclude_genres") and set(r["exclude_genres"]) & set(s.get("genres") or []):
            continue
        if r.get("studios") and not set(r["studios"]) & set(s.get("studios") or []):
            continue
        if r.get("status") and (s.get("status") or "NONE") not in r["status"]:
            continue
        if r.get("formats") and s.get("format") not in r["formats"]:
            continue
        y = s.get("year") or 0
        if r.get("year_from") and y < r["year_from"] or r.get("year_to") and y > r["year_to"]:
            continue
        if r.get("min_score") and (s.get("score") or 0) < r["min_score"]:
            continue
        out.append(s)
    return out


# ---------------------------------------------------------------- one day of one channel

def _breaks_for(next_show, shows_by_key, planning, rng, settings, t=0.0, memo=None):
    """The programmes between two show blocks."""
    items = []
    memo = {} if memo is None else memo
    roll = rng.random()
    ex = [e for e in (next_show.get("extras") or []) if e.get("dur") and e["dur"] <= OPENING_MAX
          and e["kind"] in ("opening", "ending")]
    ads = [(s, e) for s in shows_by_key.values() for e in (s.get("extras") or [])
           if e["kind"] in ("commercial", "preview") and e.get("dur") and e["dur"] <= COMMERCIAL_MAX]
    if ex and roll < 0.40 and settings.get("live_openings", True):
        e = rng.choice(ex)
        items.append({"kind": "opening", "path": e["path"], "dur": e["dur"], "show": next_show["key"],
                      "title": f"{next_show['name']} · {e['kind'].title()}", "mid": next_show.get("mid")})
    elif ads and roll < 0.70 and settings.get("live_commercials", True):
        # a commercial break: the shows' own CMs and PVs, one to three, about three minutes at most
        total = 0
        for s, e in rng.sample(ads, min(len(ads), rng.randint(1, 3))):
            if total + e["dur"] > COMMERCIAL_BREAK and total:
                break
            total += e["dur"]
            items.append({"kind": "commercial", "path": e["path"], "dur": e["dur"], "show": s["key"], "mid": s.get("mid"),
                          "title": f"{s['name']} · {'Commercial' if e['kind'] == 'commercial' else 'Preview'}"})
    elif planning and roll < 0.85 and settings.get("live_trailers", True) and \
            t - memo.get("trailer", -1e12) >= TRAILER_GAP:
        memo["trailer"] = t
        p = rng.choice(planning)
        items.append({"kind": "trailer", "dur": TRAILER_SLOT, "mid": p["mid"], "trailer": p["trailer"],
                      "title": f"Coming soon to your list: {p['title']}", "cover": p.get("cover")})
    if settings.get("live_cards", True) or not items:
        items.append({"kind": "card", "dur": BREAK_CARD, "show": next_show["key"], "title": f"Up next: {next_show['name']}",
                      "mid": next_show.get("mid")})
    return items


def _order(show):
    return sorted(show["eps"], key=lambda e: (e.get("season") or 0, e.get("ep") or 0))


def build_day(ch, shows, day, cursors, planning=(), settings=None):
    """[programme] filling one TV day: {start, end, kind: episode / opening / trailer / card, ...}."""
    settings = settings or {}
    rng = rng_for("day", day, ch["id"])
    members = [s for s in shows if s["key"] in set(ch["shows"])] if ch.get("auto") else custom_channel_shows(ch, shows)
    members = [s for s in members if s["eps"]]
    t, end = day_start(day), day_start(next_day(day))
    progs = []
    if not members:
        progs.append({"kind": "card", "start": t, "end": end, "dur": end - t, "title": "Nothing to show on this channel"})
        return progs
    cur = cursors.setdefault(ch["id"], {})
    by_key = {s["key"]: s for s in members}
    mode = ch.get("mode", "blocks")
    block = int(ch.get("block") or (1 if mode in ("shuffle", "random") else 2))
    slots = sorted(ch.get("slots") or [], key=lambda x: x.get("time", "00:00"))   # [{"time": "20:00", "show": key}]

    def next_eps(show, n):
        eps = _order(show)
        if mode == "random":
            return [rng.choice(eps) for _ in range(n)]
        i = cur.get(show["key"])
        if i is None:
            i = show.get("start_index", 0) if mode == "catchup" else 0
        out = []
        for _ in range(n):
            out.append(eps[i % len(eps)])
            i += 1
        cur[show["key"]] = i % len(eps)
        return out

    def weight(show, hour):
        if mode != "daypart":
            return 1.0
        for a, b, g in DAYPART:
            if a <= hour < b or a <= hour + 24 < b:
                return 3.0 if g and _has(show, g) else (0.3 if g and _has(show, LATE) and a != 23 else 1.0)
        return 1.0

    def slot_due(at):
        for sl in slots:
            hh, mm = map(int, sl["time"].split(":"))
            st = datetime.fromtimestamp(day_start(day)).replace(hour=hh, minute=mm).timestamp()
            if st < day_start(day):
                st += 86400
            if at <= st < at + 3600 and not sl.get("_done"):
                return sl, st
        return None, None

    last = None
    memo = {}
    # Marathon: the day's shows one after another, each from its first episode (members keep the day's order)
    queue = [by_key[k] for k in ch["shows"] if k in by_key] if mode == "marathon" else []
    marathon_left, m_show = [], None
    while t < end - 60:
        sl, st = slot_due(t)
        if sl and sl["show"] in by_key and st - t < 60 * 30:
            sl["_done"] = True
            if st > t:   # a card until the slot's time
                progs.append({"kind": "card", "start": t, "end": st, "dur": st - t,
                              "title": f"Up next: {by_key[sl['show']]['name']}", "show": sl["show"]})
                t = st
            show = by_key[sl["show"]]
            n = int(sl.get("count") or block)
        else:
            if mode == "marathon":
                if not marathon_left:
                    m_show = queue.pop(0) if queue else rng.choice(members)
                    queue.append(m_show)
                    marathon_left = list(_order(m_show))
                show = m_show
            elif mode == "new":
                show = rng.choice(members)
            else:
                hour = datetime.fromtimestamp(t).hour
                pool = [s for s in members if s["key"] != (last or {}).get("key")] or members
                ws = [weight(s, hour) for s in pool]
                show = rng.choices(pool, ws)[0]
            n = 1 if mode in ("shuffle", "random", "new") else (len(marathon_left) if mode == "marathon" else block)
        if last is not None and show["key"] != last["key"]:
            for b in _breaks_for(show, by_key, planning, rng, settings, t, memo):
                if t + b["dur"] > end:
                    break
                progs.append({**b, "start": t, "end": t + b["dur"]})
                t += b["dur"]
        if mode == "marathon":
            eps, marathon_left = marathon_left, []
        elif mode == "new":
            eps = [rng.choice(sorted(show["eps"], key=lambda e: -e.get("mtime", 0))[:6])]
        else:
            eps = next_eps(show, n)
        for e in eps:
            if t + e["dur"] > end:
                break
            progs.append({"kind": "episode", "start": t, "end": t + e["dur"], "dur": e["dur"], "path": e["path"],
                          "show": show["key"], "name": show["name"], "mid": e.get("sid") or show.get("mid"),
                          "season": e.get("season"), "ep": e.get("ep"), "title": show["name"],
                          "cover": show.get("cover")})
            t += e["dur"]
        else:
            last = show
            continue
        break   # the next episode doesn't fit before 04:00
    if t < end:   # the rest of the night: a card until the new day's line-up
        progs.append({"kind": "card", "start": t, "end": end, "dur": end - t, "title": "New line-up at 4:00"})
    for sl in slots:
        sl.pop("_done", None)
    return progs


# ---------------------------------------------------------------- the whole thing

class LiveTV:
    """Schedules on disk + what's on. `shows` = [{key (folder), name, mid, genres, studios, format, year, score,
    status, my_score, cover, trailer, eps: [{path, dur, season, ep, sid, mtime}], extras: [...] }]."""

    def __init__(self, data_dir, prefix="livetv_"):
        """prefix: "livetv_adult_" for Kura's 18+ side (its own channels and schedule)."""
        self.dir = Path(data_dir)
        self.sched_file = self.dir / f"{prefix}schedule.json"
        self.chan_file = self.dir / f"{prefix}channels.json"
        try:
            self.state = json.loads(self.sched_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.state = {}
        self.state.setdefault("days", {})
        self.state.setdefault("cursors", {})
        self.state.setdefault("reminders", [])
        try:
            self.custom = json.loads(self.chan_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.custom = []
        self.lock = threading.RLock()

    # ---- files
    def save(self):
        with self.lock:
            keep = sorted(self.state["days"])[-3:]   # yesterday, today, tomorrow
            self.state["days"] = {d: self.state["days"][d] for d in keep}
            tmp = self.sched_file.with_suffix(".part")
            tmp.write_text(json.dumps(self.state), encoding="utf-8")
            os.replace(tmp, self.sched_file)

    def save_channels(self):
        tmp = self.chan_file.with_suffix(".part")
        tmp.write_text(json.dumps(self.custom, indent=1), encoding="utf-8")
        os.replace(tmp, self.chan_file)

    # ---- days
    def has_day(self, day):
        return day in self.state["days"]

    def make_day(self, day, shows, planning=(), settings=None, force=False):
        """Generate one TV day for every channel (automatic + yours)."""
        with self.lock:
            if day in self.state["days"] and not force:
                return self.state["days"][day]
            chans = auto_channels(shows, day)
            taken = {c["number"] for c in self.custom if c.get("number")}
            n = 16
            for c in self.custom:
                ch = dict(c, auto=False)
                if not ch.get("number"):
                    while n in taken:
                        n += 1
                    ch["number"] = n
                    taken.add(n)
                    n += 1
                chans.append(ch)
            out = {"channels": [], "made": time.time()}
            cursors = self.state["cursors"]
            for ch in chans:
                progs = build_day(ch, shows, day, cursors, planning, settings)
                meta = {k: v for k, v in ch.items() if k not in ("shows", "pick", "rules", "slots")}
                out["channels"].append({**meta, "programs": progs})
            out["channels"].sort(key=lambda c: c["number"])
            self.state["days"][day] = out
            self.save()
            return out

    def forget_from(self, day):
        """Your channels changed: today and later are made again (the past stays as it aired)."""
        with self.lock:
            for d in list(self.state["days"]):
                if d >= day:
                    self.state["days"].pop(d)

    # ---- what's on
    def channels(self, day=None):
        d = self.state["days"].get(day or day_of())
        return d["channels"] if d else []

    def channel(self, cid, day=None):
        return next((c for c in self.channels(day) if c["id"] == cid), None)

    def on_at(self, cid, ts=None):
        """(programme, seconds into it) on a channel at a moment."""
        ts = time.time() if ts is None else ts
        ch = self.channel(cid, day_of(ts))
        if not ch:
            return None, 0.0
        for p in ch["programs"]:
            if p["start"] <= ts < p["end"]:
                return p, ts - p["start"]
        return None, 0.0

    def after(self, cid, prog):
        """The programme following this one (into the next day when needed)."""
        p, _ = self.on_at(cid, prog["end"] + 0.01)
        return p

    def upcoming(self, cid, ts=None, count=3):
        ts = time.time() if ts is None else ts
        out, p = [], self.on_at(cid, ts)[0]
        while p and len(out) < count:
            p = self.after(cid, p)
            if p and p["kind"] == "episode":
                out.append(p)
        return out
