"""
What are you watching right now? (like Taiga's recognition) and Discord Rich Presence.

Sources, in the order Kura trusts them:
  - Kura's built-in player (exact; handled in kura.pyw)
  - Media players: the window title of mpv, MPC-HC/BE, VLC, PotPlayer... (the file name)
  - Jellyfin: what your server says is playing on any device
  - Browsers: the active tab's title on streaming sites (Crunchyroll, HIDIVE...)

The raw text found here is turned into an anime + episode by kura.pyw (same matcher as
the torrent feed). No Qt in this file.
"""

import ctypes
import json
import os
import re
import struct
import threading
import time
import urllib.parse
import urllib.request
import uuid

# exe name (lowercase) -> name shown in settings
PLAYERS = {
    "mpv.exe": "mpv", "mpvnet.exe": "mpv.net", "mpc-hc64.exe": "MPC-HC", "mpc-hc.exe": "MPC-HC",
    "mpc-be64.exe": "MPC-BE", "mpc-be.exe": "MPC-BE", "mpc-qt.exe": "MPC-QT", "vlc.exe": "VLC",
    "potplayermini64.exe": "PotPlayer", "potplayermini.exe": "PotPlayer", "potplayer64.exe": "PotPlayer",
    "smplayer.exe": "SMPlayer", "kmplayer64x.exe": "KMPlayer", "kmplayer.exe": "KMPlayer", "gom.exe": "GOM Player",
    "bsplayer.exe": "BS.Player", "wmplayer.exe": "Windows Media Player", "zplayer.exe": "Zoom Player",
    "daumpotplayermini64.exe": "PotPlayer", "5kplayer.exe": "5KPlayer", "iina.exe": "IINA",
}
BROWSERS = {"chrome.exe": "Google Chrome", "msedge.exe": "Microsoft Edge", "firefox.exe": "Mozilla Firefox",
            "opera.exe": "Opera", "brave.exe": "Brave", "vivaldi.exe": "Vivaldi", "floorp.exe": "Floorp",
            "zen.exe": "Zen", "librewolf.exe": "LibreWolf", "waterfox.exe": "Waterfox"}
# site key -> (name, how the tab title says it's that site)
SITES = {
    "crunchyroll": ("Crunchyroll", r"crunchyroll"),
    "hidive": ("HIDIVE", r"hidive"),
    "youtube": ("YouTube", r"youtube"),
    "bilibili": ("Bilibili", r"bilibili"),
    "netflix": ("Netflix", r"netflix"),
    "primevideo": ("Prime Video", r"prime video"),
    "disneyplus": ("Disney+", r"disney\+"),
    "hulu": ("Hulu", r"hulu"),
    "adn": ("Animation Digital Network", r"animation digital network|\badn\b"),
    "tubi": ("Tubi", r"tubi"),
    "plex": ("Plex Web App", r"\bplex\b"),
    "jellyfin": ("Jellyfin Web App", r"jellyfin"),
}
PLAYER_SUFFIX = re.compile(r"\s+[-–—|]\s+(?:mpv(?:\.net)?|VLC media player|PotPlayer|MPC-HC|MPC-BE|MPC-QT|SMPlayer|"
                           r"KMPlayer|GOM Player|BS\.Player|Windows Media Player|Media Player|Zoom Player)\s*$", re.I)
VIDEO_EXT = re.compile(r"\.(mkv|mp4|avi|m4v|webm|mov|wmv|ts|m2ts)$", re.I)


# ---------------------------------------------------------------- windows

def _windows():
    """(title, exe name) of visible top-level windows."""
    if os.name != "nt":
        return []
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    out = []
    PROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def each(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if not n:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = kernel32.OpenProcess(0x1000, False, pid.value)   # PROCESS_QUERY_LIMITED_INFORMATION
        exe = ""
        if h:
            size = ctypes.c_ulong(1024)
            path = ctypes.create_unicode_buffer(1024)
            if kernel32.QueryFullProcessImageNameW(h, 0, path, ctypes.byref(size)):
                exe = os.path.basename(path.value).lower()
            kernel32.CloseHandle(h)
        out.append((buf.value, exe))
        return True
    user32.EnumWindows(PROC(each), 0)
    return out


def from_players(enabled=None):
    """[(player name, text that names the episode)] for open media player windows."""
    found = []
    for title, exe in _windows():
        if exe not in PLAYERS or (enabled is not None and PLAYERS[exe] not in enabled):
            continue
        text = PLAYER_SUFFIX.sub("", title).strip()
        text = re.sub(r"^\[(?:paused|playing|stopped)\]\s*", "", text, flags=re.I)
        if not text or text.lower() in {p.lower() for p in PLAYERS.values()}:
            continue   # player open but nothing loaded
        found.append((PLAYERS[exe], VIDEO_EXT.sub("", text)))
    return found


def from_browsers(enabled_sites=None):
    """[(site name, tab title without site/browser names)] for the active tab of browser windows."""
    found = []
    for title, exe in _windows():
        if exe not in BROWSERS:
            continue
        tab = re.sub(r"\s+[-–—]\s+(?:Google Chrome|Microsoft\u200b? Edge|Mozilla Firefox|Opera|Brave|Vivaldi|Floorp|"
                     r"Zen Browser|LibreWolf|Waterfox)\s*$", "", title).strip()
        tab = re.sub(r"\s+and \d+ more pages?$", "", tab)   # Edge adds this
        for key, (name, pattern) in SITES.items():
            if enabled_sites is not None and key not in enabled_sites:
                continue
            if re.search(pattern, tab, re.I):
                text = clean_stream_title(tab, pattern)
                if text:
                    found.append((name, text))
                break
    return found


def clean_stream_title(tab, site_pattern):
    """'Watch Frieren Episode 5 - Title - Crunchyroll' -> 'Frieren - 05' (something anitopy reads)."""
    t = re.sub(rf"\s*[-–—|:]\s*(?:watch (?:on )?)?(?:{site_pattern})[^|\-–—]*$", "", tab, flags=re.I).strip()
    t = re.sub(r"^(?:watch|ver)\s+", "", t, flags=re.I)
    m = (re.search(r"^(.+?)\s*(?:[-–—|:,]\s*)?(?:season\s*(\d+)\s*)?(?:[-–—|:,]\s*)?(?:episode|ep\.?|e)\s*(\d+)", t, re.I)
         or re.search(r"^(.+?)\s*[-–—|]?\s*s(\d+)\s*e(\d+)", t, re.I))
    if not m:
        return t if re.search(r"\d", t) else ""
    name, season, ep = m.group(1).strip(" -|:"), m.group(2), m.group(3)
    return f"{name}{f' S{int(season)}' if season and int(season) > 1 else ''} - {int(ep):02d}"


# ---------------------------------------------------------------- Jellyfin

def jellyfin_now_playing(url, api_key, user=""):
    """What Jellyfin says is playing: [{series, season, episode, title, paused, device}]."""
    url = url.rstrip("/")
    req = urllib.request.Request(f"{url}/Sessions?activeWithinSeconds=90",
                                 headers={"Authorization": f'MediaBrowser Token="{api_key}"',
                                          "X-Emby-Token": api_key, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        sessions = json.load(resp)
    out = []
    for s in sessions:
        item = s.get("NowPlayingItem")
        if not item or (user and (s.get("UserName") or "").lower() != user.lower()):
            continue
        if item.get("Type") == "Episode":
            out.append({"series": item.get("SeriesName") or "", "season": item.get("ParentIndexNumber"),
                        "episode": item.get("IndexNumber"), "title": item.get("Name") or "",
                        "paused": (s.get("PlayState") or {}).get("IsPaused", False),
                        "device": s.get("DeviceName") or s.get("Client") or ""})
        elif item.get("Type") == "Movie":
            out.append({"series": item.get("Name") or "", "season": None, "episode": 1, "title": "",
                        "paused": (s.get("PlayState") or {}).get("IsPaused", False),
                        "device": s.get("DeviceName") or ""})
    return out


# ---------------------------------------------------------------- Discord Rich Presence

class DiscordPresence(threading.Thread):
    """Talks to the Discord desktop app over its local pipe. update() just stores the newest
    activity; this thread sends it (Discord allows about one change per 15 s), reconnects
    when Discord restarts, and never blocks the app."""

    def __init__(self, client_id):
        super().__init__(daemon=True)
        self.client_id = client_id
        self.pipe = None
        self.wanted = None        # activity dict, or None to clear
        self.sent = "unset"
        self.event = threading.Event()
        self.stopping = False
        self.status = "Not connected"

    def update(self, activity):
        self.wanted = activity
        self.event.set()

    def stop(self):
        self.stopping = True
        self.wanted = None
        self.event.set()

    # -- pipe protocol: 8-byte header (op, length) + JSON
    def _send(self, op, payload):
        data = json.dumps(payload).encode()
        self.pipe.write(struct.pack("<II", op, len(data)) + data)
        self.pipe.flush()

    def _recv(self):
        head = self.pipe.read(8)
        if len(head) < 8:
            raise OSError("Discord closed the connection")
        op, n = struct.unpack("<II", head)
        return op, json.loads(self.pipe.read(n) or b"{}")

    def _connect(self):
        for i in range(10):
            try:
                self.pipe = open(rf"\\.\pipe\discord-ipc-{i}", "r+b", buffering=0)
            except OSError:
                continue
            try:
                self._send(0, {"v": 1, "client_id": str(self.client_id)})
                op, data = self._recv()
                if data.get("evt") == "READY":
                    user = (data.get("data") or {}).get("user") or {}
                    self.status = f"Connected to Discord as {user.get('global_name') or user.get('username') or 'you'}"
                    return True
                self.status = f"Discord refused: {data.get('message') or data}"
            except (OSError, ValueError) as e:
                self.status = f"Discord error: {e}"
            self._close()
        if not self.status.startswith("Discord refused"):
            self.status = "Discord isn't running"
        return False

    def _close(self):
        try:
            if self.pipe:
                self.pipe.close()
        except OSError:
            pass
        self.pipe = None

    def _apply(self, activity):
        self._send(1, {"cmd": "SET_ACTIVITY", "args": {"pid": os.getpid(), "activity": activity},
                       "nonce": str(uuid.uuid4())})
        op, data = self._recv()
        if data.get("evt") == "ERROR":
            raise ValueError((data.get("data") or {}).get("message") or "error")

    def run(self):
        last = 0
        while not self.stopping:
            self.event.wait(30)
            self.event.clear()
            if self.stopping:
                break
            wanted = self.wanted
            key = json.dumps(wanted, sort_keys=True)
            if key == self.sent:
                continue
            wait = 15 - (time.time() - last)   # Discord's rate limit
            if wait > 0 and self.sent != "unset":
                time.sleep(wait)
                wanted = self.wanted
                key = json.dumps(wanted, sort_keys=True)
            if self.pipe is None and not self._connect():
                time.sleep(10)
                self.event.set()   # try again later
                continue
            try:
                try:
                    self._apply(wanted)
                except ValueError:
                    # older Discord: retry without the newer fields
                    if wanted:
                        wanted = {k: v for k, v in wanted.items() if k not in ("status_display_type", "type")}
                    self._apply(wanted)
                self.sent, last = key, time.time()
            except (OSError, ValueError) as e:
                self.status = f"Discord error: {e}"
                self._close()
                time.sleep(5)
                self.event.set()
        try:
            if self.pipe:
                self._apply(None)
        except (OSError, ValueError):
            pass
        self._close()


def activity_for(title, episode, total=None, group="", cover="", url="", started=None, username="",
                 show_group=True, show_elapsed=True, show_username=True, paused=False):
    """The 'Watching <anime>' card, like Taiga's."""
    state = f"Episode {episode}" + (f" of {total}" if total else "") if episode else "Watching"
    if group and show_group:
        state += f" · {group}"
    act = {"type": 3, "status_display_type": 2, "details": title[:128], "state": state[:128]}
    if paused:
        act["state"] = (state + " · Paused")[:128]
    elif started and show_elapsed:
        act["timestamps"] = {"start": int(started)}
    assets = {}
    if cover:
        assets["large_image"] = cover
        assets["large_text"] = (f"{username} · " if username and show_username else "") + title[:100]
    if assets:
        act["assets"] = assets
    if url:
        act["buttons"] = [{"label": "View on AniList", "url": url}]
    return act
