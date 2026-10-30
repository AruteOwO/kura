"""
Torrent feed for Kura, modelled on Taiga's: read an RSS feed (Nyaa by default),
recognise each release with anitopy, match it to your shows and pick the new
episodes worth downloading. Chosen torrents go to qBittorrent (or whatever app
opens magnet links on this PC).

No Qt in here; kura.pyw supplies what it knows about your list and library.
"""

import email.utils
import html
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import anitopy

import anime_organizer as org

DEFAULT_FEED = "https://nyaa.si/?page=rss&c=1_2&f=0"   # Anime - English-translated
DEFAULT_SEARCH_FEED = "https://nyaa.si/?page=rss&c=1_2&f=0&q=%title%"
HISTORY_FILE = org.SCRIPT_DIR / "torrent_history.json"
FILTERS_FILE = org.SCRIPT_DIR / "torrent_filters.json"
NYAA = "{https://nyaa.si/xmlns/nyaa}"
TRACKERS = ["http://nyaa.tracker.wf:7777/announce", "udp://open.stealth.si:80/announce",
            "udp://tracker.opentrackr.org:1337/announce", "udp://exodus.desync.com:6969/announce",
            "udp://tracker.torrent.eu.org:451/announce"]


# ---------------------------------------------------------------- feed

def search_url(template, query):
    """Search feed for a title: %title% in the template is replaced (like Taiga)."""
    template = template or DEFAULT_SEARCH_FEED
    q = urllib.parse.quote_plus(query)
    if "%title%" in template:
        return template.replace("%title%", q)
    return f"{template}{'&' if '?' in template else '?'}q={q}"


def fetch(url):
    """Items of an RSS feed. Nyaa's extra fields (seeders, info hash...) are used when present."""
    req = urllib.request.Request(url, headers={"User-Agent": org.USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        root = ET.fromstring(resp.read())
    items = []
    # Nyaa's own fields; Sukebei (Kura's 18+ side) names its namespace after itself
    ns = NYAA if urllib.parse.urlparse(url).netloc != "sukebei.nyaa.si" else "{https://sukebei.nyaa.si/xmlns/nyaa}"
    for it in root.iter("item"):
        def get(tag, ns=""):
            return (it.findtext(ns + tag) or "").strip()

        def num(tag):
            try:
                return int(get(tag, ns) or 0)
            except ValueError:
                return 0
        items.append({
            "title": get("title"), "link": get("link"), "page": get("guid") or get("link"),
            "date": get("pubDate"), "size": get("size", ns), "hash": get("infoHash", ns).lower(),
            "seeders": num("seeders"), "leechers": num("leechers"), "downloads": num("downloads"),
            "trusted": get("trusted", ns) == "Yes", "remake": get("remake", ns) == "Yes",
            "category": get("categoryId", ns),
        })
    return items


def is_nyaa(url):
    return urllib.parse.urlparse(url).netloc.endswith("nyaa.si")


def fetch_nyaa_page(rss_url, page=1):
    """Nyaa's RSS stops at 75 results and ignores page numbers, so searches read the
    normal results pages instead (same data). Returns (items, page, last page)."""
    parts = urllib.parse.urlparse(rss_url)
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query) if k not in ("page", "p")]
    if page > 1:
        query.append(("p", str(page)))
    url = urllib.parse.urlunparse(parts._replace(query=urllib.parse.urlencode(query)))
    req = urllib.request.Request(url, headers={"User-Agent": org.USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        text = resp.read().decode("utf-8", "replace")
    host = parts.netloc or "nyaa.si"   # nyaa.si or sukebei.nyaa.si (Kura's 18+ side): links go to the same site
    body = text.split("<tbody>", 1)[-1].split("</tbody>", 1)[0] if "<tbody>" in text else ""
    items = []
    for row in body.split("<tr")[1:]:
        view = re.findall(r'<a href="/view/(\d+)" title="([^"]*)"', row)
        if not view:
            continue
        tid, title = view[-1]
        mag = re.search(r"magnet:\?xt=urn:btih:([0-9a-fA-F]{40})", row)
        cells = re.findall(r'<td class="text-center"[^>]*>([^<]*)</td>', row)
        stamp = re.search(r'data-timestamp="(\d+)"', row)
        cls = re.match(r'\s*class="(\w+)"', row)

        def num(i):
            try:
                return int(cells[i])
            except (IndexError, ValueError):
                return 0
        items.append({
            "title": html.unescape(title), "link": f"https://{host}/download/{tid}.torrent",
            "page": f"https://{host}/view/{tid}",
            "date": email.utils.formatdate(int(stamp.group(1))) if stamp else "",
            "size": cells[0] if cells else "", "hash": mag.group(1).lower() if mag else "",
            "seeders": num(2), "leechers": num(3), "downloads": num(4),
            "trusted": bool(cls and cls.group(1) == "success"), "remake": bool(cls and cls.group(1) == "danger"),
        })
    pager = text.split('class="pagination"', 1)[-1].split("</ul>", 1)[0] if 'class="pagination"' in text else ""
    last = max([page] + [int(n) for n in re.findall(r"[?&;]p=(\d+)", pager)])
    return items, page, last


SEASON_TAIL = re.compile(r"[\s:\-–]*\b(?:(?:season|part|cour)\s*\d+|\d+(?:st|nd|rd|th)\s+(?:season|cour)|s\d+|"
                         r"II|III|IV|VI)\b.*$", re.IGNORECASE)


def search_name(title):
    """A show's name the way you'd search for its releases: without "2nd Season" / "Season 2" / "Part 2" (groups
    write S2, Season 2, 2nd Season... and the matcher works out the season of each release), at most 8 words."""
    # symbols like ★ ☆ ♪ ♥ (AniList's "Magical★Explorer") match almost nothing on Nyaa: spaces instead
    t = " ".join(re.sub(r"[^\w\s'’:;!?.,&()\-–]", " ", title or "").split())
    base = SEASON_TAIL.sub("", t).strip(" :-–")
    if len(base) >= 3:
        t = base
    if len(t.split()) > 8 and ":" in t:   # a long light-novel title: the part before the colon
        t = t.split(":")[0].strip()
    return " ".join(t.split()[:8])


def search_pages(urls, page=0):
    """Several searches at once (every name of a show), one results page each, merged: newest first, each release
    once, `found` = the indexes of the searches that found it. Returns (items, last page)."""
    from concurrent.futures import ThreadPoolExecutor

    def one(url):
        if page:
            items, _, last = fetch_nyaa_page(url, page)
            return items, last
        return fetch(url), 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(one, urls))
    merged, by_key = [], {}
    for n, (items, _) in enumerate(results):
        for it in items:
            key = it.get("hash") or it.get("page") or it.get("title")
            if key in by_key:
                by_key[key]["found"].append(n)
                continue
            it["found"] = [n]
            by_key[key] = it
            merged.append(it)

    def stamp(it):
        try:
            return email.utils.parsedate_to_datetime(it["date"]).timestamp()
        except (TypeError, ValueError):
            return 0
    merged.sort(key=stamp, reverse=True)
    return merged, max([last for _, last in results] + [0])


# ---------------------------------------------------------------- recognising releases

def norm(title):
    """Loose form of a title for matching: lowercase letters and digits only."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).split())


def _ints(value):
    vals = value if isinstance(value, list) else [value]
    out = []
    for v in vals:
        try:
            out.append(int(str(v)))
        except (TypeError, ValueError):
            return []
    return out


def parse_release(title):
    """What a release's name says (show, season, episodes, group…). Remembered per name: the feed is filtered
    again on every visit and anitopy was most of that time."""
    hit = _PARSED.get(title)
    if hit is None:
        if len(_PARSED) > 8000:
            _PARSED.clear()
        hit = _PARSED[title] = _parse_release(title)
    return {k: (list(v) if isinstance(v, list) else v) for k, v in hit.items()}


_PARSED = {}


def _parse_release(title):
    info = org.anitopy_parse(title)   # never raises (one odd title used to stop a whole results page)
    name = info.get("anime_title") or ""
    if isinstance(name, list):
        name = name[0]
    season = (_ints(info.get("anime_season")) or [None])[0]
    eps = _ints(info.get("episode_number"))
    if len(eps) == 2 and isinstance(info.get("episode_number"), list) and eps[1] > eps[0]:
        eps = list(range(eps[0], eps[1] + 1))
    rel = info.get("release_information") or []
    rel = [rel] if isinstance(rel, str) else rel
    batch = len(eps) > 1 or any("batch" in r.lower() or "complete" in r.lower() for r in rel)
    res = str(info.get("video_resolution") or "")
    m = re.search(r"(\d{3,4})[pP]?$", res.split("x")[-1]) if res else None
    res = f"{m.group(1)}p" if m else res
    group = info.get("release_group") or ""
    if isinstance(group, list):
        group = group[0]
    version = (_ints(info.get("release_version")) or [1])[0]
    term = info.get("video_term") or ""
    term = " ".join(term) if isinstance(term, list) else str(term)
    return {"name": name, "season": season, "episodes": eps, "batch": batch, "resolution": res, "group": group,
            "version": version, "video_term": term}


_FOUND = {}   # (normalised title, matcher's names) → AniList id: kept across matchers (made on every refilter)


class Matcher:
    """Turns a parsed release (title + season + episode) into an AniList id and episode.

    names:   normalised title -> AniList id (your list, library folders, cached titles)
    seasons: AniList id -> the show's TV seasons in order (to handle "S2" and continuous numbering)
    totals:  AniList id -> episode count
    """

    def __init__(self, names, seasons, totals, close=None):
        """close: AniList ids worth a near match (shows you watch, plan or have): groups spell titles their own
        way ("Daiseijo" for AniList's "Dai Seijo", "Hitakakusu" for "Hita Kakusu")."""
        self.names, self.seasons, self.totals = names, seasons, totals
        self.compact = {}
        for k, mid in names.items():
            self.compact.setdefault(k.replace(" ", ""), mid)
        close = set(close or ())
        self.near = [(k.replace(" ", ""), mid) for k, mid in names.items() if mid in close]
        self.found = {}
        self.sig = hash((frozenset(names.items()), tuple(self.near)))

    def lookup(self, key):
        """An AniList id for a normalised title: as written, without spaces, or nearly the same (one of the shows
        in `close`, with exactly the same numbers in it, so "Season 2" never becomes "Season 3")."""
        mid = self.names.get(key)
        if mid is not None or not key:
            return mid
        if key in self.found:
            return self.found[key]
        sig = (key, self.sig)   # the same names as a matcher before: its answer (difflib was the slow part)
        if sig in _FOUND:
            self.found[key] = _FOUND[sig]
            return _FOUND[sig]
        flat = key.replace(" ", "")
        mid = self.compact.get(flat)
        if mid is None and len(flat) >= 12 and self.near:
            import difflib
            digits = re.findall(r"\d+", key)
            best, score = None, 0.92
            for k, m in self.near:
                if abs(len(k) - len(flat)) > len(flat) * 0.1 or re.findall(r"\d+", k) != digits:
                    continue
                r = difflib.SequenceMatcher(None, flat, k).ratio()
                if r >= score:
                    best, score = m, r
            mid = best
        self.found[key] = mid
        if len(_FOUND) > 20000:
            _FOUND.clear()
        _FOUND[sig] = mid
        return mid

    def match(self, name, season, episode):
        key = norm(name)
        mid = self.lookup(key)
        if mid is None:   # "Title Season 2" / "Title 2nd Season" / "Title S2" spelled out in the name
            m = re.match(r"(.+?) (?:season (\d+)|(\d+)(?:st|nd|rd|th) season|s(\d+))$", key)
            if m:
                mid = self.lookup(m.group(1))
                season = season or int(next(g for g in m.groups()[1:] if g))
        if mid is None:
            return None, episode
        chain = self.seasons.get(mid) or [mid]
        # "Title S2 - 05" where the name matched season 1: move to the second season.
        # If that season isn't known, don't guess (it would count as a season 1 episode).
        if season and season > 1 and chain[0] == mid:
            if season > len(chain) or not chain[season - 1]:
                return None, episode
            mid = chain[season - 1]
        # Continuous numbering: episode 27 of a 26-episode season is the next season's episode 1
        if episode and mid in chain:
            i = chain.index(mid)
            while i < len(chain) - 1 and self.totals.get(chain[i]) and episode > self.totals[chain[i]]:
                episode -= self.totals[chain[i]]
                i += 1
                mid = chain[i]
        return mid, episode


# ---------------------------------------------------------------- choosing what to download

# Like Taiga: every release gets a set of facts; filters test those facts and
# discard, select or prefer releases. Selected releases are the ones downloaded.

STATUS_TEXT = {"CURRENT": "Watching", "REPEATING": "Rewatching", "PLANNING": "Planning", "COMPLETED": "Completed",
               "PAUSED": "Paused", "DROPPED": "Dropped", None: "Not in list"}
AIRING_TEXT = {"RELEASING": "Airing", "FINISHED": "Finished", "NOT_YET_RELEASED": "Not yet aired",
               "CANCELLED": "Canceled", "HIATUS": "On hiatus"}
YES_NO = ["Yes", "No"]

# element -> (label, kind, suggested values)
ELEMENTS = {
    "anime_title":        ("Anime title", "text", []),
    "my_status":          ("My status", "choice", list(STATUS_TEXT.values())),
    "airing_status":      ("Airing status", "choice", list(AIRING_TEXT.values())),
    "in_library":         ("Anime is in my library", "bool", YES_NO),
    "episode_number":     ("Episode number", "number", []),
    "episode_version":    ("Episode version", "number", ["1", "2", "3"]),
    "episode_watched":    ("Episode is watched", "bool", YES_NO),
    "episode_available":  ("Episode is in my library", "bool", YES_NO),
    "episode_downloaded": ("Episode was downloaded before", "bool", YES_NO),
    "release_group":      ("Fansub group", "text", ["SubsPlease", "Erai-raws", "EMBER", "Yameii", "ToonsHub"]),
    "resolution":         ("Video resolution", "text", ["2160p", "1080p", "720p", "480p"]),
    "video_term":         ("Video codec / keywords", "text", ["HEVC", "x265", "x264", "AV1"]),
    "file_title":         ("File name", "text", []),
    "file_size":          ("File size (MiB)", "number", []),
    "seeders":            ("Seeders", "number", []),
    "trusted":            ("Trusted uploader", "bool", YES_NO),
    "batch":              ("Batch / season pack", "bool", YES_NO),
}
OPERATORS = {
    "text":   [("is", "is"), ("is_not", "is not"), ("contains", "contains"), ("not_contains", "does not contain"),
               ("begins", "begins with"), ("ends", "ends with")],
    "number": [("eq", "="), ("ne", "≠"), ("gt", ">"), ("ge", "≥"), ("lt", "<"), ("le", "≤")],
    "bool":   [("is", "is")],
    "choice": [("is", "is"), ("is_not", "is not")],
}
ACTIONS = {"discard": "Discard matched items", "select": "Select matched items",
           "prefer": "Prefer matched items to similar ones"}
DISCARD_TYPES = {"default": "Default (show, unticked)", "hide": "Hide from the list"}


def _f(name, action, conditions, match="all", discard="default", anime=None, enabled=True, anime_mode="only"):
    """anime + anime_mode: "only" = just these anime, "except" = every anime but these."""
    return {"name": name, "enabled": enabled, "action": action, "match": match, "discard": discard,
            "anime": anime or [], "anime_mode": anime_mode,
            "conditions": [{"element": e, "op": o, "value": v} for e, o, v in conditions]}


def default_filters(quality="1080p"):
    """Taiga's defaults, adapted: hide what isn't yours, drop what you have or watched,
    pick new episodes of what you watch, prefer good releases."""
    filters = [
        _f("Discard anime not in my list or library", "discard",
           [("my_status", "is", "Not in list"), ("in_library", "is", "No")], discard="hide"),
        _f("Discard watched and available episodes", "discard",
           [("episode_watched", "is", "Yes"), ("episode_available", "is", "Yes"), ("episode_downloaded", "is", "Yes")],
           match="any"),
        _f("Discard batches", "discard", [("batch", "is", "Yes")]),
        _f("Select currently watching", "select",
           [("my_status", "is", "Watching"), ("my_status", "is", "Rewatching")], match="any"),
        _f(f"Prefer {quality}" if quality and quality != "Any" else "Prefer 1080p", "prefer",
           [("resolution", "is", quality if quality and quality != "Any" else "1080p")],
           enabled=bool(quality and quality != "Any")),
        _f("Prefer new versions", "prefer", [("episode_version", "gt", "1")]),
    ]
    return filters


PRESETS = [  # (title, description, filter)
    ("(Custom)", "Create a filter from scratch", _f("New filter", "discard", [("release_group", "is", "")])),
    ("[Fansub] Anime", "Prefer one fansub group for one or more anime",
     _f("[Fansub] Anime", "prefer", [("release_group", "is", "SubsPlease")])),
    ("Discard bad video keywords", "Discards everything that is AVI, DIVX, LQ, RMVB, SD, WMV or XVID",
     _f("Discard bad video keywords", "discard",
        [("file_title", "contains", k) for k in ("AVI", "DIVX", "LQ", "RMVB", "SD", "WMV", "XVID")], match="any")),
    ("Prefer new versions", "Prefers v2 files and above when there are earlier releases of the same episode",
     _f("Prefer new versions", "prefer", [("episode_version", "gt", "1")])),
    ("Prefer a resolution", "Prefers one quality when an episode is out in several",
     _f("Prefer 1080p", "prefer", [("resolution", "is", "1080p")])),
    ("Select shows I plan to watch", "Also downloads new episodes of shows on your Planning list",
     _f("Select planning", "select", [("my_status", "is", "Planning")])),
    ("Discard a fansub group", "Never download releases from a group",
     _f("Discard [Group]", "discard", [("release_group", "is", "")])),
    ("Only one fansub group", "Skip every other group (they stay visible, unticked, so you can still pick one)",
     _f("Only SubsPlease", "discard", [("release_group", "is_not", "SubsPlease")], anime_mode="except")),
]


# ---- profiles: named filter sets, one active; the file also keeps a copy of the active
# profile under "filters" (the format before profiles existed)

def load_profiles():
    """(filters enabled?, active profile name, {name: [filters]}); None if never saved."""
    try:
        data = json.loads(FILTERS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    profiles = data.get("profiles") or {"Default": data.get("filters") or []}
    active = data.get("active") if data.get("active") in profiles else next(iter(profiles))
    return data.get("enabled", True), active, profiles


def save_profiles(enabled, active, profiles):
    data = {"enabled": enabled, "active": active, "profiles": profiles, "filters": profiles[active]}
    tmp = FILTERS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, FILTERS_FILE)


def load_filters():
    """(filters enabled?, [filters of the active profile]); None if the user never saved any."""
    p = load_profiles()
    return None if p is None else (p[0], p[2][p[1]])


def save_filters(enabled, filters):
    """Save into the active profile."""
    p = load_profiles()
    if p is None:
        save_profiles(enabled, "Default", {"Default": filters})
    else:
        p[2][p[1]] = filters
        save_profiles(enabled, p[1], p[2])


def unique_name(name, taken):
    name = (name or "").strip() or "Imported filters"
    if name not in taken:
        return name
    n = 2
    while f"{name} ({n})" in taken:
        n += 1
    return f"{name} ({n})"


# ---- sharing: a profile as a small JSON document

EXPORT_FORMAT = 1


def export_profile(name, filters, titles):
    """titles: {anime id: title}, so whoever imports it can see which shows a filter is about."""
    ids = {i for f in filters for i in f.get("anime") or []}
    return {"kura_filters": EXPORT_FORMAT, "name": name, "filters": filters,
            "anime": {str(i): titles.get(i, "") for i in sorted(ids)}}


def import_profile(text):
    """Parse a shared profile (file contents or pasted text). Returns (name, filters, notes);
    anything unknown is dropped and described in notes. Raises ValueError if it isn't one."""
    try:
        data = json.loads(text.strip())
    except ValueError:
        raise ValueError("That isn't a Kura filter profile (not valid JSON).")
    if not isinstance(data, dict) or "kura_filters" not in data or not isinstance(data.get("filters"), list):
        raise ValueError("That isn't a Kura filter profile.")
    if not isinstance(data["kura_filters"], int):
        raise ValueError("That isn't a Kura filter profile.")
    if data["kura_filters"] > EXPORT_FORMAT:
        raise ValueError("This profile was made by a newer version of Kura.")
    notes, filters = [], []
    for raw in data["filters"]:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "Untitled filter")[:120]
        action = raw.get("action")
        if action not in ACTIONS:
            notes.append(f"Skipped “{name}”: unknown action {action!r}")
            continue
        conds = []
        for c in raw.get("conditions") or []:
            el = (c or {}).get("element")
            if el not in ELEMENTS:
                notes.append(f"“{name}”: dropped unknown condition {el!r}")
                continue
            ops = [k for k, _ in OPERATORS[ELEMENTS[el][1]]]
            conds.append({"element": el, "op": c.get("op") if c.get("op") in ops else ops[0],
                          "value": str(c.get("value", ""))[:200]})
        anime = [int(a) for a in raw.get("anime") or [] if str(a).isdigit()]
        filters.append({
            "name": name, "enabled": bool(raw.get("enabled", True)), "action": action,
            "match": raw.get("match") if raw.get("match") in ("all", "any") else "all",
            "discard": raw.get("discard") if raw.get("discard") in DISCARD_TYPES else "default",
            "anime": anime, "anime_mode": "except" if raw.get("anime_mode") == "except" else "only",
            "conditions": conds,
        })
    if not filters:
        raise ValueError("That profile has no filters Kura can use.")
    return str(data.get("name") or "Imported filters")[:80], filters, notes


def _size_mib(text):
    m = re.match(r"([\d.]+)\s*([KMGT]i?B)", text or "")
    if not m:
        return 0.0
    return float(m.group(1)) * {"K": 1 / 1024, "M": 1, "G": 1024, "T": 1024 * 1024}[m.group(2)[0]]


def condition_true(cond, facts):
    kind = ELEMENTS.get(cond["element"], ("", "text", []))[1]
    value, target, op = facts.get(cond["element"]), str(cond.get("value", "")), cond.get("op", "is")
    if kind == "bool":
        return bool(value) == (target.strip().lower() in ("yes", "true", "1"))
    if kind == "number":
        try:
            a, b = float(value or 0), float(target)
        except ValueError:
            return False
        return {"eq": a == b, "ne": a != b, "gt": a > b, "ge": a >= b, "lt": a < b, "le": a <= b}.get(op, False)
    a, b = str(value or "").lower(), target.strip().lower()
    return {"is": a == b, "is_not": a != b, "contains": b in a, "not_contains": b not in a,
            "begins": a.startswith(b), "ends": a.endswith(b)}.get(op, False)


def filter_matches(flt, it):
    if flt.get("anime"):
        listed = it.get("mid") in flt["anime"]
        if flt.get("anime_mode", "only") == "except" and listed:
            return False   # "all anime except these"
        if flt.get("anime_mode", "only") == "only" and not listed:
            return False
    results = [condition_true(c, it["facts"]) for c in flt.get("conditions") or []]
    if not results:
        return True   # no conditions: everything (of the chosen anime)
    return all(results) if flt.get("match", "all") == "all" else any(results)


def evaluate(items, matcher, describe, history, filters, enabled=True, tracking=True):
    """Match every release to a show, work out its facts and run the filters.

    describe(mid) -> {"title", "status", "progress", "airing", "in_library", "local"}
    tracking:     False when there's no AniList account; shows in your library then count
                  as Watching, and "watched" means older than what you have on disk.
    Sets on each item: mid, episode, facts, state (selected / discarded / downloaded /
    neutral), hidden, reason (the filter that decided), selected.
    """
    got = {(h.get("mid"), h.get("episode")) for h in history.values() if h.get("mid")}
    for it in items:
        p = parse_release(it["title"])
        ep = p["episodes"][-1] if p["episodes"] else None
        mid, ep = matcher.match(p["name"], p["season"], ep)
        d = describe(mid) if mid else {}
        local = d.get("local") or set()
        status = d.get("status")
        progress = d.get("progress") or 0
        if not tracking and d.get("in_library"):
            status, progress = "CURRENT", max(local, default=0)
        it.update(parsed=p, mid=mid, episode=ep, state="neutral", hidden=False, reason="", selected=False)
        it["facts"] = {
            "anime_title": d.get("title") or p["name"], "my_status": STATUS_TEXT.get(status, "Not in list"),
            "airing_status": AIRING_TEXT.get(d.get("airing"), ""), "in_library": bool(d.get("in_library")),
            "episode_number": ep or 0, "episode_version": p["version"],
            "episode_watched": ep is not None and ep <= progress and status is not None,
            "episode_available": ep is not None and ep in local,
            "episode_downloaded": (mid, ep) in got and mid is not None,
            "release_group": p["group"], "resolution": p["resolution"], "video_term": p["video_term"],
            "file_title": it["title"], "file_size": _size_mib(it.get("size")), "seeders": it.get("seeders", 0),
            "trusted": it.get("trusted", False), "batch": p["batch"] or ep is None,
        }
        if it["hash"] and it["hash"] in history:
            it.update(state="downloaded", reason="In your download history")

    active = [f for f in filters if f.get("enabled", True)] if enabled else []
    # 1. Discard filters win over everything
    for f in (f for f in active if f["action"] == "discard"):
        for it in items:
            if it["state"] == "neutral" and filter_matches(f, it):
                it.update(state="discarded", reason=f["name"], hidden=f.get("discard") == "hide")
    # 2. Select filters pick what's left
    for f in (f for f in active if f["action"] == "select"):
        for it in items:
            if it["state"] == "neutral" and filter_matches(f, it):
                it.update(state="selected", reason=f["name"])
    # 3. Prefer filters, in order: among releases of the same episode, drop the ones that don't match
    def key(it):
        return (it["mid"] or norm(it["parsed"]["name"]), it["episode"])
    for f in (f for f in active if f["action"] == "prefer"):
        groups = {}
        for it in items:
            if it["state"] in ("neutral", "selected") and it["episode"] is not None:
                groups.setdefault(key(it), []).append(it)
        for rels in groups.values():
            hits = [r for r in rels if filter_matches(f, r)]
            if hits and len(hits) < len(rels):
                for r in rels:
                    if r not in hits:
                        r.update(state="discarded", reason=f"Not preferred: {f['name']}")
    # 4. Still several releases of one episode selected: keep trusted, then most seeded
    groups = {}
    for it in items:
        if it["state"] == "selected":
            groups.setdefault(key(it), []).append(it)
    for rels in groups.values():
        rels.sort(key=lambda r: (not r.get("trusted"), -r.get("seeders", 0)))
        for r in rels[1:]:
            r.update(state="discarded", reason="Duplicate release")
    for it in items:
        it["selected"] = it["state"] == "selected"
    return items


# ---------------------------------------------------------------- history & sending

def load_history():
    try:
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def update_history(h, **fields):
    history = load_history()
    if h in history:
        history[h].update(fields)
        tmp = HISTORY_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(history, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, HISTORY_FILE)


def add_history(items):
    history = load_history()
    for it in items:
        if it["hash"]:
            batch = (it.get("parsed") or {}).get("batch")
            history[it["hash"]] = {"title": it["title"], "mid": it.get("mid"),
                                   "episode": None if batch else it.get("episode"),
                                   "episodes": it.get("batch_eps"), "only_missing": it.get("only_missing", False),
                                   "skip_junk": it.get("skip_junk", False),
                                   "t": time.time()}
    tmp = HISTORY_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(history, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, HISTORY_FILE)
    return history


def magnet(item):
    if not item["hash"]:
        return item["link"]   # plain RSS feeds: the .torrent link itself
    trackers = "".join(f"&tr={urllib.parse.quote(t, safe='')}" for t in TRACKERS)
    return f"magnet:?xt=urn:btih:{item['hash']}&dn={urllib.parse.quote(item['title'])}{trackers}"


def find_qbittorrent():
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
        exe = Path(base) / "qBittorrent" / "qbittorrent.exe"
        if exe.exists():
            return str(exe)
    return ""


class QBitError(Exception):
    pass


class QBitOffline(QBitError):
    """The Web UI didn't answer at all."""


class QBit:
    """qBittorrent's Web API (Options → Web UI must be on). Torrents Kura sends get the
    "Kura" category, so Kura only ever touches its own torrents."""
    CATEGORY = "Kura"
    DONE_STATES = {"uploading", "stalledUP", "pausedUP", "stoppedUP", "queuedUP", "forcedUP", "checkingUP"}

    def __init__(self, url, user="", password=""):
        import http.cookiejar
        self.url = (url or "").strip().rstrip("/")
        if self.url and "://" not in self.url:
            self.url = "http://" + self.url
        # "localhost" tries IPv6 first; Windows takes ~2 s to give up on it for every call
        self.url = re.sub(r"^(https?://)localhost\b", r"\g<1>127.0.0.1", self.url, flags=re.I)
        self.user, self.password = user, password
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.logged_in = False

    def _call(self, path, data=None, timeout=8):
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        # qBittorrent rejects API calls whose Referer/Origin doesn't match its own address
        req = urllib.request.Request(self.url + "/api/v2/" + path, data=body,
                                     headers={"Referer": self.url, "Origin": self.url, "User-Agent": org.USER_AGENT})
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 403:
                raise PermissionError("qBittorrent refused the login") from e
            if e.code == 404:
                raise FileNotFoundError(path) from e
            raise QBitError(f"qBittorrent answered {e.code}") from e
        except (urllib.error.URLError, OSError) as e:
            raise QBitOffline(f"Can't reach qBittorrent's Web UI at {self.url}") from e

    def call(self, path, data=None):
        """An API call; logs in first (or again after qBittorrent forgot the session)."""
        if not self.logged_in:
            self.login()
        try:
            return self._call(path, data)
        except PermissionError:
            self.login()
            return self._call(path, data)

    def login(self):
        if not self.url:
            raise QBitError("No qBittorrent Web UI address set")
        try:   # no login needed when "Bypass authentication for clients on localhost" is on
            self._call("app/version")
            self.logged_in = True
            return
        except PermissionError:
            pass
        answer = self._call("auth/login", {"username": self.user, "password": self.password})
        if answer.strip() != "Ok.":
            raise QBitError("qBittorrent didn't accept the user name or password")
        self.logged_in = True

    def version(self):
        return self.call("app/version").strip()

    def torrents(self, category=CATEGORY):
        return json.loads(self.call("torrents/info", {"category": category} if category is not None else {}) or "[]")

    def ensure_category(self):
        try:
            self.call("torrents/createCategory", {"category": self.CATEGORY, "savePath": ""})
        except QBitOffline:
            raise
        except QBitError:
            pass   # 409: it exists already

    def add(self, urls, save_path=""):
        self.ensure_category()
        data = {"urls": "\n".join(urls), "category": self.CATEGORY}
        if save_path:
            data["savepath"] = save_path
        if self.call("torrents/add", data).strip() == "Fails.":
            raise QBitError("qBittorrent couldn't add the torrent")

    def _either(self, new, old, hashes):
        """qBittorrent 5 renamed pause/resume to stop/start."""
        data = {"hashes": "|".join(hashes)}
        try:
            self.call("torrents/" + new, data)
        except FileNotFoundError:
            self.call("torrents/" + old, data)

    def pause(self, hashes):
        self._either("stop", "pause", hashes)

    def resume(self, hashes):
        self._either("start", "resume", hashes)

    def delete(self, hashes, delete_files=False):
        self.call("torrents/delete", {"hashes": "|".join(hashes), "deleteFiles": "true" if delete_files else "false"})

    def preferences(self):
        return json.loads(self.call("app/preferences") or "{}")

    def files(self, h):
        """The torrent's files: name (path inside the save folder), index, priority, progress."""
        files = json.loads(self.call("torrents/files", {"hash": h}) or "[]")
        for i, f in enumerate(files):
            f.setdefault("index", i)
        return files

    def file_priority(self, h, indexes, priority):
        """0 = don't download, 1 = normal."""
        if indexes:
            self.call("torrents/filePrio", {"hash": h, "id": "|".join(str(i) for i in indexes),
                                             "priority": str(priority)})


def is_done(t):
    return t.get("progress", 0) >= 1 or t.get("state") in QBit.DONE_STATES


def send(items, client="", save_path="", category=""):
    """Hand torrents to the client. qBittorrent gets them silently (no "add torrent"
    dialog) and saves them to save_path; otherwise Windows opens each magnet link."""
    targets = [magnet(i) for i in items]
    if not targets:
        return
    if client and Path(client).exists():
        args = [client, "--skip-dialog=true"]
        if save_path:
            args.append(f"--save-path={save_path}")
        if category:
            args.append(f"--category={category}")
        subprocess.Popen(args + targets, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    elif os.name == "nt":
        for t in targets:
            os.startfile(t)
