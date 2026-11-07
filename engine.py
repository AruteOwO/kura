"""Kura's own torrent engine for Play now / Watch (libtorrent), user's choice Nov 3 2026.

Streaming through qBittorrent's Web UI couldn't say "this piece next" (a missing first piece, a jump ahead and a
batch all waited for the in-order download) and new torrents queued behind its active downloads. This engine only
streams: it downloads just the episode you watch, the pieces the player needs next get deadlines, and when the
episode is done (or you stop watching, or Kura closes) the torrent is handed to qBittorrent with the data already
on disk: it checks it, seeds or gets the rest of a batch, and Kura files it like any download.

`EngineClient` answers the same calls as torrents.QBit that stream.Stream makes (info / files / properties /
piece_states / file_priority / set_sequential), plus `want` (deadlines for what the player reads next). No Qt here.

It listens for peers like any client (DHT, UDP trackers, incoming connections, UPnP / NAT-PMP): Windows asks once
to let Kura through the firewall (user's choice, Nov 3: the first version didn't listen, found few peers and
crawled). It follows qBittorrent's network interface and proxy when those are set (a VPN binding still applies).

Since Nov 3 (user's pick, like Hayase) a stream isn't handed to qBittorrent: it downloads into "Kura stream" and is
either kept (filed into the library by Kura's organizer) or deleted when you stop watching (`drop`).
"""
import threading
import time
import urllib.request
from pathlib import Path

try:
    import libtorrent as lt
except Exception:   # not installed / didn't load: Kura streams through qBittorrent as before
    lt = None

QB_TO_LT = {0: 0, 1: 4, 6: 6, 7: 7}   # qBittorrent's file priorities -> libtorrent's (4 = normal)
LT_TO_QB = {0: 0, 4: 1, 6: 6, 7: 7}
WINDOW_BYTES = 48 << 20               # what gets a deadline ahead of the player
WINDOW_MIN = 6                        # pieces, however big they are


def available():
    return lt is not None


class EngineClient:
    """One libtorrent session for every stream. Thread-safe enough for Kura's use (libtorrent's handles are)."""

    def __init__(self, qbit_prefs=None, user_agent="Kura"):
        p = qbit_prefs or {}
        addr = (p.get("current_interface_address") or "").strip()
        s = {"listen_interfaces": f"{addr}:0" if addr else "0.0.0.0:0,[::]:0", "enable_dht": True,
             "enable_lsd": True, "enable_upnp": True, "enable_natpmp": True, "user_agent": user_agent, "alert_mask": 0,
             "dht_bootstrap_nodes": p.get("dht_bootstrap_nodes") or
             "dht.libtorrent.org:25401,router.bittorrent.com:6881,dht.transmissionbt.com:6881",
             "connections_limit": 200, "active_downloads": -1, "active_seeds": -1, "active_limit": -1,
             "request_timeout": 10, "piece_timeout": 8, "peer_connect_timeout": 7,
             "strict_end_game_mode": False}   # end game early: the last blocks of a deadline piece from any peer
        if addr:   # qBittorrent is bound to an interface (a VPN): so is this
            s["outgoing_interfaces"] = addr
        ptype = (p.get("proxy_type") or "None")
        if p.get("proxy_ip") and ptype not in ("None", 0, -1) and p.get("proxy_bittorrent", True):
            kinds = {"SOCKS4": 1, "SOCKS5": 2, "HTTP": 4, 1: 4, 2: 2, 3: 2, 4: 1}   # libtorrent proxy_type values
            s.update(proxy_hostname=p["proxy_ip"], proxy_port=int(p.get("proxy_port") or 0),
                     proxy_type=kinds.get(ptype, 2), proxy_peer_connections=True, proxy_tracker_connections=True)
            if p.get("proxy_auth_enabled"):
                s.update(proxy_username=p.get("proxy_username") or "", proxy_password=p.get("proxy_password") or "",
                         proxy_type=5 if kinds.get(ptype, 2) == 2 else 6)   # socks5_pw / http_pw
        self.ses = lt.session(s)
        self.torrents = {}       # hash -> {"h": handle, "raw": .torrent bytes or None, "save": path, "primed"}
        self.lock = threading.Lock()
        self.windows = {}        # hash -> first piece of the deadline window last set

    # ---- adding
    @staticmethod
    def fetch_torrent(url, timeout=20):
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()

    def add(self, h, save_path, link="", magnet="", raw=None):
        """A release: its .torrent (no wait for the file list), its magnet, or saved .torrent bytes (a kept stream
        going on after a restart: what's on disk is checked and kept). Nothing downloads until a stream picks its
        file (every file starts at priority 0)."""
        h = h.lower()
        with self.lock:
            if h in self.torrents:
                return h
        if not raw and link and link.lower().split("?")[0].endswith(".torrent"):
            try:
                raw = self.fetch_torrent(link)
            except Exception:
                raw = None
        if raw:
            atp = lt.add_torrent_params()
            atp.ti = lt.torrent_info(lt.bdecode(raw))
            atp.file_priorities = [0] * atp.ti.num_files()
        else:
            atp = lt.parse_magnet_uri(magnet)
        atp.save_path = save_path
        atp.flags &= ~lt.torrent_flags.auto_managed   # never queued
        atp.flags &= ~lt.torrent_flags.paused
        atp.flags |= lt.torrent_flags.sequential_download
        handle = self.ses.add_torrent(atp)
        with self.lock:
            self.torrents[h] = {"h": handle, "raw": raw, "save": save_path, "primed": raw is not None,
                                "added": time.time()}
        return h

    def get(self, h):
        with self.lock:
            return self.torrents.get(h.lower())

    # ---- what stream.Stream asks (torrents.QBit's shape)
    def info(self, h):
        t = self.get(h)
        if t is None:
            return None
        st = t["h"].status()
        if not st.has_metadata:
            return {"hash": h, "state": "metaDL", "total_size": 0, "dlspeed": st.download_payload_rate,
                    "num_seeds": st.num_seeds, "save_path": t["save"], "download_path": "", "progress": 0}
        if not t["primed"]:   # a magnet: its files are known now, nothing downloads before a stream picks one
            t["h"].prioritize_files([0] * t["h"].torrent_file().num_files())
            t["primed"] = True
        return {"hash": h, "state": "downloading", "total_size": t["h"].torrent_file().total_size(),
                "dlspeed": st.download_payload_rate, "num_seeds": st.num_seeds, "num_leechs": st.num_peers - st.num_seeds,
                "save_path": t["save"], "download_path": "", "progress": st.progress,
                "content_path": str(Path(t["save"]) / t["h"].torrent_file().name())}

    def files(self, h):
        t = self.get(h)
        if t is None or not t["h"].status().has_metadata:
            return []
        ti = t["h"].torrent_file()
        fs = ti.files()
        prio = t["h"].get_file_priorities()
        prog = t["h"].file_progress()
        out = []
        for i in range(fs.num_files()):
            if fs.file_flags(i) & lt.file_storage.flag_pad_file:
                continue
            size = fs.file_size(i)
            first = fs.file_offset(i) // ti.piece_length()
            last = (fs.file_offset(i) + max(size, 1) - 1) // ti.piece_length()
            out.append({"index": i, "name": fs.file_path(i).replace("\\", "/"), "size": size,
                        "priority": LT_TO_QB.get(prio[i], 1), "progress": (prog[i] / size) if size else 1.0,
                        "piece_range": [first, last], "offset": fs.file_offset(i)})
        return out

    def properties(self, h):
        t = self.get(h)
        return {"piece_size": t["h"].torrent_file().piece_length()} if t and t["h"].status().has_metadata else {}

    def piece_states(self, h):
        t = self.get(h)
        if t is None:
            return []
        return [2 if have else 0 for have in t["h"].status(lt.torrent_handle.query_pieces).pieces]

    def file_priority(self, h, indexes, priority):
        t = self.get(h)
        if t is not None:
            for i in indexes:
                t["h"].file_priority(i, QB_TO_LT.get(priority, 4))

    def set_sequential(self, h, on):
        t = self.get(h)
        if t is not None:
            (t["h"].set_flags if on else t["h"].unset_flags)(lt.torrent_flags.sequential_download)

    def want(self, h, first, last, piece_size, urgent=False):
        """The player reads at piece `first`: the next pieces (up to `last`, the file's end) get deadlines, nearest
        first, so they're fetched before everything else. Deadlines are never cleared: mpv reads in more than one
        place (the end of the file for its index), and clearing them for one read took them away from the piece
        playback was waiting on (user's test Nov 3: stuck at 63 MB for 24 s at 46 MB/s). urgent = the player is
        blocked on `first`: it's asked for again, first of all."""
        t = self.get(h)
        if t is None:
            return
        handle = t["h"]
        if urgent:
            if not handle.have_piece(first):
                handle.piece_priority(first, 7)
                handle.set_piece_deadline(first, 0)
            return
        if self.windows.get(h) == first:
            return
        self.windows[h] = first
        # Deadlines alone didn't steer the bandwidth (user's run Nov 3: the start crept 48 → 56 MB in 24 s while 30 MB/s
        # went to the rest of the file): the file's pieces are low priority (1), the window ahead of the player top
        # priority (7), and libtorrent always takes 7 before 1. The rest still comes, after.
        rng = t.get("lowered")
        if rng is None or rng[0] > first or rng[1] < last:   # the file's pieces low, once
            lo = rng[0] if rng else first
            start = min(lo, first)
            for p in range(start, last + 1):
                if not handle.have_piece(p):
                    handle.piece_priority(p, 1)
            t["lowered"] = (start, last)
            if not handle.have_piece(last):   # the end (MKV's index) stays first
                handle.piece_priority(last, 7)
        n = max(WINDOW_MIN, WINDOW_BYTES // max(1, piece_size))
        for k, p in enumerate(range(first, min(last, first + n - 1) + 1)):
            if not handle.have_piece(p):
                handle.piece_priority(p, 7)
                handle.set_piece_deadline(p, 300 + 250 * k)

    def focus(self, h, first, last, piece_size):
        """The player waits on piece `first`: only the window ahead of it (and the file's end) stays wanted, the rest of
        the file is off for now. With nothing else to fetch libtorrent asks every peer for the missing blocks (end game)
        instead of leaving the piece with one slow peer while fast ones fetch elsewhere (user's run Nov 3: the start
        stuck at 105 MB for 12 s at 30 MB/s with 107 seeds). `unfocus` puts the rest back."""
        t = self.get(h)
        if t is None or t.get("focused") == first:
            return
        handle = t["h"]
        n = max(WINDOW_MIN, WINDOW_BYTES // max(1, piece_size))
        lo = (t.get("lowered") or (first, last))[0]
        pr = list(handle.get_piece_priorities())
        for p in range(min(lo, first), last + 1):
            if first <= p < first + n or p == last:
                pr[p] = 7
            elif pr[p] == 1:   # only the low ones go off: another window being waited on (mpv also reads the
                pr[p] = 0      # file's end) keeps its 7s, or two waits switched each other's off
        handle.prioritize_pieces(pr)
        handle.set_piece_deadline(first, 0)
        t["focused"] = first

    def unfocus(self, h, last):
        """Enough is ready ahead of the player again: the rest of the file is wanted again (low priority)."""
        t = self.get(h)
        if t is None or t.get("focused") is None:
            return
        handle = t["h"]
        lo = (t.get("lowered") or (t["focused"], last))[0]
        pr = list(handle.get_piece_priorities())
        for p in range(min(lo, t["focused"]), last + 1):
            if pr[p] == 0:
                pr[p] = 1
        handle.prioritize_pieces(pr)
        t["focused"] = None

    def want_end(self, h, last):
        """MKV keeps its index at the end: the file's last piece right away too."""
        t = self.get(h)
        if t is not None and not t["h"].have_piece(last):
            t["h"].piece_priority(last, 7)
            t["h"].set_piece_deadline(last, 500)

    # ---- when you're done with it
    def file_path(self, h, index):
        t = self.get(h)
        if t is None or not t["h"].status().has_metadata:
            return None
        return Path(t["save"]) / t["h"].torrent_file().files().file_path(index)

    def file_done(self, h, index):
        t = self.get(h)
        if t is None or not t["h"].status().has_metadata:
            return False
        size = t["h"].torrent_file().files().file_size(index)
        return t["h"].file_progress()[index] >= size

    def drop(self, h, delete):
        """Out of the session. delete: its files go too (an episode you didn't keep, a batch's leftovers)."""
        with self.lock:
            t = self.torrents.pop(h.lower(), None)
        self.windows.pop(h.lower(), None)
        if t is None:
            return
        if delete:
            self.ses.remove_torrent(t["h"], lt.session.delete_files)
        else:
            self.ses.remove_torrent(t["h"])
        for _ in range(60):   # until libtorrent let go of the files
            try:
                if not t["h"].is_valid():
                    break
            except Exception:
                break
            time.sleep(0.05)

    def torrent_bytes(self, h):
        t = self.get(h)
        if t is None:
            return None
        if t["raw"]:
            return t["raw"]
        return lt.bencode(lt.create_torrent(t["h"].torrent_file()).generate())

    def release(self, h):
        """Out of this session, files kept (and closed): qBittorrent takes it from here."""
        with self.lock:
            t = self.torrents.pop(h.lower(), None)
        self.windows.pop(h.lower(), None)
        if t is not None:
            self.ses.remove_torrent(t["h"])
            for _ in range(50):   # until libtorrent let go of the files
                try:
                    if not t["h"].is_valid():
                        break
                except Exception:
                    break
                time.sleep(0.05)
        return t

    def stop(self):
        try:
            self.ses.pause()
        except Exception:
            pass
