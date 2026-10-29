"""
AniList account support for Kura: logging in, reading your
anime list and saving changes to it.

Requests go through anime_organizer's pacing (_throttle), so the app, a running
sort job and these calls together never go over AniList's rate limit. Your whole
list comes back in one request and is kept in anilist_list.json; edits are sent
after a short pause so clicking "+" five times is one request, not five.

The login token is encrypted with Windows DPAPI (only your Windows account can
read it) and stored in anilist_token.bin.
"""

import base64
import ctypes
import json
import os
import time
import urllib.error
import urllib.request
from datetime import date

from PySide6.QtCore import QObject, QThread, QTimer, Signal

import anime_organizer as org

TOKEN_FILE = org.SCRIPT_DIR / "anilist_token.bin"
LIST_FILE = org.LIST_FILE
DEVELOPER_URL = "https://anilist.co/settings/developer"
REDIRECT_URL = "https://anilist.co/api/v2/oauth/pin"   # AniList shows the token so it can be pasted


def authorize_url(client_id):
    return f"https://anilist.co/api/v2/oauth/authorize?client_id={client_id}&response_type=token"


STATUS_ORDER = ["CURRENT", "PLANNING", "COMPLETED", "REPEATING", "PAUSED", "DROPPED"]
STATUS_NAMES = {"CURRENT": "Watching", "PLANNING": "Planning", "COMPLETED": "Completed",
                "REPEATING": "Rewatching", "PAUSED": "Paused", "DROPPED": "Dropped"}

# Saved with each list entry, so pages can show a show without asking AniList again
MEDIA_KEYS = ("id", "format", "status", "episodes", "season", "seasonYear", "siteUrl", "title",
              "coverImage", "nextAiringEpisode", "synonyms", "isAdult", "genres")

# Kura's two sides (Anime / 18+): pages list the entries of the side you're on (Account.view)
ADULT = {"on": False}


def is_adult(media):
    """AniList's 18+ flag; entries synced before Kura asked for it: the Hentai genre."""
    m = media or {}
    if m.get("isAdult") is not None:
        return bool(m["isAdult"])
    return "Hentai" in (m.get("genres") or [])

ENTRY_FIELDS = ("id mediaId status progress score repeat notes updatedAt "
                "startedAt { year month day } completedAt { year month day }")
MEDIA_FIELDS = ("id format status episodes season seasonYear siteUrl isAdult title { romaji english } synonyms "
                "coverImage { extraLarge } nextAiringEpisode { episode airingAt } "
                "duration genres studios(isMain: true) { nodes { name isAnimationStudio } }")   # last three: Statistics (live)
QUERY_VIEWER = "query { Viewer { id name siteUrl avatar { medium } mediaListOptions { scoreFormat } } }"
QUERY_LIST = ("query ($userId: Int, $chunk: Int) { MediaListCollection(userId: $userId, type: ANIME, "
              "chunk: $chunk, perChunk: 500) { hasNextChunk lists { isCustomList entries { "
              + ENTRY_FIELDS + " media { " + MEDIA_FIELDS + " } } } } }")
MUTATION_DELETE = "mutation ($id: Int) { DeleteMediaListEntry(id: $id) { deleted } }"
FIELD_TYPES = {"status": "MediaListStatus", "progress": "Int", "score": "Float", "repeat": "Int",
               "notes": "String", "startedAt": "FuzzyDateInput", "completedAt": "FuzzyDateInput"}


def save_mutation(fields):
    params = "".join(f", ${k}: {FIELD_TYPES[k]}" for k in fields)
    args = ", ".join(f"{k}: ${k}" for k in fields)
    return (f"mutation ($mediaId: Int{params}) {{ SaveMediaListEntry(mediaId: $mediaId, {args}) "
            f"{{ {ENTRY_FIELDS} }} }}")


# ---------------------------------------------------------------- requests

class AuthError(Exception):
    """The token is missing, expired or was revoked."""


class ApiError(Exception):
    pass


def gql(query, variables=None, token=None, tries=3):
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": org.USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for attempt in range(tries):
        org._throttle()
        req = urllib.request.Request(org.ANILIST_URL, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(int(e.headers.get("Retry-After", 60)))
                continue
            try:
                msg = (json.loads(e.read()).get("errors") or [{}])[0].get("message") or ""
            except ValueError:
                msg = ""
            if e.code == 401 or "invalid token" in msg.lower() or "unauthorized" in msg.lower():
                raise AuthError(msg or "Not logged in")
            raise ApiError(msg or f"AniList error {e.code}")
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == tries - 1:
                raise ApiError(f"Can't reach AniList ({e})")
            time.sleep(3)
            continue
        if payload.get("errors"):
            msg = payload["errors"][0].get("message") or "AniList error"
            if "invalid token" in msg.lower() or "unauthorized" in msg.lower():
                raise AuthError(msg)
            raise ApiError(msg)
        return payload.get("data") or {}
    raise ApiError("AniList is busy (rate limit). Try again in a minute.")


def fetch_list(token, user_id):
    """{media id: entry with media} for your whole anime list (custom lists skipped)."""
    entries, chunk = {}, 1
    while True:
        data = gql(QUERY_LIST, {"userId": user_id, "chunk": chunk}, token=token).get("MediaListCollection") or {}
        for lst in data.get("lists") or []:
            if lst.get("isCustomList"):
                continue
            for e in lst.get("entries") or []:
                entries[e["mediaId"]] = e
        if not data.get("hasNextChunk"):
            return entries
        chunk += 1


# ---------------------------------------------------------------- token storage

def _dpapi(data, protect):
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    inp = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = BLOB()
    crypt = ctypes.windll.crypt32
    if protect:
        ok = crypt.CryptProtectData(ctypes.byref(inp), "Kura AniList token", None, None, None, 0, ctypes.byref(out))
    else:
        ok = crypt.CryptUnprotectData(ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out))
    if not ok:
        raise OSError("Windows couldn't encrypt/decrypt the AniList token")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        free = ctypes.windll.kernel32.LocalFree
        free.argtypes = [ctypes.c_void_p]
        free(ctypes.cast(out.pbData, ctypes.c_void_p))


def save_token(token):
    raw = token.encode()
    data = _dpapi(raw, True) if org.IS_WINDOWS else base64.b64encode(raw)
    TOKEN_FILE.write_bytes(data)


def load_token():
    try:
        data = TOKEN_FILE.read_bytes()
        return (_dpapi(data, False) if org.IS_WINDOWS else base64.b64decode(data)).decode()
    except (OSError, ValueError):
        return None


def today():
    d = date.today()
    return {"year": d.year, "month": d.month, "day": d.day}


def format_date(fd):
    if not fd or not fd.get("year"):
        return ""
    try:
        return date(fd["year"], fd.get("month") or 1, fd.get("day") or 1).strftime(
            "%d %b %Y" if fd.get("day") else "%b %Y" if fd.get("month") else "%Y").lstrip("0")
    except ValueError:
        return str(fd["year"])


# score format -> (maximum, decimals)
SCORE_RANGES = {"POINT_100": (100, 0), "POINT_10_DECIMAL": (10, 1), "POINT_10": (10, 0),
                "POINT_5": (5, 0), "POINT_3": (3, 0)}


def format_score(score, fmt):
    if not score:
        return ""
    if fmt == "POINT_5":
        return "★" * int(score)
    if fmt == "POINT_3":
        return {1: ":(", 2: ":|", 3: ":)"}.get(int(score), "")
    if fmt == "POINT_10_DECIMAL":
        return f"{score:.1f}"
    return str(int(score))


# ---------------------------------------------------------------- account

class Worker(QThread):
    done = Signal(object, object)   # result, error

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self):
        try:
            self.done.emit(self.fn(), None)
        except Exception as e:
            self.done.emit(None, e)


PROGRESS_SCALE = {}   # {media id: AniList episodes per file}: two-in-one episodes (Kura's Edit info)


def kura_units(e):
    """Your progress the way Kura counts: in files when each holds several of AniList's episodes ("progress" =
    files watched, "progress_anilist" = what AniList has). Run on every entry that comes from AniList or the file."""
    s = PROGRESS_SCALE.get(e.get("mediaId")) or 1
    raw = e["progress_anilist"] if "progress_anilist" in e else (e.get("progress") or 0)
    if s > 1:
        e["progress_anilist"], e["progress"] = raw, (raw or 0) // s
    elif "progress_anilist" in e:
        e["progress"] = e.pop("progress_anilist")
    return e


class Account(QObject):
    changed = Signal()            # signed in/out or the whole list reloaded
    entry_changed = Signal(int)   # one entry edited (media id)
    message = Signal(str)         # something to tell the user (errors mostly)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.workers = set()
        self.pending, self.timers = {}, {}
        self.refreshing = False
        self.token = load_token()
        self.user, self.entries, self.fetched = None, {}, 0
        try:
            data = json.loads(LIST_FILE.read_text(encoding="utf-8"))
            self.user, self.fetched = data.get("user"), data.get("fetched", 0)
            self.entries = {int(k): v for k, v in (data.get("entries") or {}).items()}
        except (OSError, ValueError):
            pass
        self.rescale()

    def rescale(self):
        """Two-in-one episodes changed (PROGRESS_SCALE): count every entry again."""
        for e in self.entries.values():
            kura_units(e)

    @property
    def connected(self):
        return bool(self.token and self.user)

    @property
    def score_format(self):
        return ((self.user or {}).get("mediaListOptions") or {}).get("scoreFormat") or "POINT_10"

    def entry(self, media_id):
        e = self.entries.get(media_id)
        return e if e and e.get("status") else None

    def view(self):
        """The entries of the side Kura is on (Anime or 18+): what pages list."""
        on = ADULT["on"]
        return {mid: e for mid, e in self.entries.items() if is_adult(e.get("media")) == on}

    # ---- plumbing
    def _run(self, fn, on_done):
        w = Worker(fn)
        w.done.connect(on_done)
        w.finished.connect(lambda: self.workers.discard(w))
        self.workers.add(w)
        w.start()

    def _save_file(self):
        data = {"user": self.user, "fetched": self.fetched,
                "entries": {str(k): v for k, v in self.entries.items() if v.get("status")}}
        tmp = LIST_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, LIST_FILE)
        org._list_ids = None   # title matching picks up the new list

    def _error(self, err):
        if isinstance(err, AuthError):
            self.token = None
            TOKEN_FILE.unlink(missing_ok=True)
            self.message.emit("Your AniList login expired or was revoked. Reconnect in Settings.")
            self.changed.emit()
        else:
            self.message.emit(f"AniList: {err}")

    # ---- sign in / out
    def sign_in(self, token, callback):
        """callback(error text or None)"""
        token = token.strip()

        def done(viewer, err):
            if err:
                callback(str(err) if not isinstance(err, AuthError) else "That token didn't work. Copy the whole thing and try again.")
                return
            save_token(token)
            self.token, self.user = token, viewer
            self.fetched = 0
            self._save_file()
            callback(None)
            self.changed.emit()
            self.refresh()
        self._run(lambda: gql(QUERY_VIEWER, token=token)["Viewer"], done)

    def sign_out(self):
        for t in self.timers.values():
            t.stop()
        self.pending.clear()
        self.token, self.user, self.entries, self.fetched = None, None, {}, 0
        TOKEN_FILE.unlink(missing_ok=True)
        LIST_FILE.unlink(missing_ok=True)
        org._list_ids = None
        self.changed.emit()

    def refresh(self, max_age=0):
        """Reload the whole list (one request). max_age: skip if it's newer than this many seconds."""
        if not self.connected or self.refreshing or self.pending:
            return
        if max_age and time.time() - self.fetched < max_age:
            return
        self.refreshing = True
        token, uid = self.token, self.user["id"]

        def done(result, err):
            self.refreshing = False
            if err:
                self._error(err)
                return
            self.user, self.entries = result
            self.rescale()
            self.fetched = time.time()
            self._save_file()
            self.changed.emit()
        self._run(lambda: (gql(QUERY_VIEWER, token=token)["Viewer"], fetch_list(token, uid)), done)

    # ---- editing
    def save(self, media_id, media=None, **fields):
        """Change your entry for one show. The app updates at once; AniList gets the
        change after a short pause so quick clicks become one request."""
        e = self.entries.get(media_id)
        if e is None:
            e = self.entries[media_id] = {"mediaId": media_id, "status": None, "progress": 0, "score": 0,
                                          "repeat": 0, "startedAt": {}, "completedAt": {}, "media": {}}
        if media and not e.get("media"):
            e["media"] = {k: media.get(k) for k in MEDIA_KEYS}
        e.update(fields)
        send = dict(fields)
        scale = PROGRESS_SCALE.get(media_id) or 1
        if "progress" in fields and scale > 1:   # files watched → AniList's episodes (file 3 of 2-in-1 = 6)
            send["progress"] = e["progress_anilist"] = fields["progress"] * scale
        self.pending.setdefault(media_id, {}).update(send)
        self.entry_changed.emit(media_id)
        timer = self.timers.get(media_id)
        if timer is None:
            timer = self.timers[media_id] = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(900)
            timer.timeout.connect(lambda m=media_id: self._commit(m))
        timer.start()

    def _commit(self, media_id):
        fields = self.pending.pop(media_id, None)
        if not fields:
            return
        token = self.token

        def done(result, err):
            if err:
                self._error(err)
                self.refresh()
                return
            e = self.entries.get(media_id)
            if e is not None:
                newer = self.pending.get(media_id, {})
                if "progress" in result and "progress" not in newer:   # AniList's count: back to Kura's
                    e.pop("progress_anilist", None)
                e.update({k: v for k, v in result.items() if k not in newer})
                kura_units(e)
            self._save_file()
            self.entry_changed.emit(media_id)
        self._run(lambda: gql(save_mutation(fields), {"mediaId": media_id, **fields}, token=token)["SaveMediaListEntry"],
                  done)

    def set_progress(self, media_id, n, media=None):
        """Set episodes watched, moving the status along the way AniList's site does."""
        e = self.entries.get(media_id) or {}
        media = media or e.get("media") or {}
        status = e.get("status")
        total = media.get("episodes")
        n = max(0, min(n, total) if total else n)
        if n == 0 and not status:
            return
        fields = {"progress": n}
        if total and n >= total:
            if status != "COMPLETED":
                fields["status"] = "COMPLETED"
                if status == "REPEATING":
                    fields["repeat"] = (e.get("repeat") or 0) + 1
                if not (e.get("completedAt") or {}).get("year"):
                    fields["completedAt"] = today()
        elif n > 0 and status in (None, "PLANNING", "PAUSED", "DROPPED", "COMPLETED"):
            fields["status"] = "CURRENT"
        if n > 0 and not (e.get("startedAt") or {}).get("year"):
            fields["startedAt"] = today()
        self.save(media_id, media, **fields)

    def set_status(self, media_id, status, media=None):
        e = self.entries.get(media_id) or {}
        media = media or e.get("media") or {}
        fields = {"status": status}
        if status == "COMPLETED":
            if media.get("episodes"):
                fields["progress"] = media["episodes"]
            if not (e.get("completedAt") or {}).get("year"):
                fields["completedAt"] = today()
        if status in ("CURRENT", "REPEATING") and not (e.get("startedAt") or {}).get("year"):
            fields["startedAt"] = today()
        if status == "REPEATING":
            fields["progress"] = 0
        self.save(media_id, media, **fields)

    def remove(self, media_id):
        e = self.entries.pop(media_id, None)
        self.pending.pop(media_id, None)
        if media_id in self.timers:
            self.timers[media_id].stop()
        self.entry_changed.emit(media_id)
        if not e or not e.get("id"):
            self._save_file()
            return
        token = self.token

        def done(_, err):
            if err:
                self._error(err)
                self.refresh()
            else:
                self._save_file()
        self._run(lambda: gql(MUTATION_DELETE, {"id": e["id"]}, token=token), done)

    def flush(self):
        """Send any changes still waiting (called when the app closes)."""
        for media_id in list(self.pending):
            self.timers[media_id].stop()
            fields = self.pending.pop(media_id)
            try:
                gql(save_mutation(fields), {"mediaId": media_id, **fields}, token=self.token)
            except Exception:
                pass
        for w in list(self.workers):
            w.wait(5000)
