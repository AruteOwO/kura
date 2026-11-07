"""Play a torrent while qBittorrent downloads it (Torrents → Play now).

qBittorrent writes pieces where they belong in the file, so a file that's still downloading already has its full
size, with holes. mpv can't read it directly: it would play the holes as garbage. Kura serves the file to its player
over a small HTTP server on 127.0.0.1 instead, handing out only bytes whose pieces qBittorrent says are complete and
waiting (the player shows it's buffering) where they aren't yet. The file is read in short opens, never held, so
qBittorrent and the organizer can rename and move it meanwhile; a file that moved is looked for again.

No Qt here (like torrents.py): Kura polls `Stream.status()` for the player's seek bar and the Torrents page.
"""
import http.server
import threading
import time
import urllib.parse
from pathlib import Path

CHUNK = 1 << 20                 # bytes sent per read
START_BYTES = 12 * (1 << 20)    # downloaded from the start before the player is told it can begin
CLOSE_CALL = 24 << 20           # the engine: a wait this close ahead of where you watch is a real one (focus)
MIME = {".mkv": "video/x-matroska", ".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm",
        ".avi": "video/x-msvideo", ".ts": "video/mp2t", ".m2ts": "video/mp2t", ".wmv": "video/x-ms-wmv"}
VIDEO_EXTS = set(MIME) | {".mov", ".flv", ".ogm", ".rmvb"}
PORT = 47130                    # tried first: a steady address keeps the player's resume points between sessions


class Stream:
    """One video file of one torrent. A thread asks qBittorrent every second which pieces it has."""

    def __init__(self, client, h, pick=None, moved=None, name="", on_held=None, engine=False, on_complete=None):
        self.client, self.hash = client, h.lower()
        # Kura's own engine (engine.py) instead of qBittorrent: only the chosen file is wanted from the start (no
        # holding / putting back), the pieces the player reads next get deadlines (client.want), and on_complete(self)
        # is called once when the file is done (Kura hands the torrent to qBittorrent then)
        self.engine, self.on_complete, self.reported = engine, on_complete, False
        self.on_held = on_held        # (hash, {index: priority} or None, [torrent folder paths]): Kura keeps them
                                      # (a crash mid-way) and the organizer leaves those folders alone meanwhile
        self.pick = pick              # (files) -> the file's index, or None for the biggest video
        self.moved = moved            # (file name, size) -> where the organizer put it (None: not found)
        self.name = name              # the release, for the address the player sees
        self.cond = threading.Condition()
        self.info, self.file, self.index = {}, None, None
        self.size = self.offset = self.piece_size = 0
        self.first_piece = self.last_piece = 0
        self.states = []
        self.complete = False
        self.phase = "starting"       # starting → metadata → ready (file known) ; or "error"
        self.error = ""
        self.closed = False
        self.found = None             # the file's path last time it was opened
        self.speed = 0
        self.gen = 0                  # each new request from the player; older ones that wait give up
        self.read_pos = 0             # where the player last read
        self.blocked = 0              # player requests waiting on a missing piece right now
        self.block_pos = None         # where the latest of them waits
        self.play_pos = None          # where you are watching, in bytes (the player tells, twice a second)
        # one episode of a batch: the other files wait (priority 0) or in-order mode would fetch the whole batch
        # first. They're put back (`restore`) before this file is done: with only it wanted, qBittorrent would call
        # the torrent finished and Kura would file it and remove it, the rest of the batch with it.
        self.held = None              # {file index: its priority before} while the others wait
        self.held_lock = threading.Lock()
        self.restored_at = 0
        self.thread = threading.Thread(target=self.run, daemon=True, name=f"stream {self.hash[:8]}")

    def start(self):
        self.thread.start()
        return self

    def close(self):
        with self.cond:
            self.closed = True
            self.cond.notify_all()

    # ---- watching qBittorrent
    def run(self):
        quiet = 0
        while not self.closed:
            try:
                self.poll()
                quiet = 0
            except Exception as e:   # offline for a moment: keep what we know, try again
                quiet += 1
                if quiet >= 30 and self.file is None:
                    self.fail(f"qBittorrent didn't answer ({e})")
            time.sleep(5 if self.complete else 0.5 if self.engine else 1)
        self.restore()   # stopped watching early: the rest of the batch goes on downloading

    def restore(self):
        """The batch's other files back to their priorities, in-order mode off (so this file's own priority still
        puts its last parts first). Safe to call more than once."""
        with self.held_lock:
            held, self.held = self.held, None
        if not held:
            return
        self.restored_at = time.time()
        try:
            by = {}
            for i, prio in held.items():
                by.setdefault(prio, []).append(i)
            for prio, idx in by.items():
                self.client.file_priority(self.hash, idx, prio)
            self.client.set_sequential(self.hash, False)
            if self.on_held:
                self.on_held(self.hash, None, None)
        except Exception:
            with self.held_lock:   # qBittorrent didn't answer: try again on the next poll
                if self.held is None:
                    self.held = held

    def fail(self, why):
        with self.cond:
            self.phase, self.error = "error", why
            self.cond.notify_all()

    def poll(self):
        c = self.client
        t = c.info(self.hash)
        if t is None:
            if self.complete:   # removed after it finished (Kura organizes and removes it): the file is all there
                return
            if self.phase != "starting":
                self.fail("The torrent was removed from qBittorrent")
            return
        self.speed = int(t.get("dlspeed") or 0)
        if self.file is None:
            if t.get("state") in ("metaDL", "forcedMetaDL") or not t.get("total_size"):
                self.phase = "metadata"
                return
            files = c.files(self.hash)
            if not files:
                return
            idx = self.pick(files) if self.pick else None
            if idx is None:
                videos = [f for f in files if Path(f.get("name", "")).suffix.lower() in VIDEO_EXTS]
                if not videos:
                    self.fail("There's no video in this torrent")
                    return
                idx = max(videos, key=lambda f: f.get("size", 0))["index"]
            f = next(x for x in files if x["index"] == idx)
            props = c.properties(self.hash)
            ps = int(props.get("piece_size") or 0)
            if not ps:
                return
            offset = f["offset"] if "offset" in f else sum(int(x.get("size", 0)) for x in files if x["index"] < idx)
            pr = f.get("piece_range") or [offset // ps, (offset + int(f["size"]) - 1) // ps]
            if offset // ps != pr[0]:   # padded (v2) torrents start each file on a piece of its own
                offset = pr[0] * ps
            if self.engine:   # the engine wants nothing else from this torrent: no holding, nothing to put back
                c.file_priority(self.hash, [idx], 7)
                c.want(self.hash, pr[0], pr[1], ps)   # the start first, and the end (MKV's index)
                c.want_end(self.hash, pr[1])
            elif len(files) > 1:   # only this file for now (see `held`)
                others = {x["index"]: int(x.get("priority", 1)) for x in files
                          if x["index"] != idx and int(x.get("priority", 1)) > 0 and float(x.get("progress") or 0) < 1}
                if others:
                    if self.on_held:   # written down first: a crash in between still finds them
                        top = f["name"].replace("\\", "/").split("/")[0]
                        paths = [p for p in {t.get("content_path"), str(Path(t.get("save_path") or "") / top)
                                             if t.get("save_path") else None} if p]
                        self.on_held(self.hash, others, paths)
                    c.file_priority(self.hash, list(others), 0)
                    with self.held_lock:
                        self.held = others
                if f.get("priority", 1) < 7:
                    c.file_priority(self.hash, [idx], 7)
            with self.cond:
                self.file, self.index, self.size = f, idx, int(f["size"])
                self.offset, self.piece_size = offset, ps
                self.first_piece, self.last_piece = pr[0], pr[1]
                self.info = t
                self.phase = "ready"
                self.cond.notify_all()
        states = c.piece_states(self.hash)
        with self.cond:
            self.info = t
            if states:
                self.states = states
            part = self.states[self.first_piece:self.last_piece + 1]
            # (the engine's progress counts wanted files only: with nothing wanted yet it said 100 %, so only the
            # file's own pieces count there)
            self.complete = bool(part) and all(s == 2 for s in part) or (
                not self.engine and float(t.get("progress") or 0) >= 1)
            self.cond.notify_all()
        # this file is done: the rest of the batch comes back. Not before: any "nearly done" margin either turned
        # in-order mode off far too early at high speed (pieces from all over the file, the start never came: user
        # report Nov 3) or could be crossed between two polls. qBittorrent calls the torrent finished for a moment;
        # Kura (StreamManager.holding) and the organizer (stream_holds.json paths) both leave it alone meanwhile.
        # the engine's focus is only for while the player waits: nobody waiting → the rest of the file is wanted again.
        # (Lifting it at "64 MB ready ahead" never happened: the focused window is 48 MB, so once it was in nothing was
        # wanted, the engine idled at 0.3 MB/s and seeds dropped us, user's run Nov 3 22:25.)
        if self.engine and not self.complete and self.file is not None and not (
                self.blocked and self.block_pos is not None and self.close_call(self.block_pos)):
            try:
                c.unfocus(self.hash, self.last_piece)
            except Exception:
                pass
        if self.held and self.complete:
            self.restore()
        if self.complete and self.on_complete and not self.reported:
            self.reported = True
            self.on_complete(self)

    # ---- what's downloaded
    def _have(self, piece):
        return self.complete or (0 <= piece < len(self.states) and self.states[piece] == 2)

    def _piece(self, pos):
        return (self.offset + pos) // self.piece_size

    def contiguous(self, pos):
        """How many bytes from pos on can be read now (up to the first missing piece)."""
        if self.file is None or pos >= self.size:
            return 0
        if self.complete:
            return self.size - pos
        p = self._piece(pos)
        while p <= self.last_piece and self._have(p):
            p += 1
        return max(0, min(self.size, (p * self.piece_size) - self.offset) - pos)

    def ranges(self):
        """The downloaded parts as [(start, end)] fractions of the file."""
        if self.file is None or not self.size:
            return []
        if self.complete:
            return [(0.0, 1.0)]
        out, run = [], None
        for p in range(self.first_piece, self.last_piece + 1):
            if self._have(p):
                a = max(0, p * self.piece_size - self.offset)
                b = min(self.size, (p + 1) * self.piece_size - self.offset)
                if run and run[1] >= a:
                    run[1] = b
                else:
                    run = [a, b]
                    out.append(run)
        return [(a / self.size, b / self.size) for a, b in out]

    def done_fraction(self):
        if self.complete:
            return 1.0
        n = self.last_piece - self.first_piece + 1
        if self.file is None or n <= 0:
            return 0.0
        return sum(1 for p in range(self.first_piece, self.last_piece + 1) if self._have(p)) / n

    def eta(self, frac):
        """Seconds until the file is downloaded up to frac (sequential: everything missing before it comes first)."""
        if self.file is None:
            return None
        target = min(self.last_piece, self._piece(int(frac * self.size)))
        missing = sum(1 for p in range(self.first_piece, target + 1) if not self._have(p))
        if not missing:
            return 0
        return missing * self.piece_size / self.speed if self.speed > 0 else None

    def can_start(self):
        """Enough from the start (and the file's end, where MKV keeps its index) to open the player without a wait."""
        if self.file is None:
            return False
        if self.complete:
            return True
        return self.contiguous(0) >= min(self.size, START_BYTES) and self._have(self.last_piece)

    def status(self):
        with self.cond:
            return {"phase": self.phase, "error": self.error, "complete": self.complete, "speed": self.speed,
                    "size": self.size, "done": self.done_fraction(), "start": self.contiguous(0),
                    "need": min(self.size, START_BYTES) if self.size else START_BYTES,
                    "piece": self.piece_size, "first_done": self._have(self.first_piece) if self.file else False,
                    "ready": self.can_start(), "ranges": self.ranges(), "eta": self.eta,
                    "file": (self.file or {}).get("name", ""), "seeds": int(self.info.get("num_seeds") or 0),
                    "peers": int(self.info.get("num_leechs") or 0), "blocked": self.blocked}

    # ---- reading
    def candidates(self):
        t, name = self.info, (self.file or {}).get("name", "")
        for base in (t.get("save_path"), t.get("download_path")):
            if base and name:
                p = Path(base) / name
                yield p
                yield p.with_name(p.name + ".!qB")   # qBittorrent's "incomplete file" extension

    def path(self):
        if self.found is not None and self.found.exists():
            return self.found
        for p in self.candidates():
            if p.exists():
                self.found = p
                return p
        if self.moved and self.file:   # organized while you watch (qBittorrent's run-on-finish hook)
            p = self.moved(Path(self.file["name"]).name, self.size)
            if p:
                self.found = Path(p)
                return self.found
        raise FileNotFoundError(f"Can't find {(self.file or {}).get('name')}")

    def read(self, pos, n):
        for attempt in range(3):
            try:
                with open(self.path(), "rb") as fh:   # a short open: never stops a rename or a move
                    fh.seek(pos)
                    return fh.read(n)
            except OSError:
                self.found = None
                if attempt == 2:
                    raise
                time.sleep(0.3)

    def wait(self, pos, want, gen):
        """Bytes readable at pos (up to want), waiting for qBittorrent; 0 when this request should give up."""
        self.read_pos = pos
        if self.engine and self.file is not None and not self.complete:
            try:   # the engine: what the player reads now (and just after) is wanted first
                self.client.want(self.hash, self._piece(pos), self.last_piece, self.piece_size)
            except Exception:
                pass
        asked, counted = time.time(), False
        with self.cond:
            try:
                while not self.closed and self.phase != "error" and gen == self.gen:
                    n = self.contiguous(pos)
                    if n > 0:
                        return min(n, want)
                    if not counted:   # the player waits (poll lifts the engine's focus only when nobody does)
                        counted = True
                        self.blocked += 1
                    self.block_pos = pos
                    self.cond.wait(1.0)
                    if self.engine and time.time() - asked >= 1.5 and self.close_call(pos):
                        asked = time.time()   # playback is about to reach it: only this part wanted for now
                        try:
                            self.client.want(self.hash, self._piece(pos), self.last_piece, self.piece_size, urgent=True)
                            self.client.focus(self.hash, self._piece(pos), self.last_piece, self.piece_size)
                        except Exception:
                            pass
            finally:
                if counted:
                    self.blocked -= 1
        return 0

    def close_call(self, pos):
        """A wait at pos is a real one (playback will soon need it), not mpv reading far ahead: the engine focuses
        only then. Before the player knows where it is (opening), every wait is (user's run Nov 3 22:28: mpv's
        read-ahead waits all the time, focus never lifted and the speed stayed at 3 MB/s)."""
        return self.play_pos is None or pos - self.play_pos < CLOSE_CALL

    def wait_file(self, timeout=600):
        end = time.time() + timeout
        with self.cond:
            while self.file is None and not self.closed and self.phase != "error" and time.time() < end:
                self.cond.wait(1.0)
            return self.file is not None and not self.closed


class Server:
    """Serves the streams to Kura's player: http://127.0.0.1:<port>/s/<token>/<release name>."""

    def __init__(self):
        self.streams, self.httpd, self.port = {}, None, 0
        self.lock = threading.Lock()

    def start(self):
        if self.httpd is not None:
            return
        handler = type("StreamHandler", (Handler,), {"server_ref": self})
        for port in (PORT, 0):
            try:
                self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
                break
            except OSError:
                continue
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True, name="stream server").start()

    def add(self, stream, token=None):
        self.start()
        token = token or stream.hash[:16]
        with self.lock:
            old = self.streams.get(token)
            if old is not None and old is not stream:
                old.close()
            self.streams[token] = stream
        name = urllib.parse.quote((stream.name or "video").replace("/", " ")[:120])
        return token, f"http://127.0.0.1:{self.port}/s/{token}/{name}"

    def get(self, token):
        with self.lock:
            return self.streams.get(token)

    def remove(self, token):
        with self.lock:
            s = self.streams.pop(token, None)
        if s is not None:
            s.close()

    def stop(self):
        for token in list(self.streams):
            self.remove(token)
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None


class Handler(http.server.BaseHTTPRequestHandler):
    server_ref = None
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def stream(self):
        parts = urllib.parse.urlparse(self.path).path.split("/")
        s = self.server_ref.get(parts[2]) if len(parts) > 2 and parts[1] == "s" else None
        if s is None:
            self.send_error(404)
            return None
        if not s.wait_file():
            self.send_error(503, s.error or "The torrent isn't ready")
            return None
        return s

    def head(self, s):
        """(start, end) of the answer after sending its headers, or None (an error was sent)."""
        size = s.size
        start, end = 0, size - 1
        rng = self.headers.get("Range", "")
        partial = rng.startswith("bytes=")
        if partial:
            a, _, b = rng[6:].split(",")[0].partition("-")
            try:
                if a:
                    start, end = int(a), (int(b) if b else size - 1)
                elif b:   # the last N bytes
                    start = max(0, size - int(b))
            except ValueError:
                partial = False
            end = min(end, size - 1)
            if start >= size or start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", MIME.get(Path(s.file["name"]).suffix.lower(), "application/octet-stream"))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        return start, end

    def do_HEAD(self):
        s = self.stream()
        if s is not None:
            self.head(s)

    def do_GET(self):
        s = self.stream()
        if s is None:
            return
        with s.cond:
            s.gen += 1
            gen = s.gen
        span = self.head(s)
        if span is None:
            return
        pos, end = span
        try:
            while pos <= end:
                n = s.wait(pos, min(CHUNK, end - pos + 1), gen)
                if not n:
                    self.close_connection = True
                    return
                data = s.read(pos, n)
                if not data:
                    self.close_connection = True
                    return
                self.wfile.write(data)
                pos += len(data)
        except (ConnectionError, OSError):
            self.close_connection = True
