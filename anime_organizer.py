"""
Anime Organizer  (Sort V10)
===========================
Sorts anime episodes into  Show Name / Season N  folders, gives each show folder
an AniList cover as its Windows folder icon, and refreshes just those folders so
the icons show up right away (no Explorer restart, no reboot).

How it works for each video file:
  1. Parse the filename with anitopy (the same parser Taiga uses)
  2. Look the title up on AniList to get one official name per show
  3. Move it into  Show / Season N  (or OVAs / Specials / Movies / Extras)
  4. Download the cover, turn it into an .ico, write desktop.ini
  5. Tell Windows to refresh that folder

Setup (once):
    pip install anitopy pillow
    (If anitopy fails to install, run  pip install --upgrade pip setuptools  and try again.)

Ways to run it:
    Double-click / run inside a folder   -> sorts the videos in that folder (like V9)
    python anime_organizer.py "D:\\Downloads\\some file.mkv"
    python anime_organizer.py --icons    -> adds missing icons to every show folder
    python anime_organizer.py --rename   -> renames existing show folders to English (or romaji)
    python anime_organizer.py --link     -> puts spin-offs / side stories inside their parent shows
    python anime_organizer.py --dry-run  -> shows what would happen, changes nothing

qBittorrent (Tools > Options > Downloads > Run external program > "Run on torrent finished"):
    pyw "C:\\path\\to\\anime_organizer.py" "%F"
"""

import argparse
import ctypes
import io
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

# ============================== SETTINGS ==============================

# Where sorted shows go. None = the folder the video is already in
# (same as V9). Set this when running from qBittorrent, e.g. r"D:\Anime"
LIBRARY_DIR = None

# Only touch downloads inside this folder (handy so non-anime torrents are ignored).
# Point Taiga's download folder here. None = process anything it's given.
ONLY_PROCESS_INSIDE = None

# "move"     = move the file (like V9; qBittorrent stops seeding it)
# "hardlink" = file appears in both places, takes no extra space, keeps seeding
#              (download folder and library must be on the same drive)
# "copy"     = full copy
MODE = "move"

# Folder names: "english" (Frieren - Beyond Journey's End) or "romaji" (Sousou no Frieren).
# Shows with no English title on AniList use the romaji one.
# Run with --rename to switch your existing folders over.
TITLE_LANGUAGE = "english"

MAKE_ICONS = True

# Give Season 1 / Season 2 / OVAs / Specials / Movies folders their own covers
SEASON_ICONS = True

# Which AniList entry a season folder is. Key = show folder name, lowercase. Value = {season number: AniList ID}.
# Filled from user_overrides.json (Kura: a show page's Edit info), not by editing this file.
SEASON_COVERS = {}

# Put a junction (a folder that opens the real one) to each spin-off / side story
# inside its parent show, e.g.  Parent / Spin Offs / Spinoff Name.
# The real folder stays at the top level so Jellyfin lists it as its own show,
# and a hidden .ignore file stops Jellyfin from counting the junction too.
LINK_RELATED = False   # Kura's show pages list relations; --link still works when asked for
ADULT_LIBRARY_DIR = None   # 18+ shows (AniList isAdult) go here instead of the library (Kura: Settings → Adult content)
CUSTOM_ICONS = set()   # show folders (normcase) whose icon is a screenshot you picked in Kura: "redo all" keeps it
# How new season folders are named: "number" (Season 2), "number_name" (Season 2 - The Evil Eye),
# or "name" (The Evil Eye). Jellyfin needs the number to know which season a folder is.
SEASON_FOLDER_STYLE = "number_name"

SPINOFF_FOLDER = "Spin Offs"
SIDE_STORY_FOLDER = "Side Stories"

# When AniList doesn't link them (or links them wrong). Key = the spin-off's folder
# name, lowercase. Value = ("Parent folder name", "spinoff" or "side story"),
# or None to never link that show.
RELATED_OVERRIDES = {
    # "attack on titan - junior high": ("Attack on Titan", "spinoff"),
}

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".m4v", ".webm"}
SIDECAR_EXTS = {".ass", ".ssa", ".srt", ".vtt", ".sub", ".idx"}  # moved along with the video

# Fix a wrong match. Key = a title (a show folder's name, or one parsed from a file name), lowercase. Value = AniList
# ID, or {"id": ..., "season": N} to also force the season folder. Filled from user_overrides.json (Kura: a show
# page's Edit info → change the match), not by editing this file.
OVERRIDES = {}
FOLDER_SEASONS = {}   # {show folder name, lowercase: {subfolder name: season number}} ("counts as Season 2")
DISPLAY_TITLES = {}   # {AniList ID as text: the title Kura shows}
EPISODE_SPLIT = {}    # {AniList ID as text: AniList episodes per file} (two-in-one episodes)

# ======================================================================

# Data files live next to the script, or next to Kura.exe when running as the app
SCRIPT_DIR = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
              else Path(__file__).resolve().parent)
CACHE_FILE = SCRIPT_DIR / "anilist_cache.json"
LIST_FILE = SCRIPT_DIR / "anilist_list.json"   # your AniList list, saved by the app when you connect
LOG_FILE = SCRIPT_DIR / "anime_organizer.log"
USER_OVERRIDES_FILE = SCRIPT_DIR / "user_overrides.json"   # your own fixes (Kura: Edit info on a show page)
IS_WINDOWS = os.name == "nt"
USER_AGENT = "AnimeOrganizer/1.0"


def load_user_overrides():
    """Your fixes from user_overrides.json into OVERRIDES / SEASON_COVERS / FOLDER_SEASONS / DISPLAY_TITLES /
    EPISODE_SPLIT (kept as the same dict objects, so every module that holds them sees the change). The file:
    {"titles": {folder or title, lowercase: AniList id}, "seasons": {show folder, lowercase: {"2": id}},
     "folder_seasons": {show folder, lowercase: {subfolder: n}}, "display": {"id": title}, "split": {"id": 2}}."""
    try:
        data = json.loads(USER_OVERRIDES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    OVERRIDES.clear()
    OVERRIDES.update({k.lower(): v for k, v in (data.get("titles") or {}).items()})
    SEASON_COVERS.clear()
    SEASON_COVERS.update({k.lower(): {int(n): i for n, i in v.items()} for k, v in (data.get("seasons") or {}).items()})
    FOLDER_SEASONS.clear()
    FOLDER_SEASONS.update({k.lower(): dict(v) for k, v in (data.get("folder_seasons") or {}).items()})
    DISPLAY_TITLES.clear()
    DISPLAY_TITLES.update(data.get("display") or {})
    EPISODE_SPLIT.clear()
    EPISODE_SPLIT.update({k: int(v) for k, v in (data.get("split") or {}).items() if int(v) > 1})
    return data


def save_user_overrides(data):
    """Write the fixes (empty parts dropped) and load them again."""
    data = {k: v for k, v in data.items() if v}
    tmp = USER_OVERRIDES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, USER_OVERRIDES_FILE)
    return load_user_overrides()


load_user_overrides()

log = logging.getLogger("organizer")

try:
    import anitopy
except ImportError:
    sys.exit("Missing package: run  pip install anitopy pillow")


# ---------------------------------------------------------------- parsing

SLUG = re.compile(r"(?i)^[a-z0-9]+(?:-[a-z0-9]+){3,}$")
SXXEXX = re.compile(r"(?i)(?:^|[\s._\-\[])S\d{1,2}[\s._]?E\d{1,4}(?:v\d)?(?=$|[\s._\-\]\[])")


def _anitopy_safe(text):
    t = text
    for _ in range(3):
        try:
            return anitopy.parse(t) or {}
        except Exception:
            t = t.rsplit(" ", 1)[0] if " " in t.strip() else ""
            if not t:
                break
    return {}


def anitopy_parse(text):
    """anitopy.parse that can't crash: it raises on some names ending in a keyword with nothing after it
    ("[K-F] One Piece Parody Episodes"); then the last word is dropped and it tries again.
    It also misses "S01E07" when the episode title right after it starts with a single letter
    ("Dara.san.of.the.Reiwa.Era.S01E07.A.Picture.Show..." → no episode, sorted as a movie into a show folder
    of its own); then the tag is set apart from the episode title ("S01E07 - A.Picture...") and it's parsed again.
    Names that are one dash-joined slug ("fushigi-no-kuni-no-succubus-1-1080p-h1x.mp4", from some sites) are read
    with spaces: anitopy took the whole slug as the title, no AniList match, no icon (sorted as a show of its own)."""
    stem, dot, ext = text.rpartition(".") if re.search(r"\.\w{2,4}$", text) else (text, "", "")
    if SLUG.match(stem):
        text = stem.replace("-", " ") + dot + ext
    info = _anitopy_safe(text)
    if not info.get("episode_number"):
        m = SXXEXX.search(text)
        if m:
            fixed = _anitopy_safe(text[:m.end()] + " - " + text[m.end():].lstrip("._ -"))
            if fixed.get("episode_number"):
                return fixed
    return info


def parse(filename):
    """Return (title, season, types, episode) from a release filename."""
    info = anitopy_parse(filename)
    title = info.get("anime_title") or Path(filename).stem.replace(".", " ")

    season = info.get("anime_season")
    if isinstance(season, list):
        season = season[0]
    try:
        season = int(season) if season else None
    except ValueError:
        season = None

    types = info.get("anime_type") or []
    if isinstance(types, str):
        types = [types]
    types = {t.upper() for t in types}

    # anitopy sometimes leaves the type in the title ("Kimetsu no Yaiba OVA"),
    # which would split OVAs/Specials into their own show folder
    title = re.sub(r"\s+(?:OVA|OAD|OAV|ONA|Specials?|SP|Movie|Gekijouban)$", "", title.strip(), flags=re.IGNORECASE)
    # ...and openings/endings in batches ("Show - NCOP1.mkv"), which would become a show of their own
    m = re.search(r"(?:\s+-\s+|\s+)(NC\s?OP|NC\s?ED|Creditless\s+(?:OP|ED|Opening|Ending))\s*\d*[a-z]?$"
                  r"|\s+-\s+(OP|ED|Opening|Ending)\s*\d*[a-z]?$", title, flags=re.IGNORECASE)
    if not m and types & {"OP", "ED", "OPENING", "ENDING", "NCOP", "NCED"}:   # anitopy saw it: "Bleach OP"
        m = re.search(r"\s+(?:(?:-\s+)?(?:Clean|Creditless)(?:\s+Final)?\s+)?(OP|ED|NCOP|NCED|Opening|Ending)\s*\d*[a-z]?$",
                      title, flags=re.IGNORECASE)   # "Show - Clean Final ED": not a show called "Show - Clean"
    if m:
        kind = (m.group(1) or m.group(2)).upper().replace(" ", "")
        types.add("NCED" if re.search(r"(ED|ENDING)$", kind) else "NCOP")
        title = title[:m.start()]
    # ...and the other extras ("Show - CM1", "Show (PV 1)", "Show Menu - 03", "Show - NCED - Song name"): the title
    # stops at the marker, the file is an extra
    m = EXTRA_IN_TITLE.search(title)
    if m and m.start() > 0:
        types.add("EXTRA")
        title = title[:m.start()]

    episode = info.get("episode_number")
    # no episode number and a marker anitopy dropped with the brackets ("Claymore (BD menu Box 1)"); with a number,
    # words like "Ending" are more likely an episode's title than an extra
    if episode is None and not types & EXTRA_TYPES and extra_kind(filename) in ("menu", "commercial", "preview", "op_ed"):
        types.add("EXTRA")
    return title.strip(" -(").strip(), season, types, episode


# ---------------------------------------------------------------- extras (openings, previews, commercials, menus...)
# Show / Extras / <kind> / [season folder's name, when the show has several] / file
EXTRAS_FOLDER = "Extras"
EXTRA_KIND_FOLDERS = {"op_ed": "Openings & Endings", "preview": "Previews", "commercial": "Commercials",
                      "menu": "Menus", "bonus": "Bonus"}
EXTRA_TOKEN = (r"(?:NC\s?OP\d*|NC\s?ED\d*|Creditless|Non[- ]?Credit|Clean\s+(?:Final\s+)?(?:Opening|Ending|OP|ED)|CMs?(?=[\s\d)\]]|$)\s*\d*"
               r"|PVs?(?=[\s\d)\]]|$)\s*\d*|(?:BD\s|DVD\s)?Menus?|Offici?al\s+Trailers?|Trailers?|Teasers?"
               r"|Promo(?:tion(?:al)?)?(?:\s+Videos?)?|TV\s+Spots?|Previews?|Bonus|Omake|Tokuten|Picture\s+Drama"
               r"|Audio\s+Drama|Live[- ]Action|Making[- ]of)")
EXTRA_IN_TITLE = re.compile(rf"(?:\s+-\s+|\s+|\(\s*){EXTRA_TOKEN}\b", re.I)
EXTRA_KIND_RULES = [   # (kind, pattern); a file's name first, then its folders (innermost first); first match wins
    ("menu", re.compile(r"\b(?:BD\s?|DVD\s?)?menus?\b", re.I)),
    ("commercial", re.compile(r"\bCMs?\s*\d*\b|\bCM\d+|\b(?:TV\s?)?spots?\b|\bcommercials?\b", re.I)),
    ("op_ed", re.compile(r"\bNC\s?(?:OP|ED)|\bcreditless\b|\bnon[- ]?credit|\bclean\s+(?:final\s+)?(?:opening|ending|op|ed)\b"
                         r"|\b(?:OP|ED)\s*\d{0,2}(?:v\d)?\b|(?<=[\s._\-\[(])(?:OP|ED)\d+|\bopenings?\b|\bendings?\b"
                         r"|\bNC\b", re.I)),
    ("preview", re.compile(r"\bPVs?\s*\d*\b|\bPV\d+|\bpreviews?\b|\byokoku\b|\bteasers?\b|\btrailers?\b"
                           r"|\bpromo(?:tion(?:al)?)?(?:\s+videos?)?\b", re.I)),
    ("bonus", re.compile(r"\bbonus\b|\bomake\b|\btokuten\b|\bpicture\s+drama\b|\baudio\s+dramas?\b|\blive[- ]action\b"
                         r"|\bmaking[- ]of\b|\binterviews?\b|\bspecials?\b", re.I)),
]


def extra_kind(name, folders=()):
    """What kind of extra a file is ("op_ed", "preview", "commercial", "menu", "bonus"), from its name, else from
    the folders it sits in (innermost first: "PV/", "Menus/", "NC/", "Creditless/"). None when nothing says."""
    for text in [Path(name).stem, *folders]:
        if text in EXTRA_KIND_FOLDERS.values():   # already sorted: its kind folder
            return next(k for k, v in EXTRA_KIND_FOLDERS.items() if v == text)
        for kind, rx in EXTRA_KIND_RULES:
            if rx.search(text.replace(".", " ").replace("_", " ")):
                return kind
    return None


def extra_kind_of(path, show_dir=None):
    """extra_kind for a file on disk, using its folders up to the show folder."""
    p = Path(path)
    parents = []
    for d in p.parents:
        if show_dir is not None and d == Path(show_dir):
            break
        parents.append(d.name)
        if len(parents) >= 4:
            break
    return extra_kind(p.name, parents)


def pick_subfolder(season, types, is_movie):
    if types & {"OVA", "OAD", "OAV"}:
        return "OVAs"
    if types & {"SPECIAL", "SPECIALS", "SP"} or season == 0:   # "S00E02" = a special, not season 1
        return "Specials"
    if types & {"NCOP", "NCED", "OP", "ED", "OPENING", "ENDING", "PV", "PREVIEW", "CM", "EXTRA"}:
        return EXTRAS_FOLDER
    if is_movie:
        return "Movies"
    return f"Season {season or 1}"


def safe_name(name):
    """Make a title safe to use as a Windows folder name."""
    name = re.sub(r"\s*:\s*", " - ", name)        # Re:Zero -> Re - Zero
    name = re.sub(r"[\\/]", " ", name)            # Fate/Zero -> Fate Zero
    name = name.replace('"', "'")
    name = re.sub(r'[<>|?*]', "", name)
    name = re.sub(r"\s{2,}", " ", name)
    return name.strip(" .") or "Unknown"


# ---------------------------------------------------------------- AniList

ANILIST_URL = "https://graphql.anilist.co"

# AniList allows 90 requests/minute normally but has been running "degraded" at 30.
# Requests are spaced out to stay under this, instead of hitting the limit and
# then waiting a full minute.
ANILIST_PER_MINUTE = 28

# A title AniList can't find isn't searched again for this many days
MISS_RETRY_DAYS = 3

_rate_lock = threading.Lock()
_last_request = 0.0


def _throttle():
    """Wait until the next request is allowed. Thread-safe, so the GUI and a
    running job share one budget."""
    global _last_request
    with _rate_lock:
        wait = _last_request + 60 / ANILIST_PER_MINUTE - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()
MEDIA_FIELDS = "id format isAdult title { romaji english } coverImage { extraLarge large }"
QUERY_SEARCH = ("query ($search: String) { Page(perPage: 8) { media(search: $search, type: ANIME) { "
                + MEDIA_FIELDS + " synonyms } } }")
QUERY_ID = "query ($id: Int) { Media(id: $id, type: ANIME) { " + MEDIA_FIELDS + " } }"


def anilist(query, variables, tries=3):
    body = json.dumps({"query": query, "variables": variables}).encode()
    for _ in range(tries):
        _throttle()
        req = urllib.request.Request(ANILIST_URL, data=body, headers={
            "Content-Type": "application/json", "Accept": "application/json",
            "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.load(resp).get("data")
        except urllib.error.HTTPError as e:
            if e.code == 429:  # rate limited (90 requests/minute)
                wait = int(e.headers.get("Retry-After", 60))
                log.info(f"  AniList rate limit, waiting {wait}s...")
                time.sleep(wait)
                continue
            if e.code == 404:
                return None
            log.warning(f"  AniList error {e.code}")
            return None
        except (urllib.error.URLError, TimeoutError) as e:
            log.warning(f"  Can't reach AniList ({e}), retrying...")
            time.sleep(3)
    return None


_list_ids = None


def my_list_ids():
    """AniList IDs on your own anime list (empty if the app was never connected)."""
    global _list_ids
    if _list_ids is None:
        try:
            _list_ids = {int(k) for k in json.loads(LIST_FILE.read_text(encoding="utf-8")).get("entries", {})}
        except (OSError, ValueError):
            _list_ids = set()
    return _list_ids


def choose(results, search, want_movie):
    if not results:
        return None
    s = search.lower()
    preferred = {"MOVIE"} if want_movie else {"TV", "TV_SHORT", "ONA"}
    mine = my_list_ids()
    # Shows on your AniList list are tried first, so a vague name like "Demon Slayer"
    # picks the show you actually watch over a look-alike
    for pool in ([m for m in results if m["id"] in mine], results):
        for m in pool:  # exact title match wins
            t = m.get("title") or {}
            if s in {(t.get("romaji") or "").lower(), (t.get("english") or "").lower()}:
                if want_movie is None or (m.get("format") == "MOVIE") == want_movie:
                    return m
        for m in pool:
            if m.get("format") in preferred and title_fits(search, to_entry(m)):
                return m
    # nothing like the name (AniList's search answered "Momoiro Paradox Classroom Special" with Kemono Friends):
    # no match beats a wrong show
    return next((m for m in results if title_fits(search, to_entry(m))), None)


def plain_title(text):
    """A title with only letters and digits: "Kon'in Kankei!?" and "Konin Kankei" are the same."""
    return re.sub(r"[^0-9a-z]+", "", (text or "").lower())


def search_shorter(title):
    """AniList's search finds nothing when a word is written differently ("Konin" for "Kon'in"): search the first
    words of the title instead, and keep only a result whose title is the same once punctuation is ignored."""
    words = title.split()
    want = plain_title(title)
    tried = set()
    for n in (len(words) - 1, (len(words) + 1) // 2, 2):
        q = " ".join(words[:n])
        if n < 2 or q in tried:
            continue
        tried.add(q)
        data = anilist(QUERY_SEARCH, {"search": q})
        for m in ((data or {}).get("Page") or {}).get("media") or []:
            t = m.get("title") or {}
            names = [t.get("romaji"), t.get("english"), *(m.get("synonyms") or [])]
            if want and any(plain_title(x) == want for x in names if x):
                log.info(f"  found as '{t.get('romaji') or t.get('english')}' (searched '{q}')")
                return m
    return None


def to_entry(media):
    t = media.get("title") or {}
    cover = (media.get("coverImage") or {})
    return {"id": media["id"], "romaji": t.get("romaji"), "english": t.get("english"),
            "format": media.get("format"), "cover": cover.get("extraLarge") or cover.get("large"),
            "adult": media.get("isAdult"), "synonyms": media.get("synonyms") or []}


def preferred_name(entry):
    if TITLE_LANGUAGE == "english":
        return entry.get("english") or entry.get("romaji")
    return entry.get("romaji") or entry.get("english")


def other_name(entry):
    """The title in the other language, so folders made before switching still match."""
    if TITLE_LANGUAGE == "english":
        return entry.get("romaji")
    return entry.get("english")


def fetch_by_id(mid, cache):
    key = f"id:{mid}"
    if key not in cache:
        data = anilist(QUERY_ID, {"id": mid})
        if not data or not data.get("Media"):
            return None
        cache[key] = to_entry(data["Media"])
    return cache[key]


def lookup(title, want_movie, cache):
    """Return (entry or None, forced_season or None)."""
    override = OVERRIDES.get(title.lower())
    if override is not None:
        oid, season = (override["id"], override.get("season")) if isinstance(override, dict) else (override, None)
        return fetch_by_id(oid, cache), season

    key = f"{title.lower()}|{'movie' if want_movie else 'show'}"
    hit = cache.get(key)
    if hit and "synonyms" not in hit and not title_fits(title, hit):
        cache.pop(key)   # matched before results had to resemble the name: look again (now with its other names)
    if key not in cache:
        miss_key = f"miss:{key}"
        if time.time() - cache.get(miss_key, 0) < MISS_RETRY_DAYS * 86400:
            return None, None  # searched recently and not found
        data = anilist(QUERY_SEARCH, {"search": title})
        results = ((data or {}).get("Page") or {}).get("media") or []
        media = choose(results, title, want_movie)
        if not media and data is not None and len(title.split()) > 2:
            media = search_shorter(title)
        if not media:
            if data is not None:  # a real "no results", not a network error
                cache[miss_key] = time.time()
            return None, None
        cache.pop(miss_key, None)
        cache[key] = to_entry(media)
    return cache[key], None


QUERY_RELATIONS = ("query ($id: Int) { Media(id: $id, type: ANIME) { " + MEDIA_FIELDS
                   + " relations { edges { relationType node { type " + MEDIA_FIELDS + " } } } } }")
TV_FORMATS = {"TV", "TV_SHORT", "ONA"}
EXTRA_KINDS = ("OVA", "SPECIAL", "MOVIE")


def media_relations(mid, cache):
    """An entry plus its related anime (prequels, sequels, side stories...)."""
    key = f"rel:{mid}"
    if key not in cache:
        data = anilist(QUERY_RELATIONS, {"id": mid})
        media = (data or {}).get("Media")
        if not media:
            return None
        rels = []
        for edge in ((media.get("relations") or {}).get("edges") or []):
            node = edge.get("node") or {}
            if node.get("type") == "ANIME" and node.get("id"):
                rels.append([edge.get("relationType"), to_entry(node)])
        cache[key] = {"entry": to_entry(media), "relations": rels}
    return cache[key]


def next_in_chain(info, rel_type):
    """Next PREQUEL/SEQUEL, preferring TV seasons over movies/OVAs."""
    cands = [n for t, n in info["relations"] if t == rel_type]
    tv = [n for n in cands if n["format"] in TV_FORMATS]
    return (tv or cands or [None])[0]


def season_map(entry, cache):
    """Follow AniList's prequel/sequel links to find each season's entry.
    Movies/OVAs in the middle of the chain (e.g. Demon Slayer's Mugen Train)
    are walked through but not counted as seasons."""
    info = media_relations(entry["id"], cache)
    if not info:
        return None

    back_seen = {entry["id"]}
    for _ in range(20):  # walk back to the very first entry
        prev = next_in_chain(info, "PREQUEL")
        if not prev or prev["id"] in back_seen:
            break
        back_seen.add(prev["id"])
        prev_info = media_relations(prev["id"], cache)
        if not prev_info:
            break
        info = prev_info

    seen = set()
    result = {"seasons": [], "OVA": None, "SPECIAL": None, "MOVIE": None, "chain_ids": seen}
    for _ in range(25):  # then forward, counting the TV seasons
        e = info["entry"]
        seen.add(e["id"])
        if e["format"] in TV_FORMATS:
            result["seasons"].append(e)
        elif e["format"] in EXTRA_KINDS and not result[e["format"]]:
            result[e["format"]] = e
        for t, n in info["relations"]:
            if t in ("SIDE_STORY", "SEQUEL", "PREQUEL") and n["format"] in EXTRA_KINDS and not result[n["format"]]:
                result[n["format"]] = n
        nxt = next_in_chain(info, "SEQUEL")
        if not nxt or nxt["id"] in seen:
            break
        info = media_relations(nxt["id"], cache)
        if not info:
            break
    seen |= back_seen  # every AniList entry that belongs to this show's main run
    return result


SEASON_DIR_RE = re.compile(r"season\s*0*(\d+)(?:\s*[-\u2013\u2014:]\s*(.*))?", re.IGNORECASE)
FAMILY_RELATIONS = {"SIDE_STORY", "SUMMARY", "OTHER", "ALTERNATIVE", "PREQUEL", "SEQUEL", "PARENT", "COMPILATION"}
KIND_SUFFIX = re.compile(r"(?:^|\s)(ovas?|oads?|specials?|movies?)$", re.IGNORECASE)
KIND_WORD = {"ova": "OVA", "ovas": "OVA", "oad": "OVA", "oads": "OVA", "special": "SPECIAL", "specials": "SPECIAL",
             "movie": "MOVIE", "movies": "MOVIE"}


def norm_title(title):
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).split())


def show_family(entry, cache, offline=False):
    """Everything on AniList that belongs to one show: its TV seasons in order and the OVAs,
    specials, movies and side stories around them (not spin-offs: those are shows of their own).
    offline=True uses only what's cached (for Kura's library scan). Members:
    {"entry", "season": n (AniList's TV entries counted), "group": n (parts joined, how release
    groups count) or None for extras}."""
    get = (lambda i: cache.get(f"rel:{i}")) if offline else (lambda i: media_relations(i, cache))
    info = get(entry["id"])
    if not info:
        return None
    seen = {entry["id"]}
    for _ in range(20):
        prev = next_in_chain(info, "PREQUEL")
        if not prev or prev["id"] in seen:
            break
        seen.add(prev["id"])
        p = get(prev["id"])
        if not p:
            break
        info = p
    chain, ids = [], set()
    for _ in range(25):
        chain.append(info)
        ids.add(info["entry"]["id"])
        nxt = next_in_chain(info, "SEQUEL")
        if not nxt or nxt["id"] in ids:
            break
        n = get(nxt["id"])
        if not n:
            break
        info = n
    members, season, group = [], 0, 0
    for i in chain:
        e = i["entry"]
        if e["format"] in TV_FORMATS:
            season += 1
            group += 0 if (group and is_part(e)) else 1
            members.append({"entry": e, "season": season, "group": group})
    others = {}
    for i in chain:
        e = i["entry"]
        if e["format"] not in TV_FORMATS:
            others[e["id"]] = e
        for t, n in i["relations"]:
            if t in FAMILY_RELATIONS and n["id"] not in ids and n["id"] not in others:
                others[n["id"]] = n
    members += [{"entry": e, "season": None, "group": None} for e in others.values()]
    return {"members": members, "seasons": [m for m in members if m["season"]]}


def classify_folder(name, fam):
    """What a subfolder of a show is: {"season": n, "group": n, "entry"} for a TV season
    ("Season 2", "Season 2 - The Evil Eye", or named after it), {"kind": "OVA"/"SPECIAL"/"MOVIE"}
    for a generic "OVAs"/"Specials" folder, or None. Only TV seasons belong in a show's folder (user's
    rule), so a folder named after an OVA / side story isn't matched to it: that entry gets its own folder."""
    import difflib
    m = SEASON_DIR_RE.fullmatch(name.strip())
    if m:
        n = int(m.group(1))
        mem = next((x for x in (fam or {}).get("members", []) if x["group"] == n), None)
        return {"season": n, "group": n, "entry": mem["entry"] if mem else None, "numbered": True}
    key = norm_title(name)
    if key in KIND_WORD:   # plain "OVAs" / "Specials" / "Movies": the kind, not a title that ends that way
        return {"kind": KIND_WORD[key]}
    # "Queen's Blade Rebellion" for the season "Rebellion": also try without the show's name
    base = franchise_norm(fam)
    short = key[len(base):].strip() if base and key.startswith(base + " ") else None
    best, score = None, 0.0
    for mem in (fam or {}).get("seasons", []):
        for t in (mem["entry"].get("english"), mem["entry"].get("romaji")):
            tk = norm_title(t)
            if not tk or not key:
                continue
            if key == tk or (short and short == tk):
                sc = 1.0
            elif len(key) >= 4 and tk.endswith(" " + key):   # "The Evil Eye" for "Queen's Blade 2: The Evil Eye"
                sc = 0.97
            else:
                sc = difflib.SequenceMatcher(None, key, tk).ratio()
            if sc > score:
                best, score = mem, sc
    if best and score >= 0.9:
        return {"season": best["season"], "group": best["group"], "entry": best["entry"]}
    k = KIND_SUFFIX.search(name.strip())
    if k:
        return {"kind": KIND_WORD[k.group(1).lower()]}
    return None


def _common_prefix(titles):
    """The words every title starts with, normalised ("queen s blade"); "" if none."""
    split = [t.split() for t in titles if t]
    if len(split) < 2 or len(split) != len(titles):
        return ""
    norm_words = [[norm_title(w) for w in t] for t in split]
    k = 0
    while all(len(w) > k and w[k] == norm_words[0][k] for w in norm_words):
        k += 1
    return norm_title(" ".join(split[0][:k]))


def franchise_bases(fam, show_name=None):
    """Ways the show's own name is written, longest-shared first: the prefix all season titles
    share (each language), the show folder's name, and season 1's titles."""
    seasons = (fam or {}).get("seasons") or []
    bases = []
    for lang in ("english", "romaji"):
        bases.append(_common_prefix([m["entry"].get(lang) for m in seasons]))
    if seasons:
        bases += [norm_title(seasons[0]["entry"].get("english")), norm_title(seasons[0]["entry"].get("romaji"))]
    bases.append(norm_title(show_name))
    return [b for i, b in enumerate(bases) if b and b not in bases[:i]]


def franchise_norm(fam):
    bases = franchise_bases(fam)
    return min(bases, key=len) if bases else ""


def _strip_prefix(title, base):
    words = title.split()
    for i in range(len(words) + 1):
        if norm_title(" ".join(words[:i])) == base:
            return " ".join(words[i:])
    return None


_GEN = r"(?:season\s*\d+|\d+(?:st|nd|rd|th)\s+season|part\s*\d+|(?:\d+(?:st|nd|rd|th)\s+)?cour(?:\s*\d+)?|" \
       r"final season|[ivx]+|[\u2160-\u216f]+|\d+)"
LEAD_GENERIC = re.compile(r"^(?:[\s:\-\u2013\u2014.,]+|" + _GEN + r"(?=[\s:\-\u2013\u2014.,]|$))+", re.IGNORECASE)
TRAIL_GENERIC = re.compile(r"(?:^|[\s:\-\u2013\u2014.,]+)" + _GEN + r"[\s.,]*$", re.IGNORECASE)


def _clean_label(rest):
    rest = LEAD_GENERIC.sub("", rest or "").strip()
    prev = None
    while prev != rest:
        prev, rest = rest, TRAIL_GENERIC.sub("", rest).strip()
    return rest.strip(" :-\u2013\u2014.,")


def season_label(fam, group, show_name=None):
    """The season's own name without the show's name ("The Evil Eye" for Queen's Blade 2: The Evil
    Eye), or None when AniList only calls it "Season 2" / "2nd Season" / "II", or when it's the same
    subtitle as season 1."""
    mem = next((m for m in (fam or {}).get("members", []) if m["group"] == group), None)
    if not mem:
        return None
    first = (fam or {}).get("seasons", [{}])[0].get("entry") or {}
    langs = ["english", "romaji"] if TITLE_LANGUAGE == "english" else ["romaji", "english"]
    bases = franchise_bases(fam, show_name)
    for lang in langs:
        title = mem["entry"].get(lang)
        if not title:
            continue
        for base in bases:
            rest = _strip_prefix(title, base)
            if rest is None:
                continue
            label = _clean_label(rest)
            if not label:
                return None
            if group == 1 and show_name and norm_title(label) in norm_title(show_name):
                return None   # already in the show folder's name ("Queen's Blade - The Exiled Virgin")
            if group > 1 and first.get(lang):
                first_rest = _strip_prefix(first[lang], base)
                if first_rest is not None and norm_title(_clean_label(first_rest)) == norm_title(label):
                    return None   # "Show II: <the same subtitle as season 1>"
            return label
    return None


def season_folder_name(fam, group, style=None, show_name=None):
    style = style or SEASON_FOLDER_STYLE
    label = season_label(fam, group, show_name) if style != "number" else None
    if label and style == "name":
        return safe_name(label)
    if label:
        return f"Season {group} - {safe_name(label)}"
    return f"Season {group}"


# ---------------------------------------------------------------- versions (censored / uncensored, TV / BD...)
# Some shows come in versions with the same episodes: Bikini Warriors' "Normal Version" and "Hyper Sexy Version",
# censored / uncensored, TV / Blu-ray, director's cuts. They live in version folders (in the show folder, or in a
# season folder); Kura counts an episode once whichever version has it and plays the version you prefer.
UNCUT_WORDS = re.compile(r"uncensored|uncut|hyper ?sexy|director'?s ?cut|extended|unrated|blu-?ray|\bbd\b|"
                         r"remaster(?:ed)?", re.I)
CUT_WORDS = re.compile(r"censored|normal|\btv\b|broadcast|\bweb\b|original|standard|regular|theatrical", re.I)
VERSION_FOLDER_RE = re.compile(r"\b(version|ver|cut|edition)\b|^(uncensored|censored|uncut|tv|bd|blu-?ray|web|"
                               r"normal|extended|unrated|remastered)$", re.I)
# language versions (Tomb Raider King: English / Korean): "English", "Korean (Original)", "English Dub", "Subbed"
LANGUAGES = r"english|korean|japanese|chinese|mandarin|cantonese|spanish|french|german|portuguese|italian"
LANGUAGE_FOLDER_RE = re.compile(rf"^(({LANGUAGES})(\s*[-(]?\s*(dub|dubbed|sub|subbed|audio|original)\)?)?|"
                                r"dub|dubbed|sub|subbed|original)$", re.I)


def is_version_folder(name):
    if re.match(r"(?i)^(season|specials?|ovas?|movies?|extras?|spin offs|side stories)\b", name):
        return False
    return bool(VERSION_FOLDER_RE.search(name) or LANGUAGE_FOLDER_RE.match(name.strip()))


def version_rank(name):
    """2 = uncensored / extended / Blu-ray (preferred by default), 0 = censored / TV / normal, 1 = anything else
    (language versions too: the most complete one plays until you pick)."""
    if LANGUAGE_FOLDER_RE.match(name.strip()) or re.search(rf"(?i)\b({LANGUAGES}|dub|dubbed)\b", name):
        return 1
    if UNCUT_WORDS.search(name):
        return 2
    if CUT_WORDS.search(name):
        return 0
    return 1


SEASON_VERSION_RE = re.compile(r"^season\s*0*(\d+)\s*[-\u2013\u2014:]\s*(.+)$", re.I)


def season_version(name):
    """(season, version) for a folder like "Season 1 - Korean" / "Season 1 - Uncensored", else None."""
    m = SEASON_VERSION_RE.match(name.strip())
    if m and is_version_folder(m.group(2).strip()):
        return int(m.group(1)), m.group(2).strip()
    return None


def season_version_groups(show_dir):
    """{season: {version: folder}} for seasons kept as side-by-side folders ("Season 1 - Korean" and
    "Season 1 - Japanese", the user's Tomb Raider King); only seasons with two or more."""
    groups = {}
    try:
        for c in Path(show_dir).iterdir():
            sv = season_version(c.name) if c.is_dir() and not is_link(c) else None
            if sv:
                groups.setdefault(sv[0], {})[sv[1]] = c
    except OSError:
        pass
    return {n: g for n, g in groups.items() if len(g) > 1}


def version_dirs(folder):
    """{version name: folder} for the version folders directly inside `folder`."""
    out = {}
    try:
        for c in Path(folder).iterdir():
            if c.is_dir() and not is_link(c) and is_version_folder(c.name):
                out[c.name] = c
    except OSError:
        pass
    return out


def version_for_file(base, filename):
    """The version folder (in base, or base's parent when base has none yet) a new episode belongs in, or None
    when the show isn't kept in versions. A file saying "Hyper Sexy" / "Uncensored" goes to the folder that says
    so; a file saying nothing goes to the plain one (Normal / TV / Censored)."""
    candidates = [version_dirs(w) if w.exists() else {} for w in (Path(base), Path(base).parent)]
    sv = season_version(Path(base).name)   # "Season 1 - Korean": its side-by-side versions
    if sv:
        candidates.insert(0, season_version_groups(Path(base).parent).get(sv[0], {}))
    for dirs in candidates:
        if not dirs:
            continue
        stem = Path(filename).stem
        langs = {n: d for n, d in dirs.items() if version_rank(n) == 1 and re.search(rf"(?i){LANGUAGES}|dub|sub", n)}
        if langs:   # language versions: the folder whose language (or dub / sub) the file mentions
            said = [w.lower() for w in re.findall(rf"(?i)\b({LANGUAGES}|dub|dubbed|eng)\b", stem)]
            said = ["english" if w in ("eng", "dub", "dubbed") else w for w in said]
            for w in said:
                hit = next((d for n, d in langs.items() if w in n.lower() or (w == "english" and "dub" in n.lower())),
                           None)
                if hit is not None:
                    return hit
            # it doesn't say: the version whose files come from the same group ([SubsPlease] → the Korean one)
            group = re.match(r"^\[([^\]]+)\]", Path(filename).name)
            if group:
                for d in langs.values():
                    try:
                        if any(f.name.startswith(f"[{group.group(1)}]") for f in d.iterdir()):
                            return d
                    except OSError:
                        pass
            return None   # nothing to go by: leave it where the show's files would go
        words = UNCUT_WORDS.findall(stem)
        if words:
            key = re.sub(r"\W", "", words[0]).lower()
            hit = next((d for n, d in dirs.items() if key in re.sub(r"\W", "", n).lower()), None)
            if hit is None:
                hit = next((d for n, d in dirs.items() if version_rank(n) == 2), None)
            return hit
        plain = [d for n, d in dirs.items() if version_rank(n) == 0]
        return plain[0] if plain else None
    return None


def season_dir(show_dir, group, fam):
    """The folder for a season (by release-group count): the one you already have, whatever it's
    called (or the one you said counts as that season: Edit info), else a new one named the way
    SEASON_FOLDER_STYLE says."""
    if show_dir.is_dir():
        for name, n in (FOLDER_SEASONS.get(show_dir.name.lower()) or {}).items():
            if n == group and (show_dir / name).is_dir():
                return show_dir / name
        for d in sorted(show_dir.iterdir()):
            if d.is_dir() and not is_link(d) and d.name not in (SPINOFF_FOLDER, SIDE_STORY_FOLDER):
                c = classify_folder(d.name, fam)
                if c and c.get("group") == group:
                    return d
    return show_dir / (season_folder_name(fam, group, show_name=show_dir.name) if fam else f"Season {group}")


QUERY_EPISODES = "query ($id: Int) { Media(id: $id, type: ANIME) { episodes status } }"


def episode_count(mid, cache):
    """How many episodes an AniList entry has (None while airing with no total yet).
    Cached; unknown counts are asked again after a day."""
    key = f"eps:{mid}"
    hit = cache.get(key)
    if hit and (hit.get("n") or time.time() - hit.get("t", 0) < 86400):
        return hit.get("n")
    data = anilist(QUERY_EPISODES, {"id": mid})
    media = (data or {}).get("Media")
    if media is None:
        return (hit or {}).get("n")
    cache[key] = {"n": media.get("episodes"), "t": time.time()}
    return media.get("episodes")


QUERY_ADULT = "query ($id: Int) { Media(id: $id, type: ANIME) { isAdult } }"


def is_adult_entry(entry, cache):
    """AniList marks the show 18+ (entries cached before Kura asked: one request, then cached)."""
    if not entry:
        return False
    if entry.get("adult") is not None:
        return bool(entry["adult"])
    key = f"adult:{entry['id']}"
    if key in cache:
        return bool(cache[key])
    data = anilist(QUERY_ADULT, {"id": entry["id"]})
    media = (data or {}).get("Media")
    if media is None:
        return False
    cache[key] = bool(media.get("isAdult"))
    return cache[key]


QUERY_ADULT_MANY = "query ($ids: [Int]) { Page(perPage: 50) { media(id_in: $ids, type: ANIME) { id isAdult } } }"


def adult_ids(ids, cache):
    """Which of these AniList ids are 18+: cached answers, the rest asked 50 per request (then cached)."""
    ask = [i for i in dict.fromkeys(ids) if f"adult:{i}" not in cache]
    for k in range(0, len(ask), 50):
        data = anilist(QUERY_ADULT_MANY, {"ids": ask[k:k + 50]})
        for m in ((data or {}).get("Page") or {}).get("media") or []:
            cache[f"adult:{m['id']}"] = bool(m.get("isAdult"))
    return {i for i in ids if cache.get(f"adult:{i}")}


def adult_library_from_settings():
    """Run from qBittorrent (no Kura around): Kura's 18+ folder from gui_settings.json, if it's switched on."""
    try:
        s = json.loads((SCRIPT_DIR / "gui_settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return s.get("adult_library") if s.get("adult_enabled") and s.get("adult_library") else None


PART_RE = re.compile(r"\b(?:part|cour)\s*(?:[2-9]|ii|iii|iv)\b|\b(?:2nd|3rd|second|third)\s+cour\b", re.IGNORECASE)


def is_part(entry):
    """An AniList entry that's the second half of a season ("... Part 2", "2nd Cour"). Release
    groups (and season folders) count it with the part before it."""
    return any(PART_RE.search(t or "") for t in (entry.get("english"), entry.get("romaji")))


def numbered_seasons(entry, cache):
    """The show's seasons as release groups count them: AniList TV entries, with "Part 2"
    entries joined to the season before. [[entry, ...] per season]."""
    smap = season_map(entry, cache)
    out = []
    for e in (smap or {}).get("seasons") or []:
        if out and is_part(e):
            out[-1].append(e)
        else:
            out.append([e])
    return out


def season_for_episode(entry, episode, cache, start=None):
    """Continuous numbering: when season 1 has 12 episodes, "Show - 13" is season 2 (episode 1).
    Walks the seasons from `start` (1-based; default: the matched entry's own season) while the
    number is past a season's end. Returns (season number, episode within it), or (None, episode)
    when AniList doesn't know the seasons or an episode count."""
    seasons = numbered_seasons(entry, cache)
    if not seasons:
        return None, episode
    i = 0 if not start else start - 1
    if not start:
        i = next((k for k, parts in enumerate(seasons) if any(e["id"] == entry["id"] for e in parts)), 0)
    i = max(0, min(i, len(seasons) - 1))
    ep = episode
    while i < len(seasons) - 1:
        counts = [episode_count(e["id"], cache) for e in seasons[i]]
        if not all(counts) or ep <= sum(counts):
            break
        ep -= sum(counts)
        i += 1
    return i + 1, ep


def continuous_starts(show_dir):
    """{season number: first episode} for the show's season folders (2 and up) that already hold
    continuously numbered episodes ("Season 2" starting at 13). Those folders show how this show is
    counted in your library, which beats AniList's split into parts."""
    starts = {}
    if not show_dir or not show_dir.is_dir():
        return starts
    for d in show_dir.iterdir():
        m = SEASON_DIR_RE.fullmatch(d.name)
        if not m or int(m.group(1)) < 2 or not d.is_dir() or is_link(d):
            continue
        eps = []
        for f in d.iterdir():
            if f.is_file() and f.suffix.lower() in VIDEO_EXTS:
                _, season, types, episode = parse(f.name)
                n = episode_number(episode)
                if n and season is None and not types & EXTRA_TYPES:
                    eps.append(n)
        if eps and min(eps) > 1:
            starts[int(m.group(1))] = min(eps)
    return starts


RENAME_CONTINUOUS = True   # episodes counted on from season 1 get their season's own number ("Show - S02E01")
STARTS_FILE = SCRIPT_DIR / "season_starts.json"   # {show folder: {season: its first continuous number}}
MOVES_FILE = SCRIPT_DIR / "moved_files.json"      # {old path: new path}: Kura moves resume points / history along


def _read(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def remembered_starts(show_dir):
    """Where renamed season folders started counting ("Season 2" was 13-25 before its files became S02E01...):
    renamed files no longer show it, and these ranges beat AniList's split into parts."""
    if not show_dir:
        return {}
    return {int(k): v for k, v in (_read(STARTS_FILE).get(show_dir.name) or {}).items()}


def remember_start(show_dir, season, first):
    data = _read(STARTS_FILE)
    data.setdefault(show_dir.name, {})[str(season)] = first
    _write(STARTS_FILE, data)


def record_move(old, new):
    """For Kura: this file is now called / lives there (its resume point and watch history follow)."""
    data = _read(MOVES_FILE)
    old, new = str(old), str(new)
    for k, v in data.items():
        if v == old:   # moved twice: the first old path goes straight to the newest
            data[k] = new
    data[old] = new
    _write(MOVES_FILE, data)


def in_season_name(name, number, season, ep_in):
    """'[SubsPlease] Show - 13v2 (1080p).mkv' → '[SubsPlease] Show - S02E01v2 (1080p).mkv': the continuous
    number swapped for the season's own, everything else kept. None when the number can't be found for sure
    (or the new name wouldn't read back as that season and episode)."""
    stem = Path(name).stem
    depth, outside = 0, []
    for ch in stem:   # numbers inside [CRC] / (1080p) don't count
        if ch in "[(":
            depth += 1
        outside.append(depth == 0)
        if ch in "])" and depth:
            depth -= 1
    hits = [m for m in re.finditer(rf"(?<![\w.]){'0*' + str(number)}(v\d+)?(?![\w.])", stem) if outside[m.start()]]
    dashed = [m for m in hits if stem[:m.start()].endswith(" - ")]
    pick = dashed[-1] if dashed else (hits[0] if len(hits) == 1 else None)
    if pick is None:
        return None
    new = stem[:pick.start()] + f"S{season:02d}E{ep_in:02d}{pick.group(1) or ''}" + stem[pick.end():] + Path(name).suffix
    _, s, _, ep = parse(new)
    return new if s == season and episode_number(ep) == ep_in else None


def renamed_sidecar(sub, video, new_video_name):
    """A subtitle / sidecar named after its episode ("Show - 13.en.ass") follows the episode's new name."""
    return Path(new_video_name).stem + sub.name[len(video.stem):]


def folder_offset(entry, cache, show_dir, season, numbers):
    """How many episodes come before this season folder in a continuous count, judged by the numbers in it:
    a range you had before (renamed folders), else AniList's counts (each TV entry its own season, or "Part 2"
    entries joined to the season before), whichever the numbers fit. None if none fits or two fit differently."""
    known = remembered_starts(show_dir).get(season)
    if known:
        return known - 1
    smap = (season_map(entry, cache) or {}).get("seasons") or []
    fits = set()
    for groups in ([[e] for e in smap], numbered_seasons(entry, cache)):
        if len(groups) < season:
            continue
        before = [episode_count(e["id"], cache) for g in groups[:season - 1] for e in g]
        if not before or not all(before):
            continue
        off = sum(before)
        here = [episode_count(e["id"], cache) for e in groups[season - 1]]
        if min(numbers) > off and (not all(here) or max(numbers) - off <= sum(here)):
            fits.add(off)
    return fits.pop() if len(fits) == 1 else None


def target_season(entry, episode, cache, show_dir):
    """Which season a continuously numbered episode belongs to: your folders' ranges when they
    have them, else AniList's episode counts. (season, episode within it) or (None, episode)."""
    starts = {**continuous_starts(show_dir), **remembered_starts(show_dir)}
    if starts:
        fits = [k for k, first in starts.items() if first <= episode]
        if fits:
            k = max(fits)
            return k, episode - starts[k] + 1
        if episode < min(starts.values()):
            return 1, episode
    return season_for_episode(entry, episode, cache, start=1)


def season_has_episode(folder, episode, absolute=None):
    """Whether a season folder already holds this episode. `episode` is the number within the
    season, `absolute` the continuous one ("25" = season 2 episode 13). Each file is read the way its
    folder numbers: tagged files ("S2 - 01") and folders that start at 1 count within the season,
    continuously numbered folders (first episode above 1) count continuously."""
    if not folder or not folder.is_dir():
        return False
    files = []
    for f in folder.iterdir():
        if f.is_file() and f.suffix.lower() in VIDEO_EXTS:
            _, season, types, ep = parse(f.name)
            n = episode_number(ep)
            if n and not types & EXTRA_TYPES:
                files.append((n, season is not None))
    untagged = [n for n, tagged in files if not tagged]
    continuous = bool(untagged) and min(untagged) > 1
    for n, tagged in files:
        if tagged or not continuous:
            if n == episode:
                return True
        elif absolute is not None and n == absolute:
            return True
    return False


EXTRA_TYPES = {"OVA", "OAD", "OAV", "SPECIAL", "SPECIALS", "SP", "NCOP", "NCED", "OP", "ED", "OPENING", "ENDING",
               "PV", "PREVIEW", "CM", "EXTRA", "MOVIE", "GEKIJOUBAN"}


def episode_number(episode):
    """anitopy's episode ('13', ['13', '14'], '13.5') → 13, or None."""
    if isinstance(episode, list):
        episode = episode[0] if episode else None
    try:
        return int(str(episode))
    except (TypeError, ValueError):
        return None


CACHE_VERSION = 2  # bumped when the saved format changes; old caches are rebuilt


def load_cache():
    try:
        cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"_version": CACHE_VERSION}
    if cache.get("_version") != CACHE_VERSION:
        return {"_version": CACHE_VERSION}
    return cache


def save_cache(cache):
    tmp = CACHE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, CACHE_FILE)


# ---------------------------------------------------------------- Windows bits

FILE_ATTRIBUTE_READONLY, FILE_ATTRIBUTE_HIDDEN, FILE_ATTRIBUTE_SYSTEM = 0x1, 0x2, 0x4

if IS_WINDOWS:
    _k32 = ctypes.windll.kernel32
    _k32.GetFileAttributesW.restype = ctypes.c_uint32
    _k32.GetFileAttributesW.argtypes = [ctypes.c_wchar_p]
    _k32.SetFileAttributesW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
    _shell = ctypes.windll.shell32
    _shell.SHChangeNotify.argtypes = [ctypes.c_long, ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_wchar_p]

    class SHFOLDERCUSTOMSETTINGS(ctypes.Structure):
        _fields_ = [("dwSize", ctypes.c_uint32), ("dwMask", ctypes.c_uint32),
                    ("pvid", ctypes.c_void_p), ("pszWebViewTemplate", ctypes.c_wchar_p),
                    ("cchWebViewTemplate", ctypes.c_uint32), ("pszWebViewTemplateVersion", ctypes.c_wchar_p),
                    ("pszInfoTip", ctypes.c_wchar_p), ("cchInfoTip", ctypes.c_uint32),
                    ("pclsid", ctypes.c_void_p), ("dwFlags", ctypes.c_uint32),
                    ("pszIconFile", ctypes.c_wchar_p), ("cchIconFile", ctypes.c_uint32),
                    ("iIconIndex", ctypes.c_int), ("pszLogo", ctypes.c_wchar_p), ("cchLogo", ctypes.c_uint32)]

    _shell.SHGetSetFolderCustomSettings.argtypes = [
        ctypes.POINTER(SHFOLDERCUSTOMSETTINGS), ctypes.c_wchar_p, ctypes.c_uint32]
    _shell.SHGetSetFolderCustomSettings.restype = ctypes.c_long


def set_folder_icon_api(folder, ico_name):
    """Set the icon the same way Explorer's Properties > Customize does.
    It writes desktop.ini, sets attributes and notifies Explorer properly."""
    if not IS_WINDOWS:
        return False
    FCSM_ICONFILE, FCS_FORCEWRITE = 0x10, 0x2
    s = SHFOLDERCUSTOMSETTINGS()
    s.dwSize = ctypes.sizeof(s)
    s.dwMask = FCSM_ICONFILE
    s.pszIconFile = ico_name
    s.iIconIndex = 0
    hr = _shell.SHGetSetFolderCustomSettings(ctypes.byref(s), str(folder), FCS_FORCEWRITE)
    if hr != 0:
        log.warning(f"  Windows icon API failed (0x{hr & 0xFFFFFFFF:08X}), writing desktop.ini manually")
    return hr == 0


def rebuild_icon_cache():
    """Ask Windows to refresh its icon cache, without killing Explorer."""
    if not IS_WINDOWS:
        return
    import subprocess
    try:
        subprocess.run(["ie4uinit.exe", "-show"], timeout=30,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as e:
        log.warning(f"  Couldn't refresh icon cache ({e})")


def set_attrs(path, add=0, remove=0):
    if not IS_WINDOWS:
        return
    cur = _k32.GetFileAttributesW(str(path))
    if cur == 0xFFFFFFFF:
        return
    _k32.SetFileAttributesW(str(path), (cur | add) & ~remove)


def refresh_folder(folder):
    """Tell Explorer this one folder changed, so it re-reads the icon."""
    if not IS_WINDOWS:
        return
    SHCNE_UPDATEDIR, SHCNE_UPDATEITEM, SHCNF_PATHW = 0x1000, 0x2000, 0x0005
    _shell.SHChangeNotify(SHCNE_UPDATEITEM, SHCNF_PATHW, str(folder), None)
    _shell.SHChangeNotify(SHCNE_UPDATEDIR, SHCNF_PATHW, str(folder.parent), None)


# ---------------------------------------------------------------- icons

def make_icon(show_dir, entry, dry, force=False, ico_name=None):
    ini = show_dir / "desktop.ini"
    ico_name = ico_name or f"{show_dir.name}.ico"   # e.g. "Sousou no Frieren.ico"
    label = Path(ico_name).stem
    if ini.exists() and not force and icon_ok(show_dir):
        return False
    if os.path.normcase(str(show_dir)) in CUSTOM_ICONS and ico_name == f"{show_dir.name}.ico":
        log.info(f"  Kept your own icon for '{label}' (set in Kura)")
        return False
    if not entry or not entry.get("cover"):
        log.info(f"  No cover found for '{label}', skipping icon")
        return False
    if dry:
        log.info(f"  [dry run] would set icon for '{label}'")
        return True

    from PIL import Image

    req = urllib.request.Request(entry["cover"], headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        img = Image.open(io.BytesIO(resp.read())).convert("RGBA")
    write_folder_icon(show_dir, img, ico_name)
    return True


def icon_from_image(show_dir, image_path):
    """A picture of your own (a screenshot from Kura's player) as the show folder's icon."""
    from PIL import Image
    img = Image.open(image_path).convert("RGBA")
    write_folder_icon(Path(show_dir), img, f"{Path(show_dir).name}.ico")
    return True


def ansi_name(name):
    """Full-width forms as plain ones ("Guilty Hole ～ Room…" → "~"): the icon API writes desktop.ini in Windows'
    ANSI code page, and "～" came out as "~", a file that didn't exist (no icon). Names it still can't hold
    (Japanese titles) keep their name; write_folder_icon then writes desktop.ini itself, in UTF-16."""
    import unicodedata
    return unicodedata.normalize("NFKC", name)


def ini_icon(folder):
    """The icon file a folder's desktop.ini points to (Path), or None."""
    ini = Path(folder) / "desktop.ini"
    if not ini.exists():
        return None
    for enc in ("utf-16", "utf-8", "mbcs" if IS_WINDOWS else "latin-1"):
        try:
            m = re.search(r"^IconResource=(.+?)(?:,-?\d+)?\s*$", ini.read_text(encoding=enc), re.MULTILINE)
            break
        except (UnicodeError, LookupError, OSError):
            m = None
    if not m:
        return None
    p = Path(m.group(1).strip())
    return p if p.is_absolute() else Path(folder) / p


def icon_ok(folder):
    """desktop.ini exists and its icon file is really there."""
    p = ini_icon(folder)
    if p is not None and ("%" in str(p) or p.suffix.lower() in (".dll", ".exe")):
        return True   # a Windows icon (shell32.dll…) or one picked in Explorer: leave it
    return bool(p and p.exists())


def write_folder_icon(show_dir, img, ico_name):
    """img (PIL, any shape) → a padded square multi-size .ico, set as the folder's icon."""
    from PIL import Image
    orig_name, ico_name = ico_name, ansi_name(ico_name)
    ini = show_dir / "desktop.ini"
    label = Path(ico_name).stem

    # Pad the poster to a square with transparent sides, like Anicon does
    side = max(img.size)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(img, ((side - img.width) // 2, (side - img.height) // 2))
    canvas = canvas.resize((256, 256), Image.LANCZOS)

    # Remove old icons: ones from earlier versions (cover_*.ico) and whatever
    # the current desktop.ini points to
    old_icons = set(show_dir.glob("cover_*.ico"))
    if orig_name != ico_name:
        old_icons.add(show_dir / orig_name)   # the one an earlier run named with characters ANSI can't hold
    if ini.exists():
        for enc in ("utf-16", "utf-8", "mbcs" if IS_WINDOWS else "latin-1"):
            try:
                m = re.search(r"^IconResource=(.+?)(?:,-?\d+)?\s*$", ini.read_text(encoding=enc), re.MULTILINE)
                break
            except (UnicodeError, LookupError):
                m = None
        if m and m.group(1).lower().endswith(".ico") and not os.path.isabs(m.group(1)):
            old_icons.add(show_dir / m.group(1))
    for old in old_icons:
        if old.exists():
            set_attrs(old, remove=FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM | FILE_ATTRIBUTE_READONLY)
            old.unlink(missing_ok=True)

    ico_path = show_dir / ico_name
    canvas.save(ico_path, sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
    set_attrs(ico_path, add=FILE_ATTRIBUTE_HIDDEN)

    if not set_folder_icon_api(show_dir, ico_name) or not icon_ok(show_dir):
        # Fallback (and non-Windows, or the API wrote a path that doesn't exist): write desktop.ini ourselves
        if ini.exists():
            set_attrs(ini, remove=FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM | FILE_ATTRIBUTE_READONLY)
        # UTF-16 so names like "Pokémon" work
        ini.write_text(f"[.ShellClassInfo]\r\nIconResource={ico_name},0\r\n", encoding="utf-16")
        # Windows only reads desktop.ini if it's hidden+system and the folder is read-only
        set_attrs(ini, add=FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM)
        set_attrs(show_dir, add=FILE_ATTRIBUTE_READONLY)
    refresh_folder(show_dir)
    log.info(f"  Icon set for '{label}'")


SUBFOLDER_KIND = {"ovas": "OVA", "specials": "SPECIAL", "movies": "MOVIE"}


def subfolders_needing_icons(show_dir, force):
    if not show_dir.is_dir():
        return []
    return [d for d in sorted(show_dir.iterdir())
            if d.is_dir() and (force or not icon_ok(d))]


def make_season_icons(show_dir, entry, cache, dry, force=False):
    """Give each Season N / OVAs / Specials / Movies folder its own cover.
    Anything it can't find falls back to the show's main cover."""
    todo = subfolders_needing_icons(show_dir, force)
    if not todo:
        return 0
    overrides = SEASON_COVERS.get(show_dir.name.lower(), {})
    smap = season_map(entry, cache) if entry else None

    count = 0
    for d in todo:
        sub_entry = None
        m = SEASON_DIR_RE.fullmatch(d.name)
        n = int(m.group(1)) if m else None
        named = None if m else classify_folder(d.name, show_family(entry, cache) if entry else None)
        if n is not None and n in overrides:
            sub_entry = fetch_by_id(overrides[n], cache)
        elif named and named.get("entry"):     # a season named after itself ("Queen's Blade Rebellion")
            sub_entry = named["entry"]
        elif smap:
            if n is not None and 1 <= n <= len(smap["seasons"]):
                sub_entry = smap["seasons"][n - 1]
            elif d.name.lower() in SUBFOLDER_KIND:
                sub_entry = smap[SUBFOLDER_KIND[d.name.lower()]]
        try:
            count += make_icon(d, sub_entry or entry, dry, force,
                               ico_name=f"{show_dir.name} - {d.name}.ico")
        except Exception:
            log.exception(f"Icon failed for {show_dir.name}/{d.name}")
    return count


# ---------------------------------------------------------------- sorting

def folder_key(name):
    """A title reduced to its letters and digits: "KAMUI ---He's Behind You", "KAMUI Hes Behind You" and
    "KAMUI.---Hes.behind.you" are one show, whatever the release did with apostrophes, dashes and dots."""
    import unicodedata
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", name or "").lower())


def find_show_folder(library, names, loose=True):
    """The library folder for a show: by exact name first, then (`loose`) ignoring punctuation (folder_key).
    Not found: where it would go (the first name)."""
    folders = [p for p in library.iterdir() if p.is_dir()]
    existing = {p.name.lower(): p for p in folders}
    for n in names:
        if n and n.lower() in existing:
            return existing[n.lower()]
    if loose:
        keys = {}
        for p in folders:
            keys.setdefault(folder_key(p.name), p)
        for n in names:
            k = folder_key(n)
            if len(k) >= 4 and k in keys:
                return keys[k]
    return library / names[0]


PLAN = None   # Kura sets a list before a preview (--dry-run): sort_file adds what it would move


def transfer(src, dst, dry):
    if dst.exists():
        log.info(f"  Already exists, skipping: {dst}")
        return False
    if dry:
        log.info(f"  [dry run] {MODE}: {src.name} -> {dst.parent}")
        return True
    dst.parent.mkdir(parents=True, exist_ok=True)
    if MODE == "hardlink":
        try:
            os.link(src, dst)
        except OSError as e:
            log.warning(f"  Hardlink failed ({e}), copying instead")
            shutil.copy2(src, dst)
    elif MODE == "copy":
        shutil.copy2(src, dst)
    else:
        shutil.move(str(src), str(dst))
    return True


def sidecars(video):
    stem = video.stem.lower()
    for f in video.parent.iterdir():
        if f.is_file() and f != video and f.suffix.lower() in SIDECAR_EXTS and f.name.lower().startswith(stem):
            yield f


SEASON_IN_NAME = re.compile(r"(?:^|[\s._\-\[(])(?:S(\d{1,2})(?!\d)|Season\s*(\d{1,2})|(\d{1,2})(?:st|nd|rd|th)\s+Season)",
                            re.IGNORECASE)


def folder_season(video, title, library):
    """Season from the folder a batch came in ("Show S2 [1080p]/Show - 01.mkv"), for files
    whose own name doesn't say. Only folders about the same show (or with no title) count."""
    key = re.sub(r"[^a-z0-9]", "", title.lower())
    for folder in list(video.parents)[:2]:
        if folder == library or not folder.name:
            break
        m = SEASON_IN_NAME.search(folder.name)
        if not m:
            continue
        ftitle = anitopy_parse(folder.name).get("anime_title") or ""
        ftitle = re.sub(r"[^a-z0-9]", "", (ftitle if isinstance(ftitle, str) else ftitle[0]).lower())
        if not ftitle or not key or ftitle.startswith(key) or key.startswith(ftitle):
            return int(next(g for g in m.groups() if g))
    return None


GENERIC_WORDS = {"op", "ed", "nc", "ncop", "nced", "pv", "cm", "menu", "menus", "bd", "dvd", "episode", "episodes",
                 "ep", "e", "s", "ending", "endings", "opening", "openings", "clean", "creditless", "textless", "full",
                 "audio", "preview", "previews", "trailer", "teaser", "promo", "spot", "vol", "volume", "special",
                 "specials", "bonus", "extra", "extras", "tv", "version", "ver", "song", "theme", "and", "the", "part",
                 "video", "movie", "ova", "oad"}


def generic_title(title):
    """A name that says nothing about the show ("OP", "PV1", "Episode 12", "S1 ED 01", "BD Menu (Vol 2)",
    "Clean Ending"): searching AniList for it finds some random show."""
    words = re.findall(r"[a-z]+|\d+", (title or "").lower())
    return all(w in GENERIC_WORDS or re.fullmatch(r"\d+|[se]p?\d+|v\d+|vol\d+|op\d+|ed\d+|pv\d+|cm\d+", w)
               for w in words)


GENERIC_FOLDERS = {"anime", "downloads", "download", "completed", "complete", "torrents", "torrent", "incoming",
                   "new", "tv", "videos", "seeding"}


def batch_title(video, library):
    """The show a batch folder is named after ("[Group] Show (BD 1080p)/03 - Episode Title.mkv"): some batches name
    their files after the episodes. The nearest folder above the file that holds two or more videos and has a
    title (not "Season 2", not your downloads folder). None otherwise."""
    roots = {os.path.normcase(str(library))}
    try:
        s = json.loads((SCRIPT_DIR / "gui_settings.json").read_text(encoding="utf-8"))
        roots |= {os.path.normcase(str(Path(s[k]))) for k in ("downloads", "library") if s.get(k)}
    except (OSError, ValueError):
        pass
    for folder in list(video.parents)[:3]:
        if not folder.name or os.path.normcase(str(folder)) in roots or folder.name.lower() in GENERIC_FOLDERS:
            return None
        ftitle = anitopy_parse(folder.name).get("anime_title") or ""
        ftitle = ftitle if isinstance(ftitle, str) else ftitle[0]
        if not ftitle or SEASON_DIR_RE.fullmatch(folder.name) or generic_title(folder.name):
            continue   # "Season 2" / "Extras" / "Openings & Endings" inside the batch: look one level up
        try:
            videos = sum(1 for f in folder.rglob("*") if f.suffix.lower() in VIDEO_EXTS)
        except OSError:
            return None
        return ftitle if videos >= 2 else None
    return None


def title_fits(title, entry):
    """Whether a parsed title really is this AniList entry's (and not an episode title that happened to match)."""
    import difflib
    key = folder_key(title)
    for name in (entry.get("english"), entry.get("romaji"), preferred_name(entry), other_name(entry),
                 *(entry.get("synonyms") or [])):
        k = folder_key(name)
        if k and key and (k in key or key in k or difflib.SequenceMatcher(None, k, key).ratio() >= 0.6):
            return True
    return False


KIND_NAMES = {"OVA": ("OVA", "OAD"), "SPECIAL": ("Specials", "Special"), "MOVIE": ("Movie",)}


def named_kind_entry(title, kind, cache):
    """An entry AniList names exactly after the show and the kind ("Queen's Blade OVA" for "Queen's Blade OVA - 01"),
    which the relations alone can't single out. None otherwise."""
    fmts = {"OVA": ("OVA", "SPECIAL"), "SPECIAL": ("SPECIAL", "OVA"), "MOVIE": ("MOVIE",)}[kind]
    for word in KIND_NAMES[kind]:
        name = f"{title} {word}"
        e, _ = lookup(name, kind == "MOVIE", cache)
        if e and e.get("format") in fmts and any(plain_title(n) == plain_title(name)
                                                  for n in (e.get("romaji"), e.get("english")) if n):
            return e
    return None


OWN_ENTRY_RELATIONS = ("SIDE_STORY", "SEQUEL", "PREQUEL", "SUMMARY", "OTHER", "ALTERNATIVE", "COMPILATION")


def own_entry(entry, kind, n, cache):
    """The OVA / special / movie AniList keeps as an entry of its own, for a file that matched a TV season
    ("Kono Subarashii Sekai ni Shukufuku wo! S3 - OVA1" → 3: BONUS STAGE, a SIDE_STORY of season 3).
    Only seasons go in a show's folder; these get a folder of their own (Kura's Relations connect them).
    None when AniList has no such entry or can't tell which one it is (then the parent's OVAs folder)."""
    info = media_relations(entry["id"], cache)
    if not info:
        return None
    fmts = {"OVA": ("OVA", "SPECIAL"), "SPECIAL": ("SPECIAL", "OVA"), "MOVIE": ("MOVIE",)}[kind]
    for fmt in fmts:   # groups and AniList don't agree on OVA vs special (Bikini Warriors' OVAs are a SPECIAL entry)
        cands = [x for t, x in info["relations"] if t in OWN_ENTRY_RELATIONS and x.get("format") == fmt]
        cands.sort(key=lambda x: (OWN_ENTRY_RELATIONS.index(next(t for t, y in info["relations"] if y is x)), x["id"]))
        if len(cands) == 1:
            return cands[0]
        if len(cands) > 1:
            # one entry per OVA (each 1 episode): the n-th in release order (ids go up with time)
            ones = sorted((x for x in cands if x.get("episodes") == 1), key=lambda x: x["id"])
            if n and len(ones) == len(cands) and n <= len(ones):
                return ones[n - 1]
            log.info(f"  ({len(cands)} {fmt} entries are linked to #{entry['id']}: can't tell which, "
                     "keeping it with the show)")
            return None
    return None


def sort_file(video, library, cache, dry):
    title, season, types, episode = parse(video.name)
    if season is None and episode is not None:
        season = folder_season(video, title, library)
        if season:
            log.info(f"  (season {season} from the folder name “{video.parent.name}”)")
    want_movie = bool(types & {"MOVIE", "GEKIJOUBAN"}) or (
        episode is None and season is None and not types & (EXTRA_TYPES - {"MOVIE", "GEKIJOUBAN"}))

    generic = generic_title(title)
    entry, forced_season = (None, None) if generic else lookup(title, want_movie, cache)
    lead_named = False
    if entry is None or not title_fits(title, entry):
        # named after its episode, not the show ("03 - The Beginning.mkv"): the batch folder's name says which show
        btitle = batch_title(video, library)
        if btitle and folder_key(btitle) != folder_key(title):
            lead = re.match(r"\s*(?:E|EP|Episode\s*)?(\d{1,3})(?:v\d)?(?!\d)", video.stem, re.IGNORECASE)
            if episode is None and lead:   # "01 - Maomao" (anitopy takes it all for a title): episode 1, not a movie
                episode, want_movie, lead_named = lead.group(1), False, True
            bentry, bseason = lookup(btitle, want_movie, cache)
            if bentry:
                log.info(f"  ('{title}' doesn't look like the show's name: the batch folder says '{btitle}')")
                entry, forced_season, title = bentry, bseason, btitle
                generic = False
    if generic:   # "OP.mkv" with nothing around it that says which show: better left than sorted into a random one
        log.info(f"{video.name}\n  '{title}' doesn't say which show and no batch folder does either: left where it is")
        if PLAN is not None:
            PLAN.append({"src": str(video), "dest": str(video.parent), "name": video.name, "show": "", "folder": "",
                         "match": "", "parsed": title, "subs": 0, "new_show": False,
                         "skip": "the name doesn't say which show"})
        return None, None
    season = forced_season or season
    if entry and ADULT_LIBRARY_DIR and is_adult_entry(entry, cache):   # 18+: the adult library instead
        library = Path(ADULT_LIBRARY_DIR)
        library.mkdir(parents=True, exist_ok=True)
        log.info(f"  (18+ on AniList: into {library})")
    is_movie = want_movie or (entry is not None and entry["format"] == "MOVIE" and episode is None)
    fmt = (entry or {}).get("format")
    if fmt in ("OVA", "SPECIAL") and not types & {"MOVIE", "GEKIJOUBAN"}:
        # no episode number made it look like a movie, but AniList says OVA / special (KONOSUBA's Choker and
        # Works of Art landed in Movies): OVAs / Specials
        is_movie = False
        types = set(types) | ({"OVA"} if fmt == "OVA" else {"SPECIAL"})
    # only seasons in a show's folder: an OVA / special / movie that AniList keeps separately gets its own
    own_folder = False
    kind = ("MOVIE" if is_movie else "OVA" if types & {"OVA", "OAD", "OAV"}
            else "SPECIAL" if types & {"SPECIAL", "SPECIALS", "SP"} and season != 0 else None)
    if entry and kind and fmt in TV_FORMATS:
        base = entry
        if season and season > 1:   # "S3 - OVA1": the title matched season 1, the OVA belongs to season 3
            fam = show_family(entry, cache) or {}
            base = next((m["entry"] for m in fam.get("seasons", []) if m["group"] == season), None)
        own = (named_kind_entry(title, kind, cache) or own_entry(base, kind, episode_number(episode), cache)
               if base else None)
        if own:
            log.info(f"  ({kind.lower()} of #{base['id']}: AniList has it as #{own['id']} "
                     f"'{preferred_name(own)}', so it gets a folder of its own)")
            entry, season, own_folder = own, None, True
            # its subfolder follows AniList's format, so "13 (Special)" and "14 (OVA)" of one entry stay together
            types = (set(types) - {"OVA", "OAD", "OAV", "SPECIAL", "SPECIALS", "SP"}) | (
                {"SPECIAL"} if own["format"] == "SPECIAL" else {"OVA"} if own["format"] == "OVA" else set())
    # a later season named on its own ("High School DxD NEW - 01", "Queen's Blade Rebellion - 01"): only TV seasons
    # go in a show's folder, so it's that season of the show, in the show's folder (not a folder of its own)
    first = None
    if entry and not own_folder and not is_movie and entry.get("format") in TV_FORMATS:
        try:
            fam = show_family(entry, cache) or {}
        except Exception:
            fam = {}
        mem = next((m for m in fam.get("seasons", []) if m["entry"]["id"] == entry["id"]), None)
        if mem and mem["group"] and mem["group"] > 1:
            first = next((m["entry"] for m in fam["seasons"] if m["group"] == 1), None)
            if first and season is None:
                season = mem["group"]
                log.info(f"  (#{entry['id']} is season {season} of '{preferred_name(first)}': into its folder)")
    # "Show - 13" with no season in the name, when season 1 has 12 episodes: season 2 (and named "Show - S02E01")
    n = episode_number(episode)
    renamed = None
    if (entry and n and season is None and not is_movie and entry.get("format") in TV_FORMATS
            and not types & EXTRA_TYPES):
        show = find_show_folder(library, [safe_name(preferred_name(entry)), safe_name(other_name(entry) or ""),
                                          safe_name(title)])
        found, ep_in = target_season(entry, n, cache, show)
        target = season_dir(show, found, show_family(entry, cache)) if found and found > 1 else None
        if found and found > 1:
            if season_has_episode(target, ep_in, n):
                log.info(f"  (episode {n} would be season {found} episode {ep_in}, but that's already there: "
                         "keeping season 1)")
            else:
                season = found
                log.info(f"  (episode {n} continues past season {found - 1}: season {found}, episode {ep_in})")
                new = in_season_name(video.name, n, found, ep_in) if RENAME_CONTINUOUS else None
                if new:
                    renamed = (new, found, n - ep_in + 1)

    if entry:
        names = [safe_name(preferred_name(entry)), safe_name(other_name(entry) or "")]
        if not own_folder:   # the parsed title is the parent show's ("Bikini Warriors"): it would find its folder
            names.append(safe_name(title))
        if first:   # the show's own folder first; a folder you kept for this season alone still counts after it
            names = [safe_name(preferred_name(first)), safe_name(other_name(first) or "")] + names
        log.info(f"{video.name}\n  parsed '{title}' -> AniList #{entry['id']} '{preferred_name(entry)}'")
    else:
        names = [safe_name(title)]
        log.info(f"{video.name}\n  parsed '{title}' -> no AniList match, using parsed title")
        # the release spelled it differently ("Hes" for "He's"): the folder you already have finds it on AniList
        known = find_show_folder(library, names)
        if known.exists() and known.name != names[0]:
            entry, _ = lookup(known.name, want_movie, cache)
            log.info(f"  (your folder '{known.name}' has the same name"
                     + (f": AniList #{entry['id']} '{preferred_name(entry)}')" if entry else ")"))
            names = [known.name]

    show_dir = find_show_folder(library, names)
    sub = pick_subfolder(season, types, is_movie)
    if sub.startswith("Season ") and entry:
        try:
            dest_dir = season_dir(show_dir, int(sub.split()[1]), show_family(entry, cache))
        except Exception:
            log.exception("  couldn't look up the show's seasons; using the plain folder name")
            dest_dir = show_dir / sub
    elif sub == EXTRAS_FOLDER:   # by kind: Extras / Openings & Endings, Previews, Commercials, Menus, Bonus
        kind = extra_kind(video.name, [p.name for p in list(video.parents)[:2]]) or "bonus"
        dest_dir = show_dir / EXTRAS_FOLDER / EXTRA_KIND_FOLDERS[kind]
    else:
        dest_dir = show_dir / sub
    # shows kept in versions (Normal / Hyper Sexy, censored / uncensored): into the matching version folder
    version = version_for_file(dest_dir if dest_dir.exists() else show_dir, video.name) if show_dir.exists() else None
    if version is not None:
        dest_dir = version

    name = renamed[0] if renamed else video.name
    if lead_named and entry:   # "01 - Maomao.mkv" → "The Apothecary Diaries - 01 - Maomao.mkv": Kura and Jellyfin can read it
        named = f"{safe_name(preferred_name(entry))} - {video.name}"
        if episode_number(parse(named)[3]) == episode_number(episode):
            name, renamed = named, renamed or (named, None, None)
    moved = transfer(video, dest_dir / name, dry)
    if moved:
        subs = list(sidecars(video))
        for sub in subs:
            transfer(sub, dest_dir / (renamed_sidecar(sub, video, name) if renamed else sub.name), dry)
        log.info(f"  -> {dest_dir}" + (f" as {name}" if renamed else ""))
        if renamed and renamed[1] and not dry:
            remember_start(show_dir, renamed[1], renamed[2])
    if dry and PLAN is not None:
        PLAN.append({"src": str(video), "dest": str(dest_dir), "name": name, "show": show_dir.name,
                     "folder": str(dest_dir.relative_to(library)) if dest_dir.is_relative_to(library) else str(dest_dir),
                     "match": preferred_name(entry) if entry else "", "parsed": title,
                     "subs": len(subs) if moved else 0, "new_show": not show_dir.exists(),
                     "skip": "" if moved else "already in the library"})
    return show_dir, entry


def fix_seasons(library, cache, dry, only=None, plan=None):
    """Episodes numbered on from the season before ("Show - 13" when season 1 has 12 episodes): moved into the
    right season folder ("Season 1/Show - 13.mkv" → Season 2) and, with RENAME_CONTINUOUS, renamed to the season's
    own number ("Show - S02E01.mkv"), also when they're in the right folder already.
    Only files without a season in their own name, only forward, never into a folder that already has that
    episode, and never out of Season 0 (specials). `only`: show folder names to fix (default all). `plan`: a list
    that gets (show, file, from, to, episode in season, new file name). Returns {show folder: entry}."""
    touched = {}
    for show_dir in sorted(p for p in library.iterdir() if p.is_dir() and not is_link(p)):
        if only and show_dir.name not in only:
            continue
        season_dirs = [d for d in show_dir.iterdir() if d.is_dir() and not is_link(d)
                       and SEASON_DIR_RE.fullmatch(d.name) and int(SEASON_DIR_RE.fullmatch(d.name).group(1)) > 0]
        if not season_dirs:
            continue
        entry, looked = None, False
        # the file list first: a moved file must not be looked at again in its new folder
        files = []
        for d in sorted(season_dirs, key=lambda d: int(SEASON_DIR_RE.fullmatch(d.name).group(1))):
            for f in sorted(d.iterdir()):
                if f.is_file() and f.suffix.lower() in VIDEO_EXTS:
                    _, season, types, episode = parse(f.name)
                    n = episode_number(episode)
                    if season is None and n and not types & EXTRA_TYPES:   # "S00E01" says season 0: leave it
                        files.append((d, f, n))
        numbers = {}   # each folder's continuous numbers, as they were before anything moved
        for d, _, n in files:
            numbers.setdefault(d, []).append(n)
        for d, f, n in files:
            if not looked:
                entry, _ = lookup(show_dir.name, False, cache)
                looked = True
            if not entry:
                break   # not on AniList: leave this show alone
            folder_n = int(SEASON_DIR_RE.fullmatch(d.name).group(1))
            # a continuous number counts from season 1; only ever move forward, so an episode
            # numbered within its own season ("Season 2/Show - 05") stays where it is
            found, ep_in = target_season(entry, n, cache, show_dir)
            if found and found > folder_n:
                dest = season_dir(show_dir, found, show_family(entry, cache))
                if season_has_episode(dest, ep_in, n):
                    log.info(f"{show_dir.name}: {f.name}\n  episode {n} would be season {found} episode {ep_in}, "
                             f"but {dest.name} already has an episode {ep_in}: left where it is")
                    continue
            elif folder_n >= 2 and RENAME_CONTINUOUS and min(numbers[d]) > 1:
                # in the right folder, but still counted on from season 1 ("Season 2/Show - 13")
                off = folder_offset(entry, cache, show_dir, folder_n, numbers[d])
                if not off or n <= off:
                    continue
                dest, found, ep_in = d, folder_n, n - off
            else:
                continue
            new_name = (in_season_name(f.name, n, found, ep_in) if RENAME_CONTINUOUS else None) or f.name
            if dest == d and new_name == f.name:
                continue
            log.info(f"{show_dir.name}: {f.name}\n  episode {n} is season {found} episode {ep_in} -> "
                     f"{dest.name}/{new_name}")
            if plan is not None:
                plan.append((show_dir.name, f.name, d.name, dest.name, ep_in, new_name))
            # always a move: inside the library, copy/hardlink mode would leave the wrong copy behind
            for src in [f, *sidecars(f)]:
                to = dest / (new_name if src == f else renamed_sidecar(src, f, new_name))
                if to.exists():
                    log.info(f"  Already exists, skipping: {to}")
                elif dry:
                    log.info(f"  [dry run] move: {src.name} -> {to}")
                else:
                    dest.mkdir(exist_ok=True)
                    shutil.move(str(src), str(to))
                    record_move(src, to)
            if not dry and new_name != f.name:
                remember_start(show_dir, found, n - ep_in + 1)
            touched[show_dir] = entry
    if not touched:
        log.info("Every episode is already in the right season folder, numbered within its season.")
    return touched


def rename_season_folders(library, cache, dry, only=None, plan=None, style=None):
    """Rename season folders to SEASON_FOLDER_STYLE ("Season 2" ↔ "Season 2 - The Evil Eye" ↔
    "The Evil Eye"). Folders named after a season are recognised too. OVAs, specials and side
    stories keep their names. `plan` gets (show, old name, new name)."""
    touched = {}
    for show_dir in sorted(p for p in library.iterdir() if p.is_dir() and not is_link(p)):
        if only and show_dir.name not in only:
            continue
        subs = [d for d in show_dir.iterdir() if d.is_dir() and not is_link(d)
                and d.name not in (SPINOFF_FOLDER, SIDE_STORY_FOLDER)]
        if not subs:
            continue
        entry, _ = lookup(show_dir.name, False, cache)
        fam = show_family(entry, cache) if entry else None
        if not fam:
            continue
        taken = {d.name.lower() for d in subs}
        for d in sorted(subs, key=lambda x: x.name.lower()):
            c = classify_folder(d.name, fam)
            if not c or not c.get("group") or (c.get("numbered") and c["group"] < 1):
                continue
            new = season_folder_name(fam, c["group"], style, show_name=show_dir.name)
            if new == d.name:
                continue
            if new.lower() in taken:
                log.info(f"{show_dir.name}: '{d.name}' would be '{new}', but that folder exists: left as it is")
                continue
            log.info(f"{show_dir.name}: '{d.name}' -> '{new}'")
            if plan is not None:
                plan.append((show_dir.name, d.name, new))
            if not dry:
                try:
                    d.rename(show_dir / new)
                except OSError as e:
                    log.warning(f"  couldn't rename it ({e}); is it open in Explorer or Jellyfin?")
                    continue
            taken.discard(d.name.lower())
            taken.add(new.lower())
            touched[show_dir] = entry
    if not touched:
        log.info("Every season folder already has the chosen name.")
    return touched


def collect(paths, library):
    """Yield video files. The library folder itself is only scanned at the top
    level so already-sorted shows are never re-sorted."""
    for p in paths:
        p = Path(p).resolve()
        if p.is_file():
            if p.suffix.lower() in VIDEO_EXTS:
                yield p
        elif p.is_dir():
            files = p.iterdir() if p == library else p.rglob("*")
            for f in sorted(files):
                if (f.is_file() and f.suffix.lower() in VIDEO_EXTS and "sample" not in f.name.lower()
                        and ".unwanted" not in f.parts):   # qBittorrent's pieces of skipped files
                    yield f


def rename_shows(library, cache, dry):
    """Rename existing show folders to the TITLE_LANGUAGE name.
    Returns {new folder: entry} for the ones that changed, so their icons get redone."""
    renamed = {}
    for d in sorted(p for p in library.iterdir() if p.is_dir()):
        entry, _ = lookup(d.name, None, cache)
        if not entry:
            log.info(f"'{d.name}': no AniList match, leaving it")
            continue
        new_name = safe_name(preferred_name(entry))
        if new_name == d.name:
            continue
        target = library / new_name
        if target.exists() and target.resolve() != d.resolve():
            log.info(f"'{d.name}' -> '{new_name}': that folder already exists, merge these two by hand")
            continue
        if dry:
            log.info(f"[dry run] would rename '{d.name}' -> '{new_name}'")
            continue
        try:
            d.rename(target)
        except OSError as e:
            log.warning(f"Couldn't rename '{d.name}' ({e}). Is it open in Explorer or Jellyfin?")
            continue
        log.info(f"Renamed '{d.name}' -> '{new_name}'")
        renamed[target] = entry
    return renamed


# ---------------------------------------------------------------- spin-offs / side stories

FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_NOT_SET = object()


def is_link(path):
    """True for a junction or symbolic link (checked without following it)."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return os.path.islink(path) or bool(getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)


def make_junction(link, target):
    if IS_WINDOWS:
        import subprocess
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                           capture_output=True, encoding="utf-8", errors="replace",
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode != 0:
            raise OSError((r.stderr or r.stdout).strip())
    else:
        os.symlink(target, link, target_is_directory=True)


def remove_link(link):
    """Removes only the link itself, never the files it points to."""
    if IS_WINDOWS:
        os.rmdir(link)   # on a junction this deletes just the junction
    else:
        os.unlink(link)


def library_shows(library, cache):
    """{show folder: (entry, ids of every entry in its main run)}"""
    shows = {}
    for d in sorted(p for p in library.iterdir() if p.is_dir() and not is_link(p)):
        entry, _ = lookup(d.name, None, cache)
        if not entry:
            continue
        smap = season_map(entry, cache)
        shows[d] = (entry, (smap or {}).get("chain_ids") or {entry["id"]})
    return shows


def pick_main_folders(folders, shows, cache):
    """If several folders belong to the same show (e.g. a 'Season 2' that got its own
    folder), keep the one holding the first season."""
    groups = []
    for f in folders:
        for g in groups:
            if shows[f][1] & shows[g[0]][1]:
                g.append(f)
                break
        else:
            groups.append([f])
    chosen = set()
    for g in groups:
        def rank(f):
            entry = shows[f][0]
            seasons = (season_map(entry, cache) or {}).get("seasons") or []   # cached, no new requests
            first = seasons[0]["id"] if seasons else None
            return (entry["id"] != first, f.name.lower())
        chosen.add(min(g, key=rank))
    return chosen


def outgoing(chain, cache):
    """(relation type, target id) for every relation of every entry in a run."""
    edges = set()
    for mid in chain:
        info = media_relations(mid, cache)
        for t, n in (info or {}).get("relations", []):
            edges.add((t, n["id"]))
    return edges


def find_related(library, shows, cache):
    """Work out which show folders are spin-offs/side stories of which.
    Returns {child folder: (parent folder, container folder name)}."""
    edges = {d: outgoing(chain, cache) for d, (_, chain) in shows.items()}
    by_name = {d.name.lower(): d for d in shows}
    links = {}

    for child, (entry, chain) in shows.items():
        override = RELATED_OVERRIDES.get(child.name.lower(), _NOT_SET)
        if override is None:
            continue
        if override is not _NOT_SET:
            parent = by_name.get(override[0].lower())
            if parent and parent != child:
                kind = SPINOFF_FOLDER if "spin" in override[1].lower() else SIDE_STORY_FOLDER
                links[child] = (parent, kind)
            else:
                log.info(f"Override for '{child.name}': no folder named '{override[0]}'")
            continue

        candidates = {}  # parent folder -> relation types pointing at the child
        for parent, (_, pchain) in shows.items():
            if parent == child or pchain & chain:  # same show's own seasons, not a spin-off
                continue
            kinds = {t for t, target in edges[parent] if target in chain and t in ("SPIN_OFF", "SIDE_STORY")}
            child_says_parent = any(t == "PARENT" and target in pchain for t, target in edges[child])
            child_says_side = any(t in ("SPIN_OFF", "SIDE_STORY") and target in pchain for t, target in edges[child])
            if child_says_parent or (kinds and not child_says_side):
                candidates[parent] = kinds

        # Prefer parents the child itself names as PARENT, when AniList gives one
        named = {p for p in candidates if any(t == "PARENT" and target in shows[p][1] for t, target in edges[child])}
        pool = pick_main_folders(named or set(candidates), shows, cache)
        if len(pool) == 1:
            parent = pool.pop()
            kind = SPINOFF_FOLDER if "SPIN_OFF" in candidates[parent] else SIDE_STORY_FOLDER
            links[child] = (parent, kind)
        elif len(pool) > 1:
            log.info(f"'{child.name}' is related to several shows ({', '.join(p.name for p in pool)}), "
                     f"add it to RELATED_OVERRIDES to choose one")
    return links


def make_shortcut(lnk, target):
    """A Windows shortcut (.lnk) to a folder: what drives without folder links (exFAT, FAT32) get."""
    import subprocess
    q = lambda p: str(p).replace("'", "''")
    script = (f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{q(lnk)}'); "
              f"$s.TargetPath = '{q(target)}'; $s.Save()")
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                       capture_output=True, encoding="utf-8", errors="replace",
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0 or not lnk.exists():
        raise OSError((r.stderr or r.stdout or "the shortcut wasn't made").strip())


def apply_links(links, shows, dry, shortcuts=False):
    """Create missing links (junctions, or .lnk shortcuts on drives that can't hold junctions)
    and remove ones whose real folder is gone (e.g. after a rename).
    Returns the parent folders that changed."""
    changed = set()
    suffix = ".lnk" if shortcuts else ""
    wanted = {parent / kind / (child.name + suffix) for child, (parent, kind) in links.items()}

    for show in shows:
        for kind in (SPINOFF_FOLDER, SIDE_STORY_FOLDER):
            container = show / kind
            if not container.is_dir():
                continue
            for item in container.iterdir():
                # a shortcut Kura made points at the library folder with the same name
                dead_shortcut = (item.suffix.lower() == ".lnk" and item not in wanted
                                 and not (show.parent / item.stem).exists())
                if (is_link(item) and not item.exists() and item not in wanted) or dead_shortcut:
                    if dry:
                        log.info(f"[dry run] would remove broken link '{show.name}/{kind}/{item.name}'")
                    else:
                        remove_link(item) if is_link(item) else item.unlink()
                        log.info(f"Removed broken link '{show.name}/{kind}/{item.name}'")

    for child, (parent, kind) in sorted(links.items()):
        container = parent / kind
        link = container / (child.name + suffix)
        if os.path.lexists(link):
            if is_link(link) and not link.exists():   # broken: recreate it
                if not dry:
                    remove_link(link)
            else:
                continue
        if dry:
            log.info(f"[dry run] would link '{child.name}' into '{parent.name}/{kind}'")
            continue
        container.mkdir(exist_ok=True)
        ignore = container / ".ignore"   # tells Jellyfin to skip this folder
        if not ignore.exists():
            ignore.write_text("", encoding="utf-8")
            set_attrs(ignore, add=FILE_ATTRIBUTE_HIDDEN)
        try:
            if shortcuts:
                make_shortcut(link, child)
            else:
                make_junction(link, child)
        except OSError as e:
            log.warning(f"Couldn't link '{child.name}' into '{parent.name}/{kind}' ({e})")
            continue
        if not shortcuts:
            set_attrs(link, add=FILE_ATTRIBUTE_READONLY)   # lets the spin-off's own icon show
            refresh_folder(link)
        log.info(f"Linked '{child.name}' into '{parent.name}/{kind}'" + (" (shortcut)" if shortcuts else ""))
        changed.add(parent)
    return changed


def link_related(library, cache, dry):
    """Returns {parent folder: entry} for parents that got new links."""
    shows = library_shows(library, cache)
    links = find_related(library, shows, cache)
    changed = apply_links(links, shows, dry, shortcuts=not supports_links(library))
    return {p: shows[p][0] for p in changed}


def file_system(path):
    """'NTFS', 'exFAT', 'FAT32', ... of the drive a path is on ('' if unknown)."""
    if not IS_WINDOWS:
        return ""
    root = os.path.splitdrive(str(Path(path).resolve()))[0] + "\\"
    name = ctypes.create_unicode_buffer(64)
    ok = ctypes.windll.kernel32.GetVolumeInformationW(ctypes.c_wchar_p(root), None, 0, None, None, None, name, 64)
    return name.value if ok else ""


def supports_links(library):
    """Folder links (junctions) only exist on NTFS and ReFS; exFAT/FAT drives get shortcuts instead."""
    fs = file_system(library)
    return not (IS_WINDOWS and fs and fs.upper() not in ("NTFS", "REFS"))


def extras_season_label(show_dir, fam, names):
    """The season folder an extra belongs to ("Season 2", "Freezing Vibration"), from the names around it
    ("S2 Extras", the file's title "High School DxD BorN"); None when the show has fewer than two seasons or
    nothing says which."""
    folders = {}   # ("season", n) → folder name
    try:
        for d in sorted(show_dir.iterdir()):
            if not d.is_dir() or is_link(d) or d.name in (EXTRAS_FOLDER, "Specials", SPINOFF_FOLDER, SIDE_STORY_FOLDER):
                continue
            c = classify_folder(d.name, fam)
            m = re.match(r"season\s*0*(\d+)\b", d.name, re.I)   # "Season 2 Träumend" (no dash)
            if c and c.get("season"):
                folders.setdefault(("season", c["season"]), d.name)
            elif m:
                folders.setdefault(("season", int(m.group(1))), d.name)
    except OSError:
        return None
    if len(folders) < 2:
        return None
    for name in names:
        if not name:
            continue
        m = SEASON_IN_NAME.search(name)
        if m and ("season", int(next(g for g in m.groups() if g))) in folders:
            return folders[("season", int(next(g for g in m.groups() if g)))]
        c = classify_folder(name, fam) if fam else None
        if c and c.get("season") and not c.get("numbered") and ("season", c["season"]) in folders:
            return folders[("season", c["season"])]
    return None


def extras_plan(library, cache, sources=(), only=None):
    """Where every extra should go: Show / Extras / <kind> / [season] / file. Looks at the library's own Extras
    folders (flat ones from before the kinds) and at outside folders holding one folder per show
    (I:\\Anime Extras\\Claymore\\...). Only videos; S00E.. files are specials (the show's Specials folder).
    Returns [{src, dest, show, kind, season, why}]; nothing is moved."""
    plan, taken = [], set()

    def add(f, show_dir, rel_parts, fam):
        title, season, types, episode = parse(f.name)
        if season == 0 and episode is not None and not types & {"NCOP", "NCED", "OP", "ED", "PV", "CM", "EXTRA"}:
            dest_dir, kind, label = show_dir / "Specials", "special", None
        else:
            kind = extra_kind(f.name, list(reversed(rel_parts))) or "bonus"
            label = extras_season_label(show_dir, fam, [*reversed(rel_parts), title])
            dest_dir = show_dir / EXTRAS_FOLDER / EXTRA_KIND_FOLDERS[kind]
            if label:
                dest_dir = dest_dir / label
        dest = dest_dir / f.name
        n = 2
        if (dest.exists() or str(dest).lower() in taken) and rel_parts:   # "Vol 2/BD Menu.mkv": "BD Menu (Vol 2)"
            dest = dest_dir / f"{f.stem.strip()} ({rel_parts[-1]}){f.suffix}"
        while dest.exists() or str(dest).lower() in taken:   # same name twice (one OP in two folders): keep both
            dest = dest_dir / f"{f.stem} ({n}){f.suffix}"
            n += 1
        if dest == f:
            return
        taken.add(str(dest).lower())
        plan.append({"src": str(f), "dest": str(dest), "show": show_dir.name, "kind": kind, "season": label or ""})

    def family(show_dir):
        entry, _ = lookup(show_dir.name, False, cache)
        return show_family(entry, cache) if entry else None

    # 1. the library's Extras folders: files not in a kind folder yet
    for show_dir in sorted(p for p in library.iterdir() if p.is_dir() and not is_link(p)):
        if only and show_dir.name not in only:
            continue
        ex = show_dir / EXTRAS_FOLDER
        if not ex.is_dir():
            continue
        files = [f for f in ex.rglob("*") if f.is_file() and f.suffix.lower() in VIDEO_EXTS
                 and f.relative_to(ex).parts[0] not in EXTRA_KIND_FOLDERS.values()]
        if files:
            fam = family(show_dir)
            for f in sorted(files):
                add(f, show_dir, list(f.relative_to(ex).parts[:-1]), fam)
    # 2. outside folders: one folder per show
    for root in sources:
        root = Path(root)
        if not root.is_dir():
            continue
        for src_show in sorted(p for p in root.iterdir() if p.is_dir()):
            show_dir = find_show_folder(library, [src_show.name])
            if not show_dir.exists():
                entry, _ = lookup(src_show.name, False, cache)
                if entry:
                    show_dir = find_show_folder(library, [safe_name(preferred_name(entry)),
                                                          safe_name(other_name(entry) or ""), src_show.name])
            if not show_dir.exists():
                log.info(f"Extras for '{src_show.name}': no show folder for it in {library}, left where they are")
                continue
            if only and show_dir.name not in only:
                continue
            fam = family(show_dir)
            for f in sorted(src_show.rglob("*")):
                if f.is_file() and f.suffix.lower() in VIDEO_EXTS and "sample" not in f.name.lower():
                    add(f, show_dir, list(f.relative_to(src_show).parts[:-1]), fam)
    return plan


def apply_extras_plan(items, dry=False):
    """Move what extras_plan said (always a move, subtitles next to a file go along); empty folders left behind
    in an outside folder are removed. Returns how many files moved."""
    moved, emptied = 0, set()
    for it in items:
        src, dest = Path(it["src"]), Path(it["dest"])
        if not src.exists() or dest.exists():
            continue
        if dry:
            log.info(f"  [dry run] {src.name} -> {dest.parent}")
            moved += 1
            continue
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            for sc in list(sidecars(src)):
                if not (dest.parent / sc.name).exists():
                    shutil.move(str(sc), str(dest.parent / sc.name))
            shutil.move(str(src), str(dest))
            moved += 1
            log.info(f"{it['show']}: {src.name} -> {dest.parent}")
            emptied.add(src.parent)
        except OSError as e:
            log.warning(f"  couldn't move {src} ({e})")
    for d in sorted(emptied, key=lambda x: len(x.parts), reverse=True):   # folders the moves emptied
        while d.is_dir() and len(d.parts) > 2 and not any(d.iterdir()):   # never a top-level folder (I:\Anime Extras)
            try:
                d.rmdir()
            except OSError:
                break
            d = d.parent
    return moved


def remove_empty_dirs(folder, library):
    """Clean up a torrent folder after its files were moved out."""
    for d in sorted((x for x in folder.rglob("*") if x.is_dir()), key=lambda x: len(x.parts), reverse=True):
        if not any(d.iterdir()):
            d.rmdir()
    if folder != library and folder.is_dir() and not any(folder.iterdir()):
        folder.rmdir()


# ---------------------------------------------------------------- main

def setup_logging():
    handlers = [logging.FileHandler(LOG_FILE, encoding="utf-8")]
    if sys.stdout is not None:  # pythonw/pyw has no console
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO, format="%(message)s", handlers=handlers)


def main():
    ap = argparse.ArgumentParser(description="Sort anime into Show/Season folders with AniList folder icons.")
    ap.add_argument("paths", nargs="*", help="Files or folders to sort (default: current folder)")
    ap.add_argument("--library", help="Where sorted shows go (overrides LIBRARY_DIR)")
    ap.add_argument("--dry-run", action="store_true", help="Show what would happen without changing anything")
    ap.add_argument("--icons", action="store_true", help="Add missing icons to every show folder in the library")
    ap.add_argument("--force-icons", action="store_true", help="Re-download icons even if a folder has one")
    ap.add_argument("--rename", action="store_true", help="Rename existing show folders to the TITLE_LANGUAGE name")
    ap.add_argument("--link", action="store_true", help="Link spin-offs and side stories into their parent shows")
    ap.add_argument("--fix-seasons", action="store_true",
                    help="Move episodes numbered past their season's end into the right season folder")
    ap.add_argument("--rename-seasons", action="store_true",
                    help="Rename season folders to SEASON_FOLDER_STYLE (Season 2 - The Evil Eye...)")
    ap.add_argument("--sort-extras", action="store_true",
                    help="Sort extras into Extras/<kind> (Openings & Endings, Previews, Commercials, Menus, Bonus)")
    ap.add_argument("--extras-from", action="append", default=[], metavar="FOLDER",
                    help="With --sort-extras: also bring in extras from this folder (one folder per show)")
    ap.add_argument("--only", action="append", default=None, metavar="SHOW",
                    help="With --fix-seasons / --rename-seasons: only this show folder (repeat for more)")
    args = ap.parse_args()

    setup_logging()
    global ADULT_LIBRARY_DIR
    if ADULT_LIBRARY_DIR is None:   # Kura sets it; on its own (qBittorrent's hook) it comes from Kura's settings
        ADULT_LIBRARY_DIR = adult_library_from_settings()
    log.info(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} =====")

    paths = args.paths or [os.getcwd()]
    if args.library or LIBRARY_DIR:
        library = Path(args.library or LIBRARY_DIR).resolve()
    else:
        first = Path(paths[0]).resolve()
        library = first if (not args.paths and first.is_dir()) else first.parent
    library.mkdir(parents=True, exist_ok=True)

    if ONLY_PROCESS_INSIDE and args.paths:
        root = Path(ONLY_PROCESS_INSIDE).resolve()
        paths = [p for p in paths if Path(p).resolve().is_relative_to(root)]
        if not paths:
            log.info("Not inside ONLY_PROCESS_INSIDE, nothing to do.")
            return

    cache = load_cache()
    touched = {}  # show folder -> AniList entry
    folders_before = {p.name for p in library.iterdir() if p.is_dir()}

    force = args.force_icons or args.rename  # renamed folders need their icons redone

    try:
        if args.rename:
            touched = rename_shows(library, cache, args.dry_run)
        elif args.fix_seasons:
            touched = fix_seasons(library, cache, args.dry_run, only=set(args.only or []) or None)
        elif args.rename_seasons:
            rename_season_folders(library, cache, args.dry_run, only=set(args.only or []) or None)
        elif args.sort_extras:
            plan = extras_plan(library, cache, args.extras_from, only=set(args.only or []) or None)
            n = apply_extras_plan(plan, args.dry_run)
            log.info(f"{'Would move' if args.dry_run else 'Moved'} {n} extra{'s' if n != 1 else ''}.")
        elif args.link:
            pass  # handled below
        elif args.icons or args.force_icons:
            for d in sorted(p for p in library.iterdir() if p.is_dir()):
                needs_main = args.force_icons or not icon_ok(d)
                needs_seasons = SEASON_ICONS and subfolders_needing_icons(d, args.force_icons)
                if not (needs_main or needs_seasons):
                    continue
                entry, _ = lookup(d.name, None, cache)
                touched[d] = entry
        else:
            for video in list(collect(paths, library)):
                try:
                    show_dir, entry = sort_file(video, library, cache, args.dry_run)
                    if show_dir is not None:
                        touched.setdefault(show_dir, entry)
                except Exception:
                    log.exception(f"Failed on {video.name}")

            if MODE == "move" and not args.dry_run:
                for p in args.paths:
                    p = Path(p).resolve()
                    if p.is_dir() and p != library:
                        remove_empty_dirs(p, library)

        # New show folders (or renamed ones) can change who's a spin-off of whom
        new_folders = {p.name for p in library.iterdir() if p.is_dir()} - folders_before
        if (args.link or LINK_RELATED and (args.rename or new_folders)) and not (args.icons or args.force_icons):
            for parent, entry in link_related(library, cache, args.dry_run).items():
                touched.setdefault(parent, entry)   # so the new container folder gets an icon

        if MAKE_ICONS:
            icons_set = 0
            for show_dir, entry in touched.items():
                if show_dir.is_dir() or args.dry_run:
                    try:
                        icons_set += make_icon(show_dir, entry, args.dry_run, force=force)
                    except Exception:
                        log.exception(f"Icon failed for {show_dir.name}")
                    if SEASON_ICONS:
                        icons_set += make_season_icons(show_dir, entry, cache, args.dry_run, force=force)
            if icons_set and not args.dry_run:
                rebuild_icon_cache()
    finally:
        save_cache(cache)

    log.info("Done")


if __name__ == "__main__":
    main()
