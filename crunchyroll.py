"""
Crunchyroll account access for Kura's "Crunchyroll import" tool (idea from
ruflas/CrunchyExporter, MIT): your watch history and watchlist, read from the same
private API the Crunchyroll website uses.

Sign-in: the website's `etp_rt` cookie is traded once for a refresh token, which is kept
encrypted (DPAPI) in crunchyroll_token.bin; the cookie itself is never stored.
No Qt in here; kura.pyw does the matching to AniList and the UI.
"""

import base64
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import anime_organizer as org

API = "https://www.crunchyroll.com"
# The website's public client (no secret); the same one open-source Crunchyroll tools use
CLIENT_ID = "noaihdevm_6iyg0a8l0q"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130.0 Safari/537.36")
TOKEN_FILE = org.SCRIPT_DIR / "crunchyroll_token.bin"      # refresh token + device, encrypted
HISTORY_FILE = org.SCRIPT_DIR / "crunchyroll_history.json"  # every episode you've played
MAP_FILE = org.SCRIPT_DIR / "crunchyroll_map.json"          # your own season → AniList choices
WATCHED_SHARE = 0.85   # a played episode counts as watched from here (or when CR says fully watched)


class CRError(Exception):
    pass


class CRAuthError(CRError):
    """The sign-in is missing, expired or was revoked."""


# ---------------------------------------------------------------- storage

def _protect(data, protect):
    if not org.IS_WINDOWS:
        return base64.b64encode(data) if protect else base64.b64decode(data)
    import anilist_account as acct
    return acct._dpapi(data, protect)


def load_creds():
    try:
        return json.loads(_protect(TOKEN_FILE.read_bytes(), False).decode())
    except (OSError, ValueError):
        return None


def save_creds(creds):
    TOKEN_FILE.write_bytes(_protect(json.dumps(creds).encode(), True))


def forget():
    TOKEN_FILE.unlink(missing_ok=True)


def _read(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def load_history():
    return _read(HISTORY_FILE, {"episodes": {}, "synced": 0})


def load_map():
    return {k: v for k, v in _read(MAP_FILE, {}).items()}


def save_map(mapping):
    _write(MAP_FILE, mapping)


# ---------------------------------------------------------------- API

class Crunchyroll:
    def __init__(self, creds=None):
        self.creds = creds if creds is not None else load_creds()
        # the short-lived access token is kept with the sign-in, so a sync right after
        # signing in (or another part of Kura) doesn't have to renew it
        self.access = (self.creds or {}).get("access")
        self.expires = (self.creds or {}).get("expires", 0)

    @property
    def signed_in(self):
        return bool(self.creds and self.creds.get("refresh_token"))

    def _request(self, url, data=None, headers=None, cookie=None, want_cookie=False):
        h = {"User-Agent": UA, "Accept": "application/json", **(headers or {})}
        if cookie:
            h["Cookie"] = cookie
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        req = urllib.request.Request(url, data=body, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read() or b"{}")
                if want_cookie:   # the website gets its renewed etp_rt as a cookie
                    for c in resp.headers.get_all("Set-Cookie") or []:
                        m = re.match(r"\s*etp_rt=([^;]+)", c)
                        if m:
                            result["_etp_rt"] = m.group(1)
                return result
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")[:300]
            log_error(f"{e.code} from {urllib.parse.urlparse(url).path}: {text}")
            if "/auth/" in url and e.code in (400, 401, 403):
                if "unsupported_grant_type" in text or "invalid_request" in text and "missing" in text:
                    raise CRError(f"Crunchyroll refused Kura's request ({text[:160]})") from e   # Kura's bug, not you
                raise CRAuthError(_auth_message(text)) from e
            if e.code == 401:
                raise CRAuthError("Crunchyroll signed Kura out. Sign in again with a fresh cookie.") from e
            raise CRError(f"Crunchyroll answered {e.code}: {text}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise CRError(f"Can't reach Crunchyroll ({e})") from e

    def _token(self, etp_rt, device):
        """The website's way: the etp_rt cookie (first the pasted one, later the one Crunchyroll
        gave back) buys an access token and a renewed etp_rt. Crunchyroll refuses the
        refresh_token grant for this client."""
        basic = base64.b64encode(f"{CLIENT_ID}:".encode()).decode()
        tok = self._request(f"{API}/auth/v1/token",
                            {"grant_type": "etp_rt_cookie", "scope": "offline_access", "device_id": device,
                             "device_name": "Kura", "device_type": "com.crunchyroll.desktop.windows"},
                            {"Authorization": f"Basic {basic}"}, cookie=f"etp_rt={etp_rt}", want_cookie=True)
        self.access, self.expires = tok["access_token"], time.time() + int(tok.get("expires_in", 300)) - 30
        return tok.get("_etp_rt") or tok.get("refresh_token") or etp_rt

    def _store(self):
        self.creds["access"], self.creds["expires"] = self.access, self.expires
        save_creds(self.creds)

    def sign_in(self, etp_rt):
        """Trade the website's etp_rt cookie for Kura's own sign-in."""
        etp_rt = etp_rt.strip().removeprefix("etp_rt=").strip().strip('"').strip(";")
        if not etp_rt:
            raise CRAuthError("Paste the etp_rt cookie first.")
        device = str(uuid.uuid4())
        renewal = self._token(etp_rt, device)
        me = self.get("/accounts/v1/me")
        try:
            profile = self.get("/accounts/v1/me/profile")
        except CRError:
            profile = {}
        self.creds = {"refresh_token": renewal, "device_id": device, "account_id": me.get("account_id"),
                      "name": profile.get("profile_name") or profile.get("username") or ""}
        self._store()
        return self.creds

    def _ensure(self):
        if self.access and time.time() < self.expires:
            return
        if not self.signed_in:
            raise CRAuthError("Not signed in to Crunchyroll.")
        try:
            renewal = self._token(self.creds["refresh_token"], self.creds.get("device_id", ""))
        except CRAuthError:
            # Another part of Kura may have renewed it meanwhile (it rotates): use the saved one
            saved = load_creds()
            if not saved or saved.get("refresh_token") == self.creds.get("refresh_token"):
                raise CRAuthError("Crunchyroll ended Kura's sign-in. Sign in again with a fresh cookie.")
            self.creds = saved
            renewal = self._token(self.creds["refresh_token"], self.creds.get("device_id", ""))
        self.creds["refresh_token"] = renewal
        self._store()

    def get(self, path, params=None):
        if not self.access or time.time() >= self.expires:
            self._ensure()
        url = path if path.startswith("http") else API + path
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        return self._request(url, headers={"Authorization": f"Bearer {self.access}"})

    # ---- your data
    def history_pages(self, locale="en-US", page_size=100):
        """Watch history, newest first, one page at a time (Crunchyroll uses a cursor)."""
        url = f"/content/v2/{self.creds['account_id']}/watch-history"
        params = {"page_size": page_size, "locale": locale}
        seen = set()
        while url and url not in seen:
            seen.add(url)
            body = self.get(url, params)
            items = body.get("data") or []
            yield [e for e in (parse_item(i) for i in items) if e]
            nxt = (body.get("meta") or {}).get("next_page") or ""
            if not items or not nxt:
                return
            url, params = nxt, None

    def sync_history(self, progress=None, locale="en-US"):
        """New plays since the last sync are added to crunchyroll_history.json (all of it the first
        time). Stops at the first page with nothing new. Returns (store, new count)."""
        store = load_history()
        eps = store.setdefault("episodes", {})
        new = 0
        for n, page in enumerate(self.history_pages(locale), 1):
            changed = 0
            for e in page:
                old = eps.get(e["episode_id"])
                if old and (old["played"], old["playhead"]) == (e["played"], e["playhead"]):
                    continue
                if old is None:
                    new += 1
                else:
                    # keep the earliest play (when you started) and "watched" once it was
                    e["first_played"] = min(filter(None, (old.get("first_played"), e["played"])), default="")
                    e["watched"] = e["watched"] or old.get("watched", False)
                eps[e["episode_id"]] = e
                changed += 1
            if progress:
                progress(n, len(eps))
            if store.get("synced") and not changed:
                break
        store["synced"] = time.time()
        _write(HISTORY_FILE, store)
        return store, new

    def watchlist(self, locale="en-US"):
        """Series on your Crunchyroll watchlist: [{series_id, title, never_watched}]."""
        out, start = {}, 0
        while True:
            body = self.get(f"/content/v2/discover/{self.creds['account_id']}/watchlist",
                            {"order": "desc", "n": 100, "start": start, "locale": locale})
            items = body.get("data") or []
            for i in items:
                panel = i.get("panel") or {}
                meta = panel.get("episode_metadata") or {}
                sid = meta.get("series_id") or (panel.get("id") if panel.get("type") == "series" else None)
                title = meta.get("series_title") or panel.get("title") or ""
                if sid and sid not in out:
                    out[sid] = {"series_id": sid, "title": title, "never_watched": bool(i.get("never_watched"))}
            total = (body.get("total") if isinstance(body.get("total"), int) else 0) or 0
            start += len(items)
            if not items or start >= total:
                return list(out.values())

    def latest(self, locale="en-US"):
        """The most recent play (for "now watching")."""
        body = self.get(f"/content/v2/{self.creds['account_id']}/watch-history", {"page_size": 1, "locale": locale})
        items = [e for e in (parse_item(i) for i in body.get("data") or []) if e]
        return items[0] if items else None


LOG_FILE = org.SCRIPT_DIR / "crunchyroll.log"


def log_error(text):
    """Crunchyroll's error answers (never tokens), for figuring out what went wrong."""
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {text}\n")
    except OSError:
        pass


def _auth_message(text):
    t = text.lower()
    if "etp_rt" in t or "invalid_grant" in t or "unauthorized" in t or "invalid" in t:
        return ("Crunchyroll didn't accept that cookie. Copy a fresh etp_rt value while signed in to "
                "crunchyroll.com, and try again.")
    return f"Crunchyroll sign-in failed ({text[:120]})"


# ---------------------------------------------------------------- history → seasons

DUB = re.compile(r"\s*\((?:[A-Za-z\- ]+ )?(?:Dub|Dubbed|Sub|Subtitled)\)\s*$|\s*\(\s*[A-Z][a-z]+(?:-[A-Za-z]+)? Dub\s*\)",
                 re.IGNORECASE)


def clean_title(title):
    """'SPY x FAMILY Season 2 (English Dub)' → 'SPY x FAMILY Season 2'."""
    return DUB.sub("", title or "").strip()


def parse_item(item):
    panel = item.get("panel") or {}
    meta = panel.get("episode_metadata") or {}
    kind = panel.get("type") or ""
    if not panel or kind not in ("episode", "movie", ""):
        return None
    if kind == "movie":
        mm = panel.get("movie_listing_metadata") or {}
        meta = {**meta, "series_id": meta.get("series_id") or panel.get("id"),
                "series_title": meta.get("series_title") or panel.get("title"),
                "season_id": panel.get("id"), "season_title": panel.get("title"), "episode_number": 1,
                "duration_ms": mm.get("duration_ms") or meta.get("duration_ms")}
    try:
        number = float(meta.get("episode_number")) if meta.get("episode_number") is not None else None
    except (TypeError, ValueError):
        number = None
    duration = (meta.get("duration_ms") or 0) / 1000
    playhead = item.get("playhead") or 0
    watched = bool(item.get("fully_watched")) or (duration > 0 and playhead >= duration * WATCHED_SHARE)
    played = item.get("date_played") or ""
    return {"episode_id": panel.get("id") or "", "series_id": meta.get("series_id") or "",
            "series_title": meta.get("series_title") or "", "season_id": meta.get("season_id") or "",
            "season_title": clean_title(meta.get("season_title") or ""),
            "season_number": meta.get("season_number") or meta.get("season_sequence_number"),
            "episode": int(number) if number is not None and number == int(number) else None,
            "sequence": meta.get("sequence_number"), "movie": kind == "movie",
            "watched": watched, "played": played, "first_played": played,
            "playhead": playhead, "duration": duration}


def seasons(store):
    """The history grouped per Crunchyroll season: episodes watched and when.
    Dubs of the same season are separate seasons on Crunchyroll; their titles clean to the same."""
    out = {}
    for e in store.get("episodes", {}).values():
        if not e.get("watched") or not e.get("episode"):
            continue
        key = e["season_id"] or e["series_id"]
        s = out.setdefault(key, {"season_id": key, "series_id": e["series_id"], "series_title": e["series_title"],
                                 "season_title": e["season_title"] or e["series_title"],
                                 "season_number": e.get("season_number"), "movie": e.get("movie"),
                                 "episodes": set(), "first": e["first_played"], "last": e["played"]})
        s["episodes"].add(e["episode"])
        s["first"] = min(filter(None, (s["first"], e["first_played"])), default="")
        s["last"] = max(s["last"] or "", e["played"] or "")
    return list(out.values())


def to_fuzzy(iso):
    """'2024-03-05T12:00:00Z' → AniList's {year, month, day}."""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", iso or "")
    return {"year": int(m[1]), "month": int(m[2]), "day": int(m[3])} if m else None
