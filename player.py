"""
Kura's built-in video player, using mpv's engine (libmpv) for playback.

mpv draws into a Qt OpenGL widget (its render API), so Kura's own controls float over
the video: a seek bar that shows the opening/ending, skip buttons, Skip Opening/Ending
(from the file's chapters or AniSkip), audio/subtitle tracks, speed, volume, fullscreen,
previous/next episode and resume. Styled (ASS) subtitles render exactly like in mpv.

Keys: Space/K pause · ←/→ short skip · Shift+←/→ long skip · ↑/↓ volume · M mute ·
F or double-click fullscreen · Esc leave fullscreen · N/P next/previous episode ·
A/S cycle audio/subtitles · [ ] speed · O skip opening/ending · E episode list
"""

import json
import os
import re
import threading
import time

import capture
import urllib.error
import urllib.request
from pathlib import Path

from PySide6.QtCore import QElapsedTimer, QEvent, QPointF, QRectF, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QIcon, QImage, QLinearGradient, QOpenGLContext, QPainter,
                           QPainterPath, QPen, QPixmap)
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit, QProgressBar,
                               QPushButton, QScrollArea, QSizePolicy, QSlider, QVBoxLayout, QWidget)

ACCENT, TEXT, MUTED, MARK = "#7c6cff", "#f2f3f7", "#a3aabb", "#f5b642"
SPEEDS = [0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0]
_mpv = None


def load_mpv(dll_dirs):
    """Import python-mpv with libmpv-2.dll found in one of dll_dirs. Raises if it can't."""
    global _mpv
    if _mpv is None:
        for d in dll_dirs:
            if d and Path(d, "libmpv-2.dll").exists():
                os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
                try:
                    os.add_dll_directory(str(d))
                except (OSError, AttributeError):
                    pass
                break
        import mpv
        _mpv = mpv
    return _mpv


def fmt_time(s):
    s = int(max(0, s or 0))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


_icon_font = None
GLYPH_RESOLVER = None   # Kura sets this: glyph -> (text, family, filled, font maker) for Material Symbols
SUB_OVERRIDES = None    # Kura sets this: (style, ASS header, video height) -> mpv sub-ass-style-overrides
PICTURE = None          # Kura sets this: the shaders module (picture enhancement: upscalers, deband, sharpen)
SHOW_KEY = None         # Kura sets this: playlist item -> (key, show name), for per-show picture settings
INSTALL_SHADERS = None  # Kura sets this: download the upscalers, then call the given function
FILLER = None           # Kura sets this: (AniList id, episode) -> "filler" / "recap" / None (from its cache)
CAPTURE_INFO = None     # Kura sets this: playlist item -> {show, folder, season} (screenshot / clip names)
CAPTURE_SAVED = None    # Kura sets this: called with (path, kind) after a screenshot / clip is saved
BACKGROUND = []         # clips still being made after the player closed
LIVE = None             # Kura sets this: Live TV (what's on each channel; see kura.pyw LiveProvider)
ANISKIP_FILE = None     # Kura sets this: aniskip_cache.json (opening / ending times kept, Tools → Library metadata)
EPISODE_INFO = None     # Kura sets this: playlist -> {"items": [{name, watched, minutes, pic}...], "missing": [...]}
EPISODE_PICS = None     # Kura sets this: [(key, kind, data)] -> pictures arrive later through PlayerView.episode_image
COVER = None            # Kura sets this: playlist item -> the show's cover (QImage) or None
_skip_lock = threading.Lock()
_skip_store = None
SKIP_NAMES = {"op": "Opening", "mixed-op": "Opening", "ed": "Ending", "mixed-ed": "Ending", "recap": "Recap"}


def aniskip(mal, episode, fetch=True):
    """AniSkip's opening / ending / recap times for an episode: [(start, end, "Opening")...], from the cache
    (30 days; "nothing known" 7 days) or asked once. None when it isn't cached and fetch is off."""
    global _skip_store
    key = f"{mal}:{int(episode)}"
    with _skip_lock:
        if _skip_store is None:
            try:
                _skip_store = json.loads(Path(ANISKIP_FILE).read_text(encoding="utf-8")) if ANISKIP_FILE else {}
            except (OSError, ValueError):
                _skip_store = {}
        hit = _skip_store.get(key)
    if hit and time.time() - hit["t"] < (30 if hit["marks"] else 7) * 86400:
        return [tuple(m) for m in hit["marks"]]
    if not fetch:
        return None
    url = (f"https://api.aniskip.com/v2/skip-times/{mal}/{int(episode)}"
           f"?types[]=op&types[]=ed&types[]=mixed-op&types[]=mixed-ed&types[]=recap&episodeLength=0")
    req = urllib.request.Request(url, headers={"User-Agent": "Kura"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:   # 404 = AniSkip knows nothing for this episode
        if e.code != 404:
            raise
        data = {}
    marks = [(r["interval"]["startTime"], r["interval"]["endTime"], SKIP_NAMES[r["skipType"]])
             for r in data.get("results") or [] if r.get("skipType") in SKIP_NAMES]
    with _skip_lock:
        _skip_store[key] = {"t": time.time(), "marks": marks}
        if ANISKIP_FILE:
            try:
                tmp = Path(ANISKIP_FILE).with_suffix(".tmp")
                tmp.write_text(json.dumps(_skip_store), encoding="utf-8")
                os.replace(tmp, ANISKIP_FILE)
            except OSError:
                pass
    return marks
# mpv's own style for plain-text subtitles (SRT), as it starts: put back when restyling is turned off
PLAIN_SUB_DEFAULTS = {"sub-color": "#FFFFFFFF", "sub-outline-color": "#FF000000", "sub-outline-size": 1.65,
                      "sub-shadow-offset": 0.0, "sub-shadow-color": "#AF000000", "sub-font": "sans-serif",
                      "sub-font-size": 38.0, "sub-bold": False}


def icon(glyph, color=TEXT, px=18):
    global _icon_font
    if _icon_font is None:
        fams = set(QFontDatabase.families())
        _icon_font = next((f for f in ("Segoe Fluent Icons", "Segoe MDL2 Assets") if f in fams), "")
    pm = QPixmap(px * 2, px * 2)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    resolved = GLYPH_RESOLVER(glyph) if GLYPH_RESOLVER else None
    if resolved and resolved[1] and resolved[1] != _icon_font:
        glyph, family, filled, make = resolved
        f = make(family, int(px * 1.9), filled)
    else:
        f = QFont(_icon_font)
        f.setPixelSize(int(px * 1.6))
    p.setFont(f)
    p.setPen(QColor(color))
    p.drawText(pm.rect(), Qt.AlignCenter, glyph)
    p.end()
    pm.setDevicePixelRatio(2)
    return QIcon(pm)


PLAY, PAUSE, PREV, NEXT = "", "", "", ""
VOLUME, MUTE, FULL, UNFULL, CC, AUDIO, CLOSE = "", "", "", "", "", "", ""
POPOUT, POPIN, PIN, PINNED = "\uE8A7", "\uE944", "\uE718", "\uE840"

STYLE = f"""
QWidget#bar {{ background: transparent; }}
QPushButton {{ background: transparent; border: none; color: {TEXT}; padding: 6px 8px; border-radius: 8px;
              font-size: 10pt; }}
QPushButton:hover {{ background: rgba(255,255,255,0.12); }}
QPushButton#skip {{ background: rgba(15,17,21,0.85); border: 1px solid rgba(255,255,255,0.3); padding: 10px 20px;
                   font-weight: 600; border-radius: 18px; }}
QPushButton#skip:hover {{ background: {ACCENT}; border-color: {ACCENT}; }}
QPushButton#corner {{ background: rgba(15,17,22,0.62); border: 1px solid rgba(255,255,255,0.14); border-radius: 19px;
                     padding: 0px; }}
QPushButton#corner:hover {{ background: rgba(40,44,56,0.9); }}
QPushButton#bigplay {{ background: white; border: none; border-radius: 27px; padding: 0px; }}
QPushButton#bigplay:hover {{ background: #e4e4ee; }}
QPushButton#round {{ border-radius: 21px; padding: 0px; font-weight: 700; font-size: 9.5pt; }}
QPushButton#epbtn {{ background: rgba(255,255,255,0.08); border-radius: 12px; padding: 7px 12px; font-weight: 600; }}
QPushButton#epbtn:hover {{ background: rgba(255,255,255,0.16); }}
QPushButton#epbtn:checked {{ background: {ACCENT}; }}
QLabel {{ color: {TEXT}; }}
QLabel#time {{ color: #c8ccd6; font-size: 9pt; }}
QLabel#title {{ font-size: 10.5pt; font-weight: 600; }}
QLabel#sub {{ color: {MUTED}; font-size: 9pt; }}
QLabel#osd {{ background: rgba(15,17,21,0.8); border-radius: 10px; padding: 8px 14px; font-size: 11pt; }}
QSlider::groove:horizontal {{ height: 4px; background: rgba(255,255,255,0.25); border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {TEXT}; border-radius: 2px; }}
QSlider::handle:horizontal {{ width: 12px; height: 12px; margin: -4px 0; border-radius: 6px; background: {TEXT}; }}
QFrame#overlaymenu {{ background: rgba(22,25,32,0.97); border: 1px solid #363c4b; border-radius: 10px; }}
QPushButton#menuitem {{ text-align: left; padding: 7px 18px 7px 10px; border-radius: 6px; }}
QPushButton#menuitem:hover {{ background: rgba(124,108,255,0.3); }}
QPushButton#menuitem:disabled {{ color: {MUTED}; }}
QFrame#menusep {{ background: #363c4b; max-height: 1px; margin: 4px 6px; }}
QLineEdit#menuinput {{ background: #10131a; border: 1px solid #363c4b; border-radius: 6px; padding: 6px 8px;
                      color: {TEXT}; min-width: 160px; }}
QMenu {{ background: #1d212a; border: 1px solid #363c4b; border-radius: 8px; padding: 5px; color: {TEXT}; }}
QMenu::item {{ padding: 7px 22px; border-radius: 5px; }}
QMenu::item:selected {{ background: rgba(124,108,255,0.3); }}
QMenu::item:checked {{ color: {ACCENT}; font-weight: 600; }}
"""


class CountRing(QWidget):
    """The countdown on the Up next card: a ring that empties, the seconds left inside."""

    def __init__(self):
        super().__init__()
        self.setFixedSize(54, 54)
        self.left, self.total = 0.0, 1.0

    def set(self, left, total):
        self.left, self.total = left, total
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(4, 4, -4, -4)
        p.setPen(QPen(QColor(255, 255, 255, 40), 4))
        p.drawEllipse(r)
        pen = QPen(QColor(ACCENT), 4)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.drawArc(r, 90 * 16, int(360 * 16 * max(0.0, self.left / self.total)))
        f = QFont(self.font())
        f.setPixelSize(17)
        f.setWeight(QFont.Bold)
        p.setFont(f)
        p.setPen(QColor(TEXT))
        p.drawText(r, Qt.AlignCenter, str(max(0, int(self.left + 0.999))))


class NextUpCard(QFrame):
    """End of an episode with autoplay on: "Up next" with a countdown, Play now and Cancel."""
    play_now = Signal(bool)   # True = the countdown ran out (nobody pressed anything)
    cancelled = Signal()

    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("nextup")
        self.setStyleSheet("QFrame#nextup { background: rgba(18,20,26,0.94); border: 1px solid rgba(255,255,255,0.12);"
                           " border-radius: 14px; }")
        self.setFixedWidth(400)
        h = QHBoxLayout(self)
        h.setContentsMargins(16, 14, 16, 14)
        h.setSpacing(14)
        self.ring = CountRing()
        h.addWidget(self.ring, 0, Qt.AlignTop)
        v = QVBoxLayout()
        v.setSpacing(2)
        k = QLabel("UP NEXT")
        k.setStyleSheet(f"color: {ACCENT}; font-size: 8pt; font-weight: 700; background: transparent;")
        v.addWidget(k)
        self.title = QLabel()
        self.title.setWordWrap(True)
        self.title.setStyleSheet(f"color: {TEXT}; font-size: 11pt; font-weight: 600; background: transparent;")
        v.addWidget(self.title)
        self.sub = QLabel()
        self.sub.setStyleSheet(f"color: {MUTED}; font-size: 9pt; background: transparent;")
        v.addWidget(self.sub)
        v.addSpacing(8)
        row = QHBoxLayout()
        row.setSpacing(8)
        now = QPushButton("Play now")
        now.setCursor(Qt.PointingHandCursor)
        now.setFocusPolicy(Qt.NoFocus)
        now.setStyleSheet("QPushButton { background: white; color: #0d0f14; border: none; border-radius: 9px;"
                          " padding: 7px 16px; font-weight: 600; } QPushButton:hover { background: #e4e4ee; }")
        now.clicked.connect(lambda: self.finish(False))
        cancel = QPushButton("Cancel")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.setFocusPolicy(Qt.NoFocus)
        cancel.setStyleSheet("QPushButton { background: rgba(255,255,255,0.12); color: white; border: 1px solid"
                             " rgba(255,255,255,0.16); border-radius: 9px; padding: 7px 14px; }"
                             " QPushButton:hover { background: rgba(255,255,255,0.2); }")
        cancel.clicked.connect(self.cancel)
        row.addWidget(now)
        row.addWidget(cancel)
        row.addStretch(1)
        v.addLayout(row)
        h.addLayout(v, 1)
        self.seconds = 8.0
        self.clock = QElapsedTimer()
        self.timer = QTimer(self, interval=50, timeout=self.tick)
        self.hide()

    def start(self, title, sub, seconds=8.0):
        self.seconds = seconds
        self.title.setText(title)
        self.sub.setText(sub)
        self.sub.setVisible(bool(sub))
        self.adjustSize()
        self.clock.start()
        self.ring.set(seconds, seconds)
        self.show()
        self.raise_()
        self.timer.start()

    def tick(self):
        left = self.seconds - self.clock.elapsed() / 1000
        self.ring.set(left, self.seconds)
        if left <= 0:
            self.finish(True)

    def finish(self, automatic=False):
        self.stop()
        self.play_now.emit(automatic)

    def cancel(self):
        self.stop()
        self.cancelled.emit()

    def stop(self):
        self.timer.stop()
        self.hide()


class StillWatchingCard(QFrame):
    """After a few episodes played by themselves with nobody touching anything: "Are you still watching?"."""
    keep_going = Signal()
    done = Signal()

    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("nextup")
        self.setStyleSheet("QFrame#nextup { background: rgba(18,20,26,0.96); border: 1px solid rgba(255,255,255,0.14);"
                           " border-radius: 16px; }")
        self.setFixedWidth(440)
        v = QVBoxLayout(self)
        v.setContentsMargins(26, 22, 26, 22)
        v.setSpacing(6)
        k = QLabel("STILL THERE?")
        k.setStyleSheet(f"color: {ACCENT}; font-size: 8pt; font-weight: 700; background: transparent;")
        v.addWidget(k)
        self.title = QLabel("Are you still watching?")
        self.title.setWordWrap(True)
        self.title.setStyleSheet(f"color: {TEXT}; font-size: 15pt; font-weight: 600; background: transparent;")
        v.addWidget(self.title)
        self.sub = QLabel("")
        self.sub.setWordWrap(True)
        self.sub.setStyleSheet(f"color: {MUTED}; font-size: 9.5pt; background: transparent;")
        v.addWidget(self.sub)
        v.addSpacing(10)
        row = QHBoxLayout()
        row.setSpacing(8)
        go = QPushButton("Continue watching")
        go.setCursor(Qt.PointingHandCursor)
        go.setFocusPolicy(Qt.NoFocus)
        go.setStyleSheet("QPushButton { background: white; color: #0d0f14; border: none; border-radius: 9px;"
                         " padding: 8px 18px; font-weight: 600; } QPushButton:hover { background: #e4e4ee; }")
        go.clicked.connect(lambda: (self.hide(), self.keep_going.emit()))
        stop = QPushButton("I'm done")
        stop.setCursor(Qt.PointingHandCursor)
        stop.setFocusPolicy(Qt.NoFocus)
        stop.setStyleSheet("QPushButton { background: rgba(255,255,255,0.12); color: white; border: 1px solid"
                           " rgba(255,255,255,0.16); border-radius: 9px; padding: 8px 16px; }"
                           " QPushButton:hover { background: rgba(255,255,255,0.2); }")
        stop.clicked.connect(lambda: (self.hide(), self.done.emit()))
        row.addWidget(go)
        row.addWidget(stop)
        row.addStretch(1)
        v.addLayout(row)
        self.hide()

    def ask(self, show, played):
        self.title.setText(f"Are you still watching {show}?" if show else "Are you still watching?")
        self.sub.setText(f"{played} episodes played in a row. The next one waits until you say so.")
        self.adjustSize()
        self.show()
        self.raise_()


class ThumbSeeker(threading.Thread):
    """Frames for the seek bar: a hidden mpv on the same file seeks to the time under the mouse and saves a
    small picture (~0.1 s). Only the latest request counts; results are kept per 2-second step."""

    def __init__(self, mpv_mod, outdir, on_image):
        super().__init__(daemon=True)
        self.mpv_mod, self.outdir, self.on_image = mpv_mod, Path(outdir), on_image
        self.lock, self.wake = threading.Lock(), threading.Event()
        self.want, self.stopped, self.m, self.path, self.n = None, False, None, None, 0
        self.outdir.mkdir(parents=True, exist_ok=True)

    def request(self, path, t):
        with self.lock:
            self.want = (path, t)
        self.wake.set()

    def stop(self):
        self.stopped = True
        self.wake.set()

    def run(self):
        while not self.stopped:
            self.wake.wait(0.5)
            self.wake.clear()
            with self.lock:
                want, self.want = self.want, None
            if want is None:
                continue
            path, t = want
            try:
                if path != self.path:
                    if self.m is not None:
                        self.m.terminate()
                    self.m = self.mpv_mod.MPV(vo="null", ao="null", audio="no", sub="no", terminal=False, pause=True,
                                              hwdec="auto-copy-safe", vf="scale=320:-2", ytdl=False,
                                              input_default_bindings=False, osc=False)
                    self.m.play(path)
                    end = time.time() + 8
                    while self.m.time_pos is None and time.time() < end and not self.stopped:
                        time.sleep(0.03)
                    self.path = path
                self.m.command("seek", f"{t:.2f}", "absolute+keyframes")
                time.sleep(0.06)
                self.n += 1
                out = self.outdir / f"seek{self.n % 4}.jpg"
                self.m.command("screenshot-to-file", str(out), "video")
                img = QImage(str(out))
                if not img.isNull():
                    self.on_image(path, t, img)
            except Exception:
                self.path = None
        if self.m is not None:
            try:
                self.m.terminate()
            except Exception:
                pass


class OverlayMenu(QFrame):
    """A menu drawn inside the player. Real popup menus are separate windows, and Windows can keep
    them hidden behind a fullscreen window that shows OpenGL video (the menus "didn't work" in
    fullscreen). items: (text, callback, checked or None), or None for a separator."""

    def __init__(self, view, items, anchor, input_text=None, on_input=None):
        super().__init__(view)
        self.setObjectName("overlaymenu")
        self.view, self.anchor = view, anchor
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(1)
        self.input = None
        self.pad_items, self.pad_sel = [], -1   # controller: which item is highlighted
        if on_input is not None:   # a one-line question instead of a list ("Go to time")
            lay.addWidget(QLabel(input_text))
            self.input = QLineEdit()
            self.input.setObjectName("menuinput")
            self.input.returnPressed.connect(lambda: (self.close_menu(), on_input(self.input.text())))
            lay.addWidget(self.input)
        for item in items:
            if item is None:
                sep = QFrame()
                sep.setObjectName("menusep")
                sep.setFixedHeight(1)
                lay.addWidget(sep)
                continue
            text, fn, checked = item
            b = QPushButton(("✓   " if checked else "     ") + text if checked is not None else text)
            b.setObjectName("menuitem")
            b.setFocusPolicy(Qt.NoFocus)
            b.setCursor(Qt.PointingHandCursor)
            if checked:
                b.setStyleSheet(f"color: {ACCENT}; font-weight: 600;")
            if fn is None:
                b.setEnabled(False)
            else:
                b.clicked.connect(lambda _=False, f=fn: (self.close_menu(), f()))
                self.pad_items.append(b)
            lay.addWidget(b)
        self.adjustSize()
        self.place()
        self.show()
        self.raise_()
        QApplication.instance().installEventFilter(self)
        if self.input is not None:
            self.input.setFocus()

    def pad_move(self, step):
        """The d-pad moves the highlight (the first press lands on the first item)."""
        if not self.pad_items:
            return
        if 0 <= self.pad_sel < len(self.pad_items):
            self.pad_items[self.pad_sel].setStyleSheet(self.pad_items[self.pad_sel].property("base_css") or "")
        self.pad_sel = 0 if self.pad_sel < 0 else (self.pad_sel + step) % len(self.pad_items)
        b = self.pad_items[self.pad_sel]
        if b.property("base_css") is None:
            b.setProperty("base_css", b.styleSheet())
        b.setStyleSheet((b.property("base_css") or "") + f" QPushButton {{ background: rgba(124,108,255,0.35);"
                                                         f" border-radius: 8px; }}")

    def pad_activate(self):
        if 0 <= self.pad_sel < len(self.pad_items):
            self.pad_items[self.pad_sel].click()
        elif self.pad_items:
            self.pad_move(1)

    def place(self):
        v, a = self.view, self.anchor
        if a is not None and a.isVisible():
            top_left = a.mapTo(v, a.rect().topLeft())
            x = top_left.x() + a.width() - self.width()
            y = top_left.y() - self.height() - 8
        else:
            x, y = (v.width() - self.width()) // 2, (v.height() - self.height()) // 2
        self.move(max(8, min(x, v.width() - self.width() - 8)), max(8, y))

    def eventFilter(self, obj, e):
        if e.type() == QEvent.MouseButtonPress and isinstance(obj, QWidget) and obj.window() is self.window():
            inside = obj is self or self.isAncestorOf(obj)
            if not inside:
                on_anchor = self.anchor is not None and (obj is self.anchor or self.anchor.isAncestorOf(obj))
                self.close_menu()
                if on_anchor:
                    return True   # a second click on the same button just closes the menu
        elif e.type() == QEvent.KeyPress and e.key() == Qt.Key_Escape:
            self.close_menu()
            return True
        return False

    def close_menu(self):
        if self.view.menu is self:
            self.view.menu = None
        QApplication.instance().removeEventFilter(self)
        self.hide()
        self.deleteLater()
        self.view.setFocus()
        self.view.show_controls()


class MpvWidget(QOpenGLWidget):
    """mpv renders each frame into this widget's OpenGL framebuffer."""
    frame = Signal()
    ready = Signal()   # the render surface exists; only then can mpv start playing

    def __init__(self, mpv_mod, parent, **options):
        super().__init__(parent)
        # video_timing_offset=0: mpv asks for each frame at its display time (not 50 ms early),
        # which is what rendering without blocking needs
        self.mpv = mpv_mod.MPV(vo="libmpv", log_handler=self._log, loglevel="warn", video_timing_offset=0, **options)
        self._mod = mpv_mod
        self.ctx = None
        self.frame.connect(self.update, Qt.QueuedConnection)
        self.setMouseTracking(True)

    @staticmethod
    def _log(level, component, message):
        if level in ("error", "fatal") and "INVALID_ENUM" not in message:   # harmless OpenGL noise
            print(f"mpv {level}: {message.strip()}")

    def initializeGL(self):
        # Also runs again after the widget moves to another window (Kura <-> pop-out):
        # Qt destroys the old GL context then, so mpv gets a new render context here.
        def get_proc(_, name):
            return QOpenGLContext.currentContext().getProcAddress(name)
        self._get_proc = self._mod.MpvGlGetProcAddressFn(get_proc)
        self.ctx = self._mod.MpvRenderContext(self.mpv, "opengl", opengl_init_params={"get_proc_address": self._get_proc})
        self.ctx.update_cb = self.frame.emit
        self.context().aboutToBeDestroyed.connect(self.free_render, Qt.DirectConnection)
        if getattr(self, "_moved_vid", None) is not None:   # bring the picture back after a move
            vid, self._moved_vid = self._moved_vid, None
            self.mpv.vid = vid
        self.ready.emit()

    def free_render(self):
        """The GL context is going away (window change): release mpv's render context first."""
        if self.ctx:
            try:
                self._moved_vid = self.mpv.vid
            except Exception:
                self._moved_vid = None
            self.makeCurrent()
            self.ctx.free()
            self.ctx = None
            self.doneCurrent()

    def paintGL(self):
        if self.ctx:
            r = self.devicePixelRatioF()
            # Don't wait for the frame's display time: this runs on Kura's UI thread, and that wait
            # (mpv's default) left the interface ~38 ms behind on every frame while a video played
            self.ctx.render(flip_y=True, block_for_target_time=False,
                            opengl_fbo={"w": int(self.width() * r), "h": int(self.height() * r),
                                        "fbo": self.defaultFramebufferObject()})

    def shutdown(self):
        try:
            self._moved_vid = None
            if self.ctx:
                self.makeCurrent()
                self.ctx.free()
                self.ctx = None
                self.doneCurrent()
        finally:
            self.mpv.terminate()


class SeekBar(QWidget):
    """Progress bar with opening/ending ranges and chapter ticks; click or drag to seek."""
    seek = Signal(float, bool)   # seconds, final (False while dragging)

    def __init__(self, parent, view):
        super().__init__(parent)
        self.view = view
        self.duration, self.position, self.marks, self.chapters = 0.0, 0.0, [], []
        self.hover_x, self.dragging = None, False
        self.clip_range, self.clip_drag = None, None   # the clip editor's start / end
        self.setMouseTracking(True)
        self.setFixedHeight(26)
        self.setCursor(Qt.PointingHandCursor)

    def _x_to_time(self, x):
        return max(0.0, min(1.0, (x - 6) / max(1, self.width() - 12))) * self.duration

    def _clip_x(self, t):
        return 6 + (self.width() - 12) * t / self.duration

    def mousePressEvent(self, e):
        if self.duration and self.clip_range:
            x = e.position().x()
            near = [(abs(self._clip_x(t) - x), i) for i, t in enumerate(self.clip_range)]
            d, i = min(near)
            if d <= 8:   # grabbed one end of the clip
                self.clip_drag = i
                return
        if self.duration:
            self.dragging = True
            self.position = self._x_to_time(e.position().x())
            self.seek.emit(self.position, False)
            self.update()

    def mouseMoveEvent(self, e):
        self.hover_x = e.position().x()
        if self.clip_drag is not None and self.duration:
            t = self._x_to_time(self.hover_x)
            bar = self.view.clip_bar
            (bar.set_start if self.clip_drag == 0 else bar.set_end)(t)
            self.seek.emit(t, False)
            self.show_hover_time()
            self.update()
            return
        if self.dragging and self.duration:
            self.position = self._x_to_time(self.hover_x)
            self.seek.emit(self.position, False)
        self.show_hover_time()
        self.update()

    def mouseReleaseEvent(self, e):
        if self.clip_drag is not None:
            self.clip_drag = None
            self.seek.emit(self._x_to_time(e.position().x()), True)
            return
        if self.dragging:
            self.dragging = False
            self.seek.emit(self._x_to_time(e.position().x()), True)

    def leaveEvent(self, e):
        self.hover_x = None
        self.view.hover_label("", None, 0)
        self.view.hover_thumb(None, None)
        self.update()

    def show_hover_time(self):
        """Time (and Opening/Ending) under the mouse, shown above the bar by the window."""
        if self.hover_x is None or not self.duration:
            return
        t = self._x_to_time(self.hover_x)
        label = next((name for s, e, name in self.marks if s <= t <= e), "")
        text = f"{fmt_time(t)}  ·  {label}" if label else fmt_time(t)
        x = int(max(0, min(self.width() - 80, self.hover_x - 40)))
        self.view.hover_label(text, self.mapTo(self.view, QPointF(x, 0).toPoint()), 0)
        self.view.hover_thumb(t, self.mapTo(self.view, QPointF(self.hover_x, 0).toPoint()))

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w = self.width() - 12
        big = self.hover_x is not None or self.dragging
        h = 6 if big else 4
        y = self.height() / 2 - h / 2
        track = QRectF(6, y, w, h)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 60))
        p.drawRoundedRect(track, h / 2, h / 2)
        if self.duration:
            for start, end, _ in self.marks:   # opening / ending
                x1, x2 = 6 + w * start / self.duration, 6 + w * min(end, self.duration) / self.duration
                p.setBrush(QColor(245, 182, 66, 150))
                p.drawRect(QRectF(x1, y, max(2, x2 - x1), h))
            done = w * min(1, self.position / self.duration)
            p.setBrush(QColor(ACCENT))
            p.drawRoundedRect(QRectF(6, y, done, h), h / 2, h / 2)
            p.setBrush(QColor(0, 0, 0, 140))
            for c in self.chapters:
                if 0 < c < self.duration:
                    p.drawRect(QRectF(6 + w * c / self.duration - 1, y, 2, h))
            if self.clip_range:   # the clip: a band with a handle at each end
                a, b = (self._clip_x(t) for t in self.clip_range)
                p.setBrush(QColor(255, 255, 255, 70))
                p.drawRect(QRectF(a, y - 3, max(2, b - a), h + 6))
                p.setBrush(QColor(ACCENT).lighter(130))
                for x in (a, b):
                    p.drawRoundedRect(QRectF(x - 3, self.height() / 2 - 9, 6, 18), 3, 3)
            if big:
                p.setBrush(QColor("white"))
                p.drawEllipse(QPointF(6 + done, self.height() / 2), 7, 7)


class Bar(QWidget):
    """Control bar with a dark fade behind it, so white controls read on bright video."""

    def __init__(self, parent, fade_down):
        super().__init__(parent)
        self.fade_down = fade_down
        self.dark = 190
        self.setObjectName("bar")

    def paintEvent(self, _):
        p = QPainter(self)
        g = QLinearGradient(0, 0, 0, self.height())
        dark, clear = QColor(0, 0, 0, self.dark), QColor(0, 0, 0, 0)
        g.setColorAt(0, dark if self.fade_down else clear)
        g.setColorAt(1, clear if self.fade_down else dark)
        p.fillRect(self.rect(), g)


class ElideLabel(QLabel):
    """A one-line label that shortens its text with "…" instead of pushing the controls around."""

    def __init__(self, text=""):
        super().__init__()
        self.full = ""
        self.setMinimumWidth(10)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setText(text)

    def setText(self, text):
        self.full = text or ""
        self.setToolTip(self.full)
        self._fit()

    def text(self):
        return self.full

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit()

    def _fit(self):
        super().setText(self.fontMetrics().elidedText(self.full, Qt.ElideRight, max(10, self.width())))


class Dock(QFrame):
    """The controls in one rounded bar floating over the bottom of the video."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("bar")
        self.radius = 22

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor(255, 255, 255, 36), 1))
        p.setBrush(QColor(14, 16, 22, 205))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), self.radius, self.radius)


def cover_crop(img, w, h, radius):
    """img cut to fill w×h (never stretched), with rounded corners."""
    out = QPixmap(w * 2, h * 2)
    out.setDevicePixelRatio(2)
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, w, h), radius, radius)
    p.setClipPath(path)
    scaled = img.scaled(w * 2, h * 2, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    p.drawImage(QRectF(0, 0, w, h), scaled, QRectF((scaled.width() - w * 2) / 2, (scaled.height() - h * 2) / 2,
                                                   w * 2, h * 2))
    p.end()
    return out


class EpisodeRow(QWidget):
    """One episode in the episode list: picture, number, title, a note (Playing / Watched / Up next / Filler /
    Not downloaded) and how far you got."""
    clicked = Signal()
    H = 74

    def __init__(self, row):
        super().__init__()
        self.row, self.img, self.hover, self.lit = row, None, False, False
        self.setFixedHeight(self.H)
        self.setCursor(Qt.PointingHandCursor)
        self.setMouseTracking(True)

    def enterEvent(self, e):
        self.hover = True
        self.update()

    def leaveEvent(self, e):
        self.hover = False
        self.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()

    def paintEvent(self, _):
        r = self.row
        state = r["state"]
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        if state == "now" or self.hover or self.lit:
            p.setBrush(QColor(124, 108, 255, 52) if state == "now" else QColor(255, 255, 255, 18 if self.hover else 30))
            p.drawRoundedRect(QRectF(self.rect()).adjusted(0, 1, 0, -1), 14, 14)
        if self.lit:
            p.setPen(QPen(QColor(ACCENT), 1.5))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(QRectF(self.rect()).adjusted(1, 2, -1, -2), 13, 13)
            p.setPen(Qt.NoPen)
        if state == "seen":
            p.setOpacity(0.55)
        thumb = QRectF(8, 8, 104, 58)
        clip = QPainterPath()
        clip.addRoundedRect(thumb, 9, 9)
        p.save()
        p.setClipPath(clip)
        if self.img is not None and state != "missing":
            p.drawPixmap(thumb.toRect(), self.img)
        elif state == "missing":
            p.fillRect(thumb, QColor("#1b2030"))
            p.setPen(QPen(QColor("#252b3c"), 6))
            for x in range(-60, 120, 14):
                p.drawLine(QPointF(thumb.left() + x, thumb.bottom()), QPointF(thumb.left() + x + 60, thumb.top()))
            p.setPen(QColor(TEXT))
            f = QFont(self.font())
            f.setPixelSize(12)
            f.setWeight(QFont.DemiBold)
            p.setFont(f)
            p.drawText(thumb, Qt.AlignCenter, "Find")
        else:
            g = QLinearGradient(thumb.topLeft(), thumb.bottomRight())
            g.setColorAt(0, QColor("#2c3448"))
            g.setColorAt(1, QColor("#1d2230"))
            p.fillRect(thumb, g)
            if r.get("ep") is not None:
                f = QFont(self.font())
                f.setPixelSize(22)
                f.setWeight(QFont.Bold)
                p.setFont(f)
                p.setPen(QColor(255, 255, 255, 60))
                p.drawText(thumb, Qt.AlignCenter, f"{r['ep']:g}" if isinstance(r["ep"], float) else str(r["ep"]))
        if r.get("prog"):
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, 120))
            p.drawRect(QRectF(thumb.left(), thumb.bottom() - 3, thumb.width(), 3))
            p.setBrush(QColor(ACCENT))
            p.drawRect(QRectF(thumb.left(), thumb.bottom() - 3, thumb.width() * min(1.0, r["prog"]), 3))
        p.restore()
        x, right = 124, self.width() - (30 if state == "seen" else 10)
        f = QFont(self.font())
        f.setPixelSize(11)
        f.setWeight(QFont.Bold)
        f.setLetterSpacing(QFont.AbsoluteSpacing, 0.6)
        p.setFont(f)
        p.setPen(QColor("#b9adff") if state == "now" else QColor("#8a92a6"))
        p.drawText(QRectF(x, 11, right - x, 16), Qt.AlignLeft | Qt.AlignVCenter, r.get("num") or "")
        f = QFont(self.font())
        f.setPixelSize(13)
        f.setWeight(QFont.DemiBold)
        p.setFont(f)
        p.setPen(QColor(TEXT))
        p.drawText(QRectF(x, 28, right - x, 18), Qt.AlignLeft | Qt.AlignVCenter,
                   p.fontMetrics().elidedText(r.get("title") or "", Qt.ElideRight, int(right - x)))
        f = QFont(self.font())
        f.setPixelSize(11)
        p.setFont(f)
        p.setPen(QColor(r.get("note_color") or "#8a92a6"))
        p.drawText(QRectF(x, 47, right - x, 16), Qt.AlignLeft | Qt.AlignVCenter,
                   p.fontMetrics().elidedText(r.get("note") or "", Qt.ElideRight, int(right - x)))
        if state == "seen":
            p.setOpacity(1.0)
            p.setPen(QPen(QColor("#b9adff"), 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            cx, cy = self.width() - 18, self.H / 2
            path = QPainterPath(QPointF(cx - 5, cy))
            path.lineTo(cx - 1.5, cy + 3.5)
            path.lineTo(cx + 5, cy - 4)
            p.drawPath(path)


class ThinLine(QWidget):
    """How much of the season you've watched: a thin accent line."""

    def __init__(self):
        super().__init__()
        self.setFixedHeight(3)
        self.frac = 0.0

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 26))
        p.drawRoundedRect(QRectF(self.rect()), 1.5, 1.5)
        if self.frac > 0:
            p.setBrush(QColor(ACCENT))
            p.drawRoundedRect(QRectF(0, 0, self.width() * min(1.0, self.frac), 3), 1.5, 1.5)


class EpisodePanel(QFrame):
    """The episode list beside the video (the dock's Episodes button, or E): every season on disk plus the aired
    episodes you don't have, the one playing highlighted. The video keeps playing next to it."""
    play = Signal(int)          # playlist index
    find = Signal(int, float)   # a missing episode: AniList id, episode
    closed = Signal()
    need_pics = Signal(list)    # keys of rows that are on screen without a picture yet

    def __init__(self, view):
        super().__init__(view)
        self.setObjectName("eppanel")
        self.setStyleSheet(
            "QFrame#eppanel { background: rgba(14,16,22,0.9); border: 1px solid rgba(255,255,255,0.14);"
            " border-radius: 22px; }"
            " QLabel { background: transparent; }"
            " QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: none; }"
            " QScrollBar:vertical { background: transparent; width: 8px; margin: 2px; }"
            " QScrollBar::handle:vertical { background: rgba(255,255,255,0.18); border-radius: 3px; min-height: 30px; }"
            " QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }"
            " QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }"
            " QPushButton#pill { background: rgba(255,255,255,0.08); color: #c8ccd6; border-radius: 12px;"
            " padding: 5px 12px; font-weight: 600; font-size: 9pt; }"
            " QPushButton#pill:hover { background: rgba(255,255,255,0.16); }"
            " QPushButton#pill:checked { background: white; color: #0d0f14; }")
        self.view = view
        self.sections, self.key, self.rows, self.pics, self.sel = [], None, [], {}, -1
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 16, 8, 10)
        v.setSpacing(10)
        head = QHBoxLayout()
        head.setContentsMargins(2, 0, 8, 0)
        texts = QVBoxLayout()
        texts.setSpacing(0)
        t = QLabel("Episodes")
        t.setStyleSheet(f"color: {TEXT}; font-size: 11.5pt; font-weight: 700;")
        texts.addWidget(t)
        self.count = QLabel("")
        self.count.setStyleSheet("color: #8a92a6; font-size: 8.5pt;")
        texts.addWidget(self.count)
        head.addLayout(texts, 1)
        close = QPushButton()
        close.setObjectName("corner")
        close.setFixedSize(30, 30)
        close.setStyleSheet("QPushButton { border-radius: 15px; }")
        close.setIcon(icon(CLOSE, px=14))
        close.setToolTip("Close the episode list (E)")
        close.setCursor(Qt.PointingHandCursor)
        close.clicked.connect(self.closed)
        head.addWidget(close, 0, Qt.AlignTop)
        v.addLayout(head)
        self.pill_box = QVBoxLayout()
        self.pill_box.setContentsMargins(2, 0, 8, 0)
        self.pill_box.setSpacing(6)
        v.addLayout(self.pill_box)
        self.line = ThinLine()
        line_row = QHBoxLayout()
        line_row.setContentsMargins(2, 0, 8, 0)
        line_row.addWidget(self.line)
        v.addLayout(line_row)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.verticalScrollBar().valueChanged.connect(lambda _: self.ask_pics())
        self.list = QWidget()
        self.list_lay = QVBoxLayout(self.list)
        self.list_lay.setContentsMargins(0, 0, 4, 0)
        self.list_lay.setSpacing(2)
        self.scroll.setWidget(self.list)
        v.addWidget(self.scroll, 1)
        self.hide()

    def set_sections(self, sections, key):
        """sections: [{key, name, rows: [{state, num, title, note, note_color, prog, ep, index | find, pic}]}]"""
        self.sections = sections
        keys = [s["key"] for s in sections]
        self.show_section(key if key in keys else (keys[0] if keys else None), keep_scroll=key == self.key)

    def show_section(self, key, keep_scroll=False):
        self.key = key
        old = self.scroll.verticalScrollBar().value()
        while self.pill_box.count():   # pills, wrapped into rows
            item = self.pill_box.takeAt(0)
            lay = item.layout()
            while lay is not None and lay.count():
                w = lay.takeAt(0).widget()
                if w is not None:
                    w.deleteLater()
        if len(self.sections) > 1:
            row, used, room = None, 0, max(200, self.width() - 40)
            for s in self.sections:
                b = QPushButton(s["name"])
                b.setObjectName("pill")
                b.setCheckable(True)
                b.setChecked(s["key"] == key)
                b.setCursor(Qt.PointingHandCursor)
                b.setFocusPolicy(Qt.NoFocus)
                b.clicked.connect(lambda _=False, k=s["key"]: self.show_section(k))
                w = b.sizeHint().width() + 6
                if row is None or used + w > room:
                    row = QHBoxLayout()
                    row.setSpacing(6)
                    row.addStretch(1)
                    self.pill_box.addLayout(row)
                    used = 0
                row.insertWidget(row.count() - 1, b)
                used += w
        sec = next((s for s in self.sections if s["key"] == key), None)
        rows = sec["rows"] if sec else []
        local = [r for r in rows if r["state"] != "missing"]
        seen = sum(1 for r in rows if r["state"] == "seen")
        missing = len(rows) - len(local)
        bits = [f"{seen} of {len(rows)} watched" if seen else f"{len(rows)} episode{'s' if len(rows) != 1 else ''}"]
        if missing:
            bits.append(f"{missing} not downloaded")
        self.count.setText("  ·  ".join(bits))
        self.line.frac = seen / len(rows) if rows else 0
        self.line.update()
        while self.list_lay.count():
            it = self.list_lay.takeAt(0)
            if it.widget() is not None:
                it.widget().deleteLater()
        self.rows, self.sel = [], -1
        for r in rows:
            w = EpisodeRow(r)
            w.setFocusPolicy(Qt.NoFocus)
            w.img = self.pics.get(r.get("pic_key"))
            w.clicked.connect(lambda r=r: self.activate(r))
            self.list_lay.addWidget(w)
            self.rows.append(w)
        self.list_lay.addStretch(1)
        for w in self.findChildren(QWidget):
            w.setFocusPolicy(Qt.NoFocus)
        if keep_scroll:
            QTimer.singleShot(0, lambda: self.scroll.verticalScrollBar().setValue(old))
        else:
            now = next((i for i, w in enumerate(self.rows) if w.row["state"] == "now"), None)
            QTimer.singleShot(0, lambda: self.scroll_to(now if now is not None else 0))
        QTimer.singleShot(30, self.ask_pics)

    def scroll_to(self, i):
        if 0 <= i < len(self.rows):
            bar = self.scroll.verticalScrollBar()
            y = self.rows[i].y() - (self.scroll.viewport().height() - EpisodeRow.H) // 3
            bar.setValue(max(0, y))
        self.ask_pics()

    def activate(self, r):
        if r["state"] == "missing":
            self.find.emit(r["find"][0], r["find"][1])
        elif r.get("index") is not None:
            self.play.emit(r["index"])

    def ask_pics(self):
        """Pictures for the rows you can see (and a few around them): the ones without one are asked for."""
        if not self.isVisible() or not self.rows:
            return
        top = self.scroll.verticalScrollBar().value()
        h = self.scroll.viewport().height()
        first = max(0, top // (EpisodeRow.H + 2) - 3)
        last = min(len(self.rows), (top + h) // (EpisodeRow.H + 2) + 6)
        want = [w.row["pic_key"] for w in self.rows[first:last]
                if w.row.get("pic_key") and w.row["pic_key"] not in self.pics and w.row.get("pic")]
        if want:
            self.need_pics.emit(want)

    def set_pic(self, key, img):
        pm = cover_crop(img, 104, 58, 9)
        self.pics[key] = pm
        for w in self.rows:
            if w.row.get("pic_key") == key:
                w.img = pm
                w.update()

    def pad_move(self, step):
        if not self.rows:
            return
        if 0 <= self.sel < len(self.rows):
            self.rows[self.sel].lit = False
            self.rows[self.sel].update()
        if self.sel < 0:
            now = next((i for i, w in enumerate(self.rows) if w.row["state"] == "now"), 0)
            self.sel = now
        else:
            self.sel = max(0, min(len(self.rows) - 1, self.sel + step))
        w = self.rows[self.sel]
        w.lit = True
        w.update()
        self.scroll.ensureWidgetVisible(w, 0, EpisodeRow.H)

    def pad_activate(self):
        if 0 <= self.sel < len(self.rows):
            self.activate(self.rows[self.sel].row)
        else:
            self.pad_move(0)


def clock(ts):
    return time.strftime("%H:%M", time.localtime(ts))


class LiveBanner(QFrame):
    """Bottom left when you tune in: channel, what's on, its times and progress, what's next."""

    def __init__(self, view):
        super().__init__(view)
        self.setObjectName("livebanner")
        self.setStyleSheet("#livebanner { background: rgba(14,16,22,0.93); border: 1px solid rgba(255,255,255,0.12);"
                           " border-radius: 14px; } QLabel { background: transparent; color: white; }")
        h = QHBoxLayout(self)
        h.setContentsMargins(16, 14, 20, 14)
        h.setSpacing(16)
        self.num = QLabel("")
        self.num.setFixedWidth(64)
        self.num.setAlignment(Qt.AlignCenter)
        h.addWidget(self.num)
        col = QVBoxLayout()
        col.setSpacing(3)
        self.chan = QLabel("")
        self.chan.setStyleSheet("color: rgba(255,255,255,0.65); font-size: 9pt; font-weight: 600;")
        self.what = QLabel("")
        self.what.setStyleSheet("font-size: 14pt; font-weight: 700;")
        self.sub = QLabel("")
        self.sub.setStyleSheet("color: rgba(255,255,255,0.75);")
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(4)
        self.next = QLabel("")
        self.next.setStyleSheet("color: rgba(255,255,255,0.6); font-size: 9pt;")
        for w in (self.chan, self.what, self.sub, self.bar, self.next):
            col.addWidget(w)
        h.addLayout(col, 1)
        self.timer = QTimer(self, singleShot=True, timeout=self.hide)
        self.hide()

    def show_for(self, ch, prog, nxt, ms=6000):
        colour = ch.get("colour") or ACCENT
        self.num.setText(str(ch.get("number", "")))
        self.num.setStyleSheet(f"background: {colour}; color: white; border-radius: 12px; font-size: 20pt;"
                               " font-weight: 800; padding: 8px 0;")
        self.chan.setText(ch.get("name", "").upper())
        self.what.setText(prog.get("title") or "")
        bits = []
        if prog.get("kind") == "episode" and prog.get("ep") is not None:
            bits.append((f"Season {prog['season']} · " if prog.get("season") else "") + f"Episode {prog['ep']}")
        bits.append(f"{clock(prog['start'])} – {clock(prog['end'])}")
        self.sub.setText("  ·  ".join(bits))
        self.bar.setStyleSheet(f"QProgressBar {{ background: rgba(255,255,255,0.15); border: none; border-radius: 2px; }}"
                               f" QProgressBar::chunk {{ background: {colour}; border-radius: 2px; }}")
        self.update_bar(prog)
        self.next.setText(f"Next: {nxt.get('title')} at {clock(nxt['start'])}" if nxt else "")
        self.adjustSize()
        self.setFixedWidth(max(460, min(640, self.sizeHint().width())))
        self.show()
        self.raise_()
        self.timer.start(ms)

    def update_bar(self, prog):
        span = max(1.0, prog["end"] - prog["start"])
        self.bar.setValue(int(1000 * min(1.0, max(0.0, (time.time() - prog["start"]) / span))))


class LiveCard(QWidget):
    """Fills the screen between shows ("Up next") and when a trailer can't play."""

    def __init__(self, view):
        super().__init__(view)
        self.img, self.title, self.small, self.colour, self.until = None, "", "", ACCENT, 0
        self.timer = QTimer(self, interval=500, timeout=self.update)
        self.hide()

    def show_card(self, title, small, img=None, colour=None, until=0):
        self.title, self.small, self.img, self.colour, self.until = title, small, img, colour or ACCENT, until
        self.setGeometry(self.parentWidget().rect())
        self.show()
        self.raise_()
        self.timer.start()
        self.update()

    def hideEvent(self, e):
        self.timer.stop()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect()
        p.fillRect(r, QColor(10, 11, 15))
        if self.img is not None and not self.img.isNull():   # the cover, large and dim behind
            big = self.img.scaled(r.size(), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            p.setOpacity(0.18)
            p.drawImage((r.width() - big.width()) // 2, (r.height() - big.height()) // 2, big)
            p.setOpacity(1.0)
            h = int(r.height() * 0.42)
            poster = self.img.scaledToHeight(h, Qt.SmoothTransformation)
            x = r.width() // 2 - poster.width() - 40
            path = QPainterPath()
            path.addRoundedRect(QRectF(x, (r.height() - h) / 2, poster.width(), h), 14, 14)
            p.setClipPath(path)
            p.drawImage(x, (r.height() - h) // 2, poster)
            p.setClipping(False)
            tx = r.width() // 2
        else:
            tx = r.width() // 2 - 260
        y = r.height() // 2 - 40
        f = QFont(self.font())
        f.setPointSizeF(11)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(self.colour))
        p.drawText(tx, y, self.small.upper())
        f.setPointSizeF(24)
        p.setFont(f)
        p.setPen(QColor("white"))
        p.drawText(QRectF(tx, y + 12, max(300, r.width() - tx - 60), 120), Qt.TextWordWrap, self.title)
        if self.until:
            left = max(0, int(self.until - time.time()))
            f.setPointSizeF(12)
            f.setBold(False)
            p.setFont(f)
            p.setPen(QColor(255, 255, 255, 150))
            p.drawText(tx, y + 150, f"Starts in {left // 60}:{left % 60:02d}" if left >= 60 else f"Starts in {left} s")


class CaptureCard(QFrame):
    """Bottom right: the screenshot just taken / the clip being made, with Copy and Folder."""

    def __init__(self, view):
        super().__init__(view)
        self.view = view
        self.path = None
        self.setObjectName("capcard")
        self.setStyleSheet("#capcard { background: rgba(18,20,26,0.92); border: 1px solid rgba(255,255,255,0.12);"
                           " border-radius: 12px; } QLabel { background: transparent; }"
                           " #capcard QPushButton { padding: 4px 10px; font-size: 9pt; }")
        h = QHBoxLayout(self)
        h.setContentsMargins(10, 10, 12, 10)
        h.setSpacing(10)
        self.thumb = QLabel()
        self.thumb.setFixedSize(112, 63)
        self.thumb.setStyleSheet("background: rgba(255,255,255,0.06); border-radius: 6px;")
        h.addWidget(self.thumb)
        col = QVBoxLayout()
        col.setSpacing(4)
        self.title = QLabel("")
        self.title.setStyleSheet("color: white; font-weight: 600;")
        self.name = QLabel("")
        self.name.setStyleSheet("color: rgba(255,255,255,0.6); font-size: 8.5pt;")
        self.name.setMaximumWidth(230)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(4)
        self.bar.setStyleSheet(f"QProgressBar {{ background: rgba(255,255,255,0.15); border: none; border-radius: 2px; }}"
                               f" QProgressBar::chunk {{ background: {ACCENT}; border-radius: 2px; }}")
        col.addWidget(self.title)
        col.addWidget(self.name)
        col.addWidget(self.bar)
        row = QHBoxLayout()
        row.setSpacing(6)
        self.copy_btn = QPushButton("Copy")
        self.copy_btn.clicked.connect(self.copy)
        self.folder_btn = QPushButton("Folder")
        self.folder_btn.clicked.connect(self.reveal)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.cancel)
        for b in (self.copy_btn, self.folder_btn, self.cancel_btn):
            b.setCursor(Qt.PointingHandCursor)
            b.setFocusPolicy(Qt.NoFocus)
            row.addWidget(b)
        row.addStretch(1)
        col.addLayout(row)
        h.addLayout(col)
        self.timer = QTimer(self, singleShot=True, timeout=self.hide)
        self.job = None
        self.hide()

    def set_thumb(self, qimg):
        if qimg is None or qimg.isNull():
            self.thumb.clear()
            return
        pm = QPixmap.fromImage(qimg.scaled(112, 63, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation))
        self.thumb.setPixmap(pm.copy(max(0, (pm.width() - 112) // 2), max(0, (pm.height() - 63) // 2), 112, 63))

    def working(self, title, name, job, qimg=None):
        self.job, self.path = job, None
        self.title.setText(title)
        self.name.setText(name)
        self.set_thumb(qimg)
        self.bar.setValue(0)
        self.bar.show()
        self.copy_btn.hide()
        self.folder_btn.hide()
        self.cancel_btn.show()
        self.timer.stop()
        self.popup()

    def progress(self, f):
        self.bar.setValue(int(f * 100))

    def done(self, title, path, qimg=None, error=None):
        self.job, self.path = None, path
        self.title.setText(title)
        self.name.setText(error or (Path(path).name if path else ""))
        self.name.setToolTip(str(path or ""))
        if qimg is not None:
            self.set_thumb(qimg)
        self.bar.hide()
        self.cancel_btn.hide()
        self.copy_btn.setVisible(bool(path))
        self.folder_btn.setVisible(bool(path))
        self.popup()
        self.timer.start(6000 if not error else 9000)

    def popup(self):
        self.adjustSize()
        self.view.place_floating()
        self.show()
        self.raise_()

    def copy(self):
        if not self.path:
            return
        cb = QApplication.clipboard()
        if Path(self.path).suffix.lower() in (".png", ".jpg", ".webp", ".avif") and \
                Path(self.path).suffix.lower() != ".avif":
            cb.setImage(QImage(str(self.path)))
        else:   # a file (pastes into Discord, Explorer...)
            from PySide6.QtCore import QMimeData, QUrl
            md = QMimeData()
            md.setUrls([QUrl.fromLocalFile(str(self.path))])
            cb.setMimeData(md)
        self.view.show_osd("Copied")

    def reveal(self):
        if self.path and os.name == "nt":
            import subprocess
            subprocess.Popen(["explorer", "/select,", os.path.normpath(str(self.path))])

    def cancel(self):
        if self.job is not None:
            self.job.requestInterruption()

    def enterEvent(self, e):
        self.timer.stop()

    def leaveEvent(self, e):
        if self.job is None and self.isVisible():
            self.timer.start(3000)


class ClipJob(QThread):
    """Renders one clip (capture.render_clip) or a contact sheet on a worker thread."""
    progressed = Signal(float)
    finished_job = Signal(str, str)   # path, error ("" = fine)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self):
        try:
            path = self.fn(self.progressed.emit, self.isInterruptionRequested)
            self.finished_job.emit(str(path), "")
        except InterruptedError:
            self.finished_job.emit("", "cancelled")
        except Exception as e:
            self.finished_job.emit("", str(e) or type(e).__name__)


class ClipBar(QFrame):
    """The clip editor above the controls: start / end (I / O, or drag the marks on the seek bar), frame steps,
    a looping preview, the format and size, then Save (Enter)."""

    def __init__(self, view):
        super().__init__(view)
        self.view = view
        self.start = self.end = 0.0
        self.setObjectName("clipbar")
        self.setStyleSheet("#clipbar { background: rgba(18,20,26,0.94); border: 1px solid rgba(255,255,255,0.12);"
                           " border-radius: 12px; } QLabel { background: transparent; color: white; }"
                           " #clipbar QPushButton { padding: 5px 10px; font-size: 9pt; }"
                           f" #clipbar QPushButton#save {{ background: {ACCENT}; border: none; color: white;"
                           " font-weight: 600; padding: 5px 16px; }"
                           " #clipbar QPushButton:checked { background: rgba(124,108,255,0.45); }")
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 10, 14, 10)
        v.setSpacing(8)
        r1 = QHBoxLayout()
        r1.setSpacing(6)
        t = QLabel("Clip")
        t.setStyleSheet("font-weight: 700; font-size: 11pt;")
        r1.addWidget(t)
        r1.addSpacing(8)
        self.start_btn = self._b(r1, "Start 0:00", "Start here (I)", self.set_start)
        self.back_btn = self._b(r1, "◂", "One frame back (,)", lambda: self.view.frame_step(-1))
        self.fwd_btn = self._b(r1, "▸", "One frame forward (.)", lambda: self.view.frame_step(1))
        self.end_btn = self._b(r1, "End 0:00", "End here (O)", self.set_end)
        self.length = QLabel("")
        self.length.setStyleSheet("color: rgba(255,255,255,0.7);")
        r1.addSpacing(6)
        r1.addWidget(self.length)
        r1.addStretch(1)
        self.loop_btn = self._b(r1, "Loop preview", "Play the clip on a loop (L)", self.toggle_loop)
        self.loop_btn.setCheckable(True)
        v.addLayout(r1)
        r2 = QHBoxLayout()
        r2.setSpacing(6)
        self.fmt_btn = self._b(r2, "", "Format", lambda: self.choose("clip_format"))
        self.size_btn = self._b(r2, "", "Size", lambda: self.choose("clip_size"))
        self.limit_btn = self._b(r2, "", "Fit under a file size", lambda: self.choose("clip_limit"))
        self.subs_btn = self._b(r2, "Subtitles", "Burn in the subtitles you see (with your Style)",
                                lambda: self.toggle_opt("clip_subs"))
        self.subs_btn.setCheckable(True)
        self.audio_btn = self._b(r2, "Sound", "Keep the sound", lambda: self.toggle_opt("clip_audio"))
        self.audio_btn.setCheckable(True)
        self.enh_btn = self._b(r2, "Enhanced", "With your picture enhancement (Anime4K etc.), upscaled",
                               lambda: self.toggle_opt("clip_enhanced"))
        self.enh_btn.setCheckable(True)
        self.est = QLabel("")
        self.est.setStyleSheet("color: rgba(255,255,255,0.6);")
        r2.addSpacing(6)
        r2.addWidget(self.est)
        r2.addStretch(1)
        self._b(r2, "Cancel", "Close (Esc)", self.close_bar)
        self.save_btn = self._b(r2, "Save clip", "Make the clip (Enter)", self.save)
        self.save_btn.setObjectName("save")
        v.addLayout(r2)
        for w in self.findChildren(QWidget):
            w.setFocusPolicy(Qt.NoFocus)
        self.hide()

    def _b(self, row, text, tip, fn):
        b = QPushButton(text)
        b.setToolTip(tip)
        b.setCursor(Qt.PointingHandCursor)
        b.clicked.connect(fn)
        row.addWidget(b)
        return b

    @property
    def settings(self):
        return self.view.settings

    def open(self):
        pos = float(self.view.mpv.time_pos or 0)
        dur = float(self.view.mpv.duration or 0)
        self.start, self.end = pos, min(dur or pos + 15, pos + 15)
        self.loop_btn.setChecked(False)
        self.refresh()
        self.show()
        self.view.show_controls()
        self.view.place_floating()
        self.raise_()

    def close_bar(self):
        self.view.set_loop(None)
        self.hide()
        self.view.seekbar.clip_range = None
        self.view.seekbar.update()
        self.view.place_floating()

    def set_start(self, t=None):
        self.start = float(self.view.mpv.time_pos or 0) if t is None or t is False else t
        if self.end <= self.start:
            self.end = min(float(self.view.mpv.duration or self.start + 15), self.start + 15)
        self.refresh()

    def set_end(self, t=None):
        self.end = float(self.view.mpv.time_pos or 0) if t is None or t is False else t
        if self.end <= self.start:
            self.start = max(0.0, self.end - 15)
        self.refresh()

    def toggle_loop(self):
        self.view.set_loop((self.start, self.end) if self.loop_btn.isChecked() else None)

    def toggle_opt(self, key):
        self.settings[key] = not capture.opt(self.settings, key)
        self.view.save_settings(self.settings)
        self.refresh()

    def choose(self, key):
        cur = capture.opt(self.settings, key)
        if key == "clip_format":
            options = [(k, v[0]) for k, v in capture.CLIP_FORMATS.items()]
        elif key == "clip_size" and self.enh_btn.isVisible() and self.enh_btn.isChecked():
            key, cur = "clip_enh_size", capture.opt(self.settings, "clip_enh_size")
            options = [("1080", "1080p (enhanced)"), ("1440", "1440p (upscaled)"), ("2160", "4K (upscaled)")]
        elif key == "clip_size":
            options = [("original", "Original size"), ("1080", "1080p"), ("720", "720p"), ("480", "480p")]
        else:
            options = [(0, "No size limit"), (8, "Under 8 MB"), (10, "Under 10 MB (Discord)"), (25, "Under 25 MB"),
                       (50, "Under 50 MB (Discord Nitro Basic)"), (100, "Under 100 MB")]

        def pick(val):
            self.settings[key] = val
            self.view.save_settings(self.settings)
            self.refresh()
        self.view.open_menu([(text, lambda v=val: pick(v), val == cur) for val, text in options],
                            {"clip_format": self.fmt_btn, "clip_size": self.size_btn,
                             "clip_enh_size": self.size_btn}.get(key, self.limit_btn))

    def refresh(self):
        st = self.settings
        self.start_btn.setText(f"Start {fmt_time(self.start)}")
        self.end_btn.setText(f"End {fmt_time(self.end)}")
        dur = max(0.0, self.end - self.start)
        self.length.setText(f"{dur:.1f} s")
        fmt = capture.opt(st, "clip_format")
        self.fmt_btn.setText(capture.CLIP_FORMATS.get(fmt, capture.CLIP_FORMATS["h264"])[0].split(" (")[0])
        size = capture.opt(st, "clip_size")
        self.size_btn.setText("Original size" if size == "original" else f"{size}p")
        limit = capture.opt(st, "clip_limit")
        self.limit_btn.setText(f"Under {limit} MB" if limit else "No size limit")
        gif = fmt in ("gif", "webp")
        self.size_btn.setVisible(not gif and fmt != "copy")
        self.subs_btn.setVisible(fmt != "copy")
        self.subs_btn.setChecked(bool(capture.opt(st, "clip_subs")))
        self.audio_btn.setVisible(not gif)
        self.audio_btn.setChecked(bool(capture.opt(st, "clip_audio")))
        can_enh = fmt in capture.VIDEO_FORMATS and self.view.enhanced()
        self.enh_btn.setVisible(can_enh)
        self.enh_btn.setChecked(bool(capture.opt(st, "clip_enhanced")))
        enh_on = can_enh and bool(capture.opt(st, "clip_enhanced"))
        if enh_on:
            self.size_btn.setText(f"{capture.opt(st, 'clip_enh_size')}p (upscaled)")
        src_rate = None
        try:
            src_rate = os.path.getsize(self.view.current["path"]) / 1048576 / max(1.0, float(self.view.mpv.duration or 1))
        except (OSError, TypeError, KeyError):
            pass
        mb = capture.estimate(st, dur, int(self.view.mpv.height or 1080), src_rate, enh_on)
        self.est.setText(f"about {mb:.1f} MB" if mb >= 1 else f"about {mb * 1024:.0f} KB")
        self.save_btn.setEnabled(dur >= 0.2)
        self.view.seekbar.clip_range = (self.start, self.end)
        self.view.seekbar.update()
        if self.loop_btn.isChecked():
            self.view.set_loop((self.start, self.end))
        self.adjustSize()

    def save(self):
        if self.end - self.start < 0.2:
            return
        start, end = self.start, self.end
        self.close_bar()
        self.view.start_clip(start, end)


def background_done(path, err):
    """A clip finished after the player closed: problems go to kura.log (there's no card to show them)."""
    import logging
    if err:
        logging.getLogger("kura").warning(f"Clip made after the player closed failed: {err}")
    else:
        logging.getLogger("kura").info(f"Clip saved after the player closed: {path}")


def run_background(works):
    """Clips still waiting when the player closed: made one after another."""
    if not works:
        return
    job = ClipJob(works.pop(0))
    job.finished_job.connect(background_done)
    BACKGROUND.append(job)
    job.finished.connect(lambda j=job: (BACKGROUND.remove(j) if j in BACKGROUND else None, run_background(works)))
    job.start()


class PlayerView(QWidget):
    """The player. It lives inside Kura's window (Kura turns into the player) or in a
    PopoutWindow, and can move between them while playing. open(playlist, index) plays
    playlist[index] = {path, mid, episode, title, subtitle}; Kura listens to `progress`
    to update your list and Discord."""
    progress = Signal(dict)       # every 2 s: path, mid, episode, pos, duration, paused
    seek_done = Signal()          # an async seek finished (sent from mpv's thread)
    item_finished = Signal(dict)  # an episode stopped playing (closed, next, ended)
    close_requested = Signal()    # the close / back button
    popout_toggled = Signal()     # pop out of Kura, or back in
    mini_toggled = Signal()       # the mini player (small, on top, in a corner) or back
    thumb_ready = Signal(str, float, QImage)   # a seek bar frame (from ThumbSeeker's thread)
    on_top_toggled = Signal(bool)
    find_subs = Signal(str)       # "Find subtitles…" in the subtitle menu: this file
    find_episode = Signal(int, float)   # a missing episode clicked in the episode list: AniList id, episode
    title_changed = Signal(str)
    marks_ready = Signal(str, list)

    def __init__(self, mpv_mod, settings, save_settings, state_file, mal_lookup=None):
        super().__init__(None)
        self.settings, self.save_settings = settings, save_settings
        self.mpv_mod = mpv_mod
        self.auto_streak, self.tracks_applied, self.seeker, self.drag = 0, None, None, None
        self.state_file, self.mal_lookup = Path(state_file), mal_lookup
        self.playlist, self.index, self.current = [], -1, None
        self.marks, self.chapters_done, self.skipped = [], False, set()
        self.resume_pending, self.last_save, self.last_emit, self.ended_at = False, 0, 0, None
        self.mode, self.on_top = "embedded", False
        self.setMinimumSize(480, 270)
        self.setAttribute(Qt.WA_StyledBackground)
        self.setStyleSheet(STYLE + "PlayerView { background: black; }")
        self.setMouseTracking(True)

        s = settings
        # GPU decoding with frames copied back and converted to 3-plane yuv420p10: mpv's 2-plane
        # NV12/P010 upload is garbled in Qt's GL context (noise band), the 3-plane path is clean
        # and keeps 10-bit releases at full quality.
        decode = ({"hwdec": "auto-copy-safe", "vf": "format=yuv420p10"} if s.get("player_hwdec", True)
                  else {"hwdec": "no", "vf": "format=yuv420p10"})
        self.base_vf = decode["vf"]
        extra = {}
        if PICTURE is not None:   # compiled shaders are kept, so switching upscalers is quick after the first time
            try:
                PICTURE.CACHE_DIR.mkdir(parents=True, exist_ok=True)
                extra["gpu_shader_cache_dir"] = str(PICTURE.CACHE_DIR)
            except OSError:
                pass
        # debanding as tuned for anime (smooth skies and dark gradients without smearing detail)
        extra.update(deband_iterations=2, deband_threshold=40, deband_range=20, deband_grain=6)
        self.picture_session, self.picture_applied, self.comparing, self.last_mode = None, None, False, "a4k_a"
        self.video = MpvWidget(
            mpv_mod, self, keep_open="yes", input_default_bindings=False, osc=False, **decode, **extra,
            input_vo_keyboard=False, alang=s.get("player_audio_lang", "jpn,ja,eng"),
            slang=s.get("player_sub_lang", "eng,en"), sub_auto="fuzzy", volume=s.get("player_volume", 100),
            volume_max=130, ytdl=False)
        self.mpv = self.video.mpv
        self.video.installEventFilter(self)
        self.marks_ready.connect(self.on_marks)

        # top: round buttons in the corner (the title is in the dock)
        self.top = Bar(self, fade_down=True)
        self.top.dark = 110
        tl = QHBoxLayout(self.top)
        tl.setContentsMargins(20, 18, 20, 10)
        tl.setSpacing(8)
        tl.addStretch(1)
        self.badge_slot = QHBoxLayout()   # Live TV's LIVE badge goes here
        tl.addLayout(self.badge_slot)
        self.pin_btn = self._corner(tl, lambda: self.on_top_toggled.emit(not self.on_top))
        self.mini_btn = self._corner(tl, self.mini_toggled)
        self.pop_btn = self._corner(tl, self.popout_toggled)
        close = self._corner(tl, self.close_requested)
        close.setIcon(icon(CLOSE, px=16))
        close.setToolTip("Close player")

        # bottom: the dock (seek bar, then the show · play controls · the rest)
        self.bottom = Dock(self)
        bl = QVBoxLayout(self.bottom)
        bl.setContentsMargins(18, 12, 18, 10)
        bl.setSpacing(4)
        seek_row = QHBoxLayout()
        seek_row.setSpacing(8)
        self.time_lbl = QLabel("0:00")
        self.time_lbl.setObjectName("time")
        self.time_lbl.setMinimumWidth(40)
        seek_row.addWidget(self.time_lbl)
        self.seekbar = SeekBar(self.bottom, self)
        self.seekbar.seek.connect(self.on_seek)
        seek_row.addWidget(self.seekbar, 1)
        self.dur_lbl = QLabel("0:00")
        self.dur_lbl.setObjectName("time")
        self.dur_lbl.setMinimumWidth(40)
        self.dur_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        seek_row.addWidget(self.dur_lbl)
        bl.addLayout(seek_row)
        row = QHBoxLayout()
        row.setSpacing(0)
        left = QHBoxLayout()
        left.setSpacing(12)
        self.cover = QLabel()
        self.cover.setFixedSize(40, 56)
        self.cover.hide()
        left.addWidget(self.cover)
        texts = QVBoxLayout()
        texts.setSpacing(0)
        self.title = ElideLabel("")
        self.title.setObjectName("title")
        self.subtitle = ElideLabel("")
        self.subtitle.setObjectName("sub")
        texts.addStretch(1)
        texts.addWidget(self.title)
        texts.addWidget(self.subtitle)
        texts.addStretch(1)
        left.addLayout(texts, 1)
        left.addSpacing(10)
        mid = QHBoxLayout()
        mid.setSpacing(6)
        short, long = int(s.get("player_skip_short", 10)), int(s.get("player_skip_long", 85))
        self.prev_btn = self._btn(mid, PREV, "Previous episode (P)", lambda: self.step_episode(-1))
        b1 = self._round(mid, f"−{short}", f"Back {short} seconds (←)", lambda: self.skip(-short))
        self.play_btn = self._btn(mid, PAUSE, "Play / pause (Space)", self.toggle_pause, px=22)
        self.play_btn.setIcon(icon(PAUSE, "#0d0f14", px=22))
        self.play_btn.setObjectName("bigplay")
        self.play_btn.setFixedSize(54, 54)
        b2 = self._round(mid, f"+{short}", f"Forward {short} seconds (→)", lambda: self.skip(short))
        b3 = self._round(mid, f"+{long}", f"Forward {long} seconds (Shift+→)", lambda: self.skip(long))
        self.next_btn = self._btn(mid, NEXT, "Next episode (N)", lambda: self.step_episode(1))
        right = QHBoxLayout()
        right.setSpacing(2)
        right.addSpacing(10)
        right.addStretch(1)
        self.ep_btn = self._btn(right, "list", "Episodes (E)", self.toggle_episodes)
        self.ep_btn.setText("Episodes")
        self.ep_btn.setObjectName("epbtn")
        self.ep_btn.setCheckable(True)
        right.addSpacing(6)
        self.speed_btn = self._text_btn(right, "1×", "Playback speed ([ and ])", self.speed_menu)
        self.audio_btn = self._btn(right, AUDIO, "Audio track (A)", lambda: self.track_menu("audio"))
        self.sub_btn = self._btn(right, CC, "Subtitles (S)", lambda: self.track_menu("sub"))
        self.vol_btn = self._btn(right, VOLUME, "Mute (M)", self.toggle_mute)
        self.vol = QSlider(Qt.Horizontal)
        self.vol.setRange(0, 130)
        self.vol.setFixedWidth(80)
        self.vol.setValue(int(s.get("player_volume", 100)))
        self.vol.valueChanged.connect(self.set_volume)
        self.vol.installEventFilter(self)   # the wheel works the same over the slider as over the video
        right.addWidget(self.vol)
        right.addSpacing(4)
        self.full_btn = self._btn(right, FULL, "Fullscreen (F)", self.toggle_fullscreen)
        self.more_btn = self._text_btn(right, "⋯", "More", self.more_menu)
        row.addLayout(left, 1)
        row.addLayout(mid, 0)
        row.addLayout(right, 1)
        bl.addLayout(row)
        self.long_btn = b3
        # the mini player only keeps the basics
        self.mini_hidden = [b1, b2, b3, self.speed_btn, self.audio_btn, self.sub_btn, self.vol, self.full_btn,
                            self.subtitle, self.pin_btn, self.ep_btn, self.dur_lbl]
        self.ep_panel = EpisodePanel(self)
        self.ep_panel.play.connect(lambda i: (self.note_input(), self.play_index(i)))
        self.ep_panel.find.connect(self.find_episode)
        self.ep_panel.closed.connect(self.close_episodes)
        self.ep_panel.need_pics.connect(self.ask_episode_pics)
        self.ep_info, self.ep_info_for = None, None

        self.next_card = NextUpCard(self)   # end of an episode: countdown to the next one
        self.next_card.play_now.connect(self.up_next)
        self.still_card = StillWatchingCard(self)
        self.still_card.keep_going.connect(lambda: (self.note_input(), self.step_episode(1)))
        self.still_card.done.connect(self.close_requested)
        self.thumb = QLabel(self)   # the frame above the seek bar while hovering it
        self.thumb.setObjectName("seekthumb")
        self.thumb.hide()
        self.thumb_time = None
        self.thumb_cache = {}
        self.thumb_ready.connect(self.on_thumb)
        self.skip_btn = QPushButton("", self)
        self.skip_btn.setObjectName("skip")
        self.skip_btn.setCursor(Qt.PointingHandCursor)
        self.skip_btn.clicked.connect(self.skip_mark)
        self.skip_btn.hide()
        self.live = None   # Live TV: {cid, prog, delay, last, typed}
        self.live_card = LiveCard(self)
        self.live_banner = LiveBanner(self)
        self.live_badge = QPushButton("●  LIVE")
        self.badge_slot.addWidget(self.live_badge, 0, Qt.AlignTop)
        self.live_badge.setCursor(Qt.PointingHandCursor)
        self.live_badge.clicked.connect(self.jump_live)
        self.live_badge.hide()
        self.typed_timer = QTimer(self, singleShot=True, interval=1400, timeout=self.typed_done)
        self.cap_card = CaptureCard(self)   # the screenshot / clip just made
        self.clip_bar = ClipBar(self)
        self.clip_jobs = []   # clips waiting their turn (one renders at a time)
        self.clip_running = None
        self.osd = QLabel("", self)
        self.osd.setObjectName("osd")
        self.osd.hide()
        self.osd_timer = QTimer(self, singleShot=True, timeout=self.osd.hide)
        self.hover = QLabel("", self)
        self.hover.setObjectName("osd")
        self.hover.hide()

        self.menu = None   # the OverlayMenu that's open
        # Seek bar: one seek at a time, sent without waiting, always to the latest position;
        # the bar shows where you put it until mpv gets there (it used to snap back)
        self.seek_busy, self.seek_pending, self.seek_hold = False, None, None
        self.seek_done.connect(self._seek_finished)
        for bar in (self.top, self.bottom):
            bar.installEventFilter(self)   # the wheel sets the volume over the control bars too
        self.wheel_acc = 0  # wheel distance not yet turned into a volume step (120 = one notch)
        # mpv gets the volume at most every 15 ms (one setting per wheel event / mouse move was too many)
        self.vol_timer = QTimer(self, singleShot=True, interval=15, timeout=self.apply_volume)
        self.hide_timer = QTimer(self, singleShot=True, interval=2500, timeout=self.hide_controls)
        self.tick_timer = QTimer(self, interval=250, timeout=self.tick)
        self.tick_timer.start()
        self.controls_visible = True
        self.set_mode("embedded")
        self.show_controls()
        # Keys go to the player itself: it takes focus, and nothing inside it can take it away
        # (a focused button would eat Space, the volume slider the arrow keys)
        self.setFocusPolicy(Qt.StrongFocus)
        for w in self.findChildren(QWidget):
            w.setFocusPolicy(Qt.NoFocus)

    # ---- building blocks
    def _btn(self, row, glyph, tip, fn, px=18):
        b = QPushButton()
        b.setIcon(icon(glyph, px=px))
        b.setIconSize(QSize(px, px))
        b.setToolTip(tip)
        b.setCursor(Qt.PointingHandCursor)
        b.clicked.connect(fn)
        row.addWidget(b)
        return b

    def _text_btn(self, row, text, tip, fn):
        b = QPushButton(text)
        b.setToolTip(tip)
        b.setCursor(Qt.PointingHandCursor)
        b.clicked.connect(fn)
        row.addWidget(b)
        return b

    def _round(self, row, text, tip, fn):
        b = self._text_btn(row, text, tip, fn)
        b.setObjectName("round")
        b.setFixedSize(42, 42)
        return b

    def _corner(self, row, fn):
        b = QPushButton()
        b.setObjectName("corner")
        b.setFixedSize(38, 38)
        b.setIconSize(QSize(16, 16))
        b.setCursor(Qt.PointingHandCursor)
        b.clicked.connect(fn)
        row.addWidget(b, 0, Qt.AlignTop)
        return b

    def set_times(self, pos, dur):
        self.time_lbl.setText(fmt_time(pos))
        self.dur_lbl.setText(fmt_time(dur))
        if self.mode == "mini":   # no room for two labels
            self.time_lbl.setText(f"{fmt_time(pos)} / {fmt_time(dur)}")

    def set_cover(self, item):
        img = None
        if COVER is not None and item and not item.get("live"):
            try:
                img = COVER(item)
            except Exception:
                img = None
        if img is None or img.isNull():
            self.cover.clear()
            self.cover.setProperty("has", False)
        else:
            self.cover.setPixmap(cover_crop(img, 40, 56, 7))
            self.cover.setProperty("has", True)
        self.fit_dock()

    def fit_dock(self):
        """What fits in the dock at this width (the mini player has its own list)."""
        if self.mode == "mini":
            self.cover.hide()
            return
        w = self.bottom.width()
        self.cover.setVisible(bool(self.cover.property("has")) and w >= 1000)
        self.long_btn.setVisible(w >= 940)
        self.vol.setVisible(w >= 880)
        self.speed_btn.setVisible(w >= 760)
        self.ep_btn.setText("Episodes" if w >= 820 else "")
        self.title.setVisible(w >= 700)
        self.subtitle.setVisible(w >= 700)

    # ---- the episode list (beside the video)
    def toggle_episodes(self):
        if self.ep_panel.isVisible():
            self.close_episodes()
        else:
            self.open_episodes()

    def open_episodes(self):
        if not self.current or self.live or self.mode == "mini":
            self.ep_btn.setChecked(False)
            return
        if self.menu is not None:
            self.menu.close_menu()
        self.ep_panel.show()
        self.ep_panel.raise_()
        self.ep_btn.setChecked(True)
        self.refresh_episodes(scroll=True)
        self.place_floating()
        self.show_controls()

    def close_episodes(self):
        self.ep_panel.hide()
        self.ep_btn.setChecked(False)
        self.place_floating()
        self.setFocus()

    def refresh_episodes(self, scroll=False):
        """Fill the episode list: the playlist (grouped by season) plus the aired episodes you don't have."""
        if not self.ep_panel.isVisible():
            return
        first = self.playlist[0]["path"] if self.playlist else None
        if self.ep_info_for != (first, len(self.playlist)) or scroll:
            self.ep_info = None
            if EPISODE_INFO is not None:
                try:
                    self.ep_info = EPISODE_INFO(self.playlist)
                except Exception:
                    self.ep_info = None
            if self.ep_info_for is not None and self.ep_info_for[0] != first:
                self.ep_panel.pics = {}
            self.ep_info_for = (first, len(self.playlist))
        info = self.ep_info or {}
        extra = info.get("items") or []
        resume = self._resume_state()
        nxt = self.next_index()
        skip_filler = self.settings.get("player_skip_filler", False)
        sections = {}

        def section(season):
            key = season or 0
            if key not in sections:
                sections[key] = {"key": key, "name": f"Season {season}" if season else "Episodes", "rows": []}
            return sections[key]
        for i, item in enumerate(self.playlist):
            ex = extra[i] if i < len(extra) and extra[i] else {}
            ep = item.get("episode")
            minutes = ex.get("minutes")
            state, prog, note, color = "plain", 0.0, "", None
            saved = resume.get(item["path"]) or {}
            if saved.get("dur"):
                prog = saved.get("pos", 0) / saved["dur"]
            kind = self.episode_kind(item)
            if i == self.index:
                state = "now"
                try:
                    pos, dur = self.mpv.time_pos or 0, self.mpv.duration or 0
                except Exception:
                    pos = dur = 0
                prog = pos / dur if dur else 0
                left = max(1, int((dur - pos) / 60 + 0.5)) if dur else None
                note, color = (f"Playing  ·  {left} min left" if left else "Playing"), "#d6cfff"
            elif ex.get("watched"):
                state, note, prog = "seen", "Watched", 0
            elif kind == "filler":
                note, color = ("Filler  ·  skipped by Up next" if skip_filler else "Filler"), MARK
            elif i == nxt:
                note = "Up next" + (f"  ·  {minutes} min" if minutes else "")
            elif prog > 0.02 and saved.get("dur"):
                note = f"{max(1, int((saved['dur'] - saved.get('pos', 0)) / 60 + 0.5))} min left"
            elif minutes:
                note = f"{minutes} min"
            if kind == "recap" and state == "plain":
                note, color = "Recap", MARK
            ep_txt = (f"{ep:g}" if isinstance(ep, float) else str(ep)) if ep is not None else ""
            if item.get("recap") is not None:
                ep_txt = f"{item['recap']:g}"
            section(item.get("season"))["rows"].append({
                "state": state, "num": f"EPISODE {ep_txt}" if ep_txt else "",
                "title": ex.get("name") or (f"Episode {ep_txt}" if ep_txt else Path(item["path"]).stem),
                "note": note, "note_color": color, "prog": prog if state != "seen" else 0, "ep": ep,
                "index": i, "pic": ex.get("pic"), "pic_key": item["path"],
                "sort": float(item.get("recap") or ep or 0)})
        for m in info.get("missing") or []:
            ep = m["episode"]
            ep_txt = f"{ep:g}" if isinstance(ep, float) else str(ep)
            section(m.get("season"))["rows"].append({
                "state": "missing", "num": f"EPISODE {ep_txt}", "title": m.get("name") or f"Episode {ep_txt}",
                "note": "Not downloaded  ·  Find", "note_color": "#8a92a6", "prog": 0, "ep": ep,
                "find": (m["mid"], float(ep)), "pic": None, "pic_key": None, "sort": float(ep)})
        out = []
        for key in sorted(sections):
            sec = sections[key]
            sec["rows"].sort(key=lambda r: r["sort"])
            out.append(sec)
        cur = (self.playlist[self.index].get("season") or 0) if 0 <= self.index < len(self.playlist) else None
        # the season you picked stays, until another episode starts (it may be in the next season)
        moved = getattr(self, "ep_last_index", None) != self.index
        key = cur if scroll or moved or self.ep_panel.key is None else self.ep_panel.key
        self.ep_last_index = self.index
        self.ep_panel.set_sections(out, key)

    def ask_episode_pics(self, keys):
        if EPISODE_PICS is None:
            return
        want = set(keys)
        jobs = []
        for sec in self.ep_panel.sections:
            for r in sec["rows"]:
                if r.get("pic_key") in want and r.get("pic"):
                    kind, data = r["pic"]
                    jobs.append((r["pic_key"], kind, data))
                    want.discard(r["pic_key"])
        stills = [j for j in jobs if j[1] == "url"]
        try:
            EPISODE_PICS(stills + [j for j in jobs if j[1] != "url"])   # quick ones first
        except Exception:
            pass

    def episode_image(self, key, img):
        """A picture for the episode list arrived (from Kura's picture worker)."""
        if img is not None and not img.isNull():
            self.ep_panel.set_pic(key, img)

    def hover_thumb(self, t, pos):
        """The frame at the time under the mouse, above the seek bar (asked from a hidden mpv)."""
        if t is None or not self.current or not self.settings.get("player_seek_thumbs", True):
            self.thumb.hide()
            self.thumb_time = None
            return
        self.thumb_pos = pos
        step = round(t / 2) * 2
        self.thumb_time = step
        if step in self.thumb_cache:
            self.show_thumb(self.thumb_cache[step])
        else:
            if self.seeker is None:
                outdir = Path(os.environ.get("TEMP", ".")) / "kura_seek"
                self.seeker = ThumbSeeker(self.mpv_mod, outdir, lambda path, tt, img: self.thumb_ready.emit(path, tt, img))
                self.seeker.start()
            self.seeker.request(self.current["path"], step)

    def on_thumb(self, path, t, img):
        if not self.current or path != self.current["path"]:
            return
        self.thumb_cache[t] = img
        if self.thumb_time == t:
            self.show_thumb(img)

    def show_thumb(self, img):
        w = 240 if self.mode != "mini" else 160
        pm = QPixmap.fromImage(img.scaledToWidth(w, Qt.SmoothTransformation))
        rounded = QPixmap(pm.size())
        rounded.fill(Qt.transparent)
        p = QPainter(rounded)
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(rounded.rect()), 8, 8)
        p.setClipPath(path)
        p.drawPixmap(0, 0, pm)
        p.setClipping(False)
        p.setPen(QPen(QColor(255, 255, 255, 90), 1.5))
        p.drawPath(path)
        p.end()
        self.thumb.setPixmap(rounded)
        self.thumb.resize(rounded.size())
        pos = getattr(self, "thumb_pos", None)
        if pos is None:
            return
        x = max(8, min(self.width() - rounded.width() - 8, pos.x() - rounded.width() // 2))
        self.thumb.move(x, pos.y() - rounded.height() - 38)
        self.thumb.show()
        self.thumb.raise_()
        self.hover.raise_()

    def hover_label(self, text, pos, width):
        if not text:
            self.hover.hide()
            return
        self.hover.setText(text)
        self.hover.adjustSize()
        self.hover.move(pos.x(), pos.y() - self.hover.height() - 4)
        self.hover.show()
        self.hover.raise_()

    def resizeEvent(self, e):
        if e is not None:
            super().resizeEvent(e)
        self.video.setGeometry(self.rect())
        mini = self.mode == "mini"
        self.top.setGeometry(0, 0, self.width(), 60 if mini else 76)
        margin = 6 if mini else 24
        dw = self.width() - 2 * margin if mini else min(1160, self.width() - 2 * margin)
        self.bottom.radius = 14 if mini else 22
        bh = self.bottom.sizeHint().height()
        self.bottom.setGeometry((self.width() - dw) // 2, self.height() - bh - margin, dw, bh)
        self.fit_dock()
        self.place_floating()

    def above_controls(self):
        """How far above the window's bottom floating cards sit: over the dock while it shows."""
        return (self.height() - self.bottom.geometry().top() + 14) if self.controls_visible else 40

    def right_edge(self):
        """Where cards on the right end: left of the episode list while it's open."""
        p = getattr(self, "ep_panel", None)
        return (p.x() - 14) if p is not None and p.isVisible() else self.width() - 28

    def place_floating(self):
        p = getattr(self, "ep_panel", None)
        if p is not None and p.isVisible():
            top = self.top.height() - 8
            bottom = self.bottom.geometry().top() - 14 if self.controls_visible else self.height() - 24
            pw = min(384, max(300, self.width() // 3))
            p.setGeometry(self.width() - pw - 22, top, pw, max(160, bottom - top))
        nc = getattr(self, "next_card", None)
        if nc is not None:
            nc.adjustSize()
            nc.move(self.right_edge() - nc.width(), self.height() - self.above_controls() - nc.height())
        self.skip_btn.adjustSize()
        self.skip_btn.move(self.right_edge() - self.skip_btn.width(),
                           self.height() - self.above_controls() - self.skip_btn.height())
        self.osd.adjustSize()
        self.osd.move((self.width() - self.osd.width()) // 2, 90 if self.mode != "mini" else 40)
        lb = getattr(self, "live_banner", None)
        if lb is not None and lb.isVisible():
            lb.move(28, self.height() - self.above_controls() - lb.height())
        lc = getattr(self, "live_card", None)
        if lc is not None and lc.isVisible():
            lc.setGeometry(self.rect())
            lc.lower()
            self.video.lower()
        cb = getattr(self, "clip_bar", None)
        if cb is not None and cb.isVisible():
            cb.adjustSize()
            cb.setFixedWidth(min(self.width() - 40, max(cb.sizeHint().width(), 720)))
            # at the top, under the title: the subtitles at the bottom stay visible while you pick the range
            cb.move((self.width() - cb.width()) // 2, (self.top.height() if self.controls_visible else 0) + 8)
        card = getattr(self, "cap_card", None)
        if card is not None and card.isVisible():
            card.adjustSize()
            card.move(self.right_edge() - card.width(), self.height() - self.above_controls() - card.height())
        sc = getattr(self, "still_card", None)
        if sc is not None and sc.isVisible():
            sc.adjustSize()
            sc.move((self.width() - sc.width()) // 2, (self.height() - sc.height()) // 2)

    # ---- controls showing/hiding
    def show_controls(self):
        self.controls_visible = True
        self.top.show()
        self.bottom.show()
        self.unsetCursor()
        self.video.unsetCursor()
        self.place_floating()
        self.update()
        self.hide_timer.start()

    def hide_controls(self):
        if not self.current:
            return
        over_bar = self.bottom.geometry().contains(self.mapFromGlobal(self.cursor().pos()))
        if self.mpv.pause or over_bar or self.menu is not None or QApplication.activePopupWidget() or \
                self.clip_bar.isVisible() or self.ep_panel.isVisible():
            self.hide_timer.start()
            return
        self.controls_visible = False
        self.top.hide()
        self.bottom.hide()
        self.hover.hide()
        self.thumb.hide()
        self.video.setCursor(Qt.BlankCursor)
        self.place_floating()
        self.update()

    def note_input(self):
        """Somebody's there: the "still watching?" count starts again."""
        self.auto_streak = 0

    def eventFilter(self, obj, e):
        if obj is self.video:
            if e.type() == QEvent.MouseButtonPress and self.ep_panel.isVisible():
                self.close_episodes()   # a click beside the episode list closes it (doesn't pause)
                self.swallow_release = True
                return True
            if e.type() == QEvent.MouseButtonRelease and getattr(self, "swallow_release", False):
                self.swallow_release = False
                return True
            if e.type() == QEvent.MouseButtonPress:
                self.setFocus()
                self.note_input()
                if self.mode == "mini" and e.button() == Qt.LeftButton:   # the mini player moves by dragging
                    self.drag = (e.globalPosition().toPoint(), self.window().pos(), False)
            if e.type() == QEvent.MouseMove and self.drag is not None and e.buttons() & Qt.LeftButton:
                start, origin, moved = self.drag
                delta = e.globalPosition().toPoint() - start
                if moved or delta.manhattanLength() > 6:
                    self.drag = (start, origin, True)
                    self.window().move(origin + delta)
                return True
            if e.type() == QEvent.MouseButtonRelease and self.drag is not None:
                moved, self.drag = self.drag[2], None
                if moved:
                    return True
            if e.type() == QEvent.MouseMove:
                self.show_controls()
            elif e.type() == QEvent.MouseButtonDblClick:
                if self.mode == "mini":
                    self.mini_toggled.emit()   # double-click the mini player: back to the full one
                else:
                    self.toggle_fullscreen()
                return True
            elif e.type() == QEvent.MouseButtonRelease and e.button() == Qt.LeftButton:
                QTimer.singleShot(220, self._single_click)
                self._clicks = getattr(self, "_clicks", 0) + 1
            elif e.type() == QEvent.Wheel:
                self.wheel_volume(e)
                return True
        elif e.type() == QEvent.Wheel and (obj is self.vol or obj is self.top or obj is self.bottom):
            self.wheel_volume(e)
            return True
        return False

    def wheel_volume(self, e):
        """5 % per wheel notch. Counts real distance (120 = one notch): zero-length events (the
        wheel sends those) used to turn the volume down, and smooth-scrolling wheels sent many."""
        dy = e.angleDelta().y()
        if not dy:
            return
        self.note_input()
        if (dy > 0) != (self.wheel_acc > 0) and self.wheel_acc:
            self.wheel_acc = 0   # changed direction: start counting again
        self.wheel_acc += dy
        steps = int(self.wheel_acc / 120)
        if steps:
            self.wheel_acc -= steps * 120
            self.vol.setValue(self.vol.value() + 5 * steps)
        self.show_controls()

    def wheelEvent(self, e):
        self.wheel_volume(e)   # over the control bars too
        e.accept()

    def _single_click(self):
        clicks, self._clicks = getattr(self, "_clicks", 0), 0
        if clicks == 1:
            self.toggle_pause()

    def mouseMoveEvent(self, e):
        self.show_controls()

    # ---- playing
    def open(self, playlist, index=0):
        self.end_live()
        self.playlist = playlist
        self.show()
        self.raise_()
        self.activateWindow()
        if self.video.ctx is None:   # first open: wait for the video surface
            self.video.ready.connect(lambda: self.play_index(index), Qt.SingleShotConnection)
        else:
            self.play_index(index)

    def play_index(self, i):
        if not (0 <= i < len(self.playlist)):
            return
        self.finish_current()
        old_show = self.show_key()[0] if self.current else None
        self.index, self.current = i, dict(self.playlist[i])
        if self.show_key()[0] != old_show:
            self.picture_session = None   # what you picked "just now" was for the previous show
        self.marks, self.chapters_done, self.skipped, self.ended_at = [], False, set(), None
        self.resume_pending = bool(self.settings.get("player_resume", True))
        if getattr(self, "next_card", None) is not None:
            self.next_card.stop()
            self.still_card.hide()
        self.tracks_applied = None
        self.thumb_cache = {}
        self.mpv.pause = False
        self.mpv.play(self.current["path"])
        # mpv pauses itself at the end of a file (keep-open) and ignores "unpause" while it's still there:
        # unpause again once the new file is playing, or the next episode opened paused
        self.force_play = True
        self.title.setText(self.current.get("title") or Path(self.current["path"]).stem)
        kind = self.episode_kind(self.current)
        self.subtitle.setText((self.current.get("subtitle") or "") + (f"  ·  {kind.title()}" if kind and kind != "recap" else ""))
        self.title_changed.emit(f"{self.title.text()} {self.subtitle.text()} · Kura Player".strip())
        self.prev_btn.setEnabled(i > 0)
        self.next_btn.setEnabled(i < len(self.playlist) - 1)
        self.set_cover(self.current)
        self.refresh_episodes()
        self.seekbar.marks, self.seekbar.chapters = [], []
        if self.settings.get("player_aniskip", True) and self.current.get("mid") and self.current.get("episode"):
            threading.Thread(target=self._fetch_aniskip, args=(dict(self.current),), daemon=True).start()
        self.show_controls()

    def _fetch_aniskip(self, item):
        """Opening/ending times from AniSkip (community timestamps, keyed by MyAnimeList id)."""
        try:
            mal = self.mal_lookup(item["mid"]) if self.mal_lookup else None
            if not mal:
                return
            marks = aniskip(mal, item["episode"])   # cached (Tools → Library metadata), else asked now
            self.marks_ready.emit(item["path"], marks or [])
        except Exception:
            pass

    def on_marks(self, path, marks):
        if self.current and path == self.current["path"] and marks:
            self.marks = sorted(marks)
            self.seekbar.marks = self.marks
            self.seekbar.update()

    def chapter_marks(self):
        """Openings/endings named in the file's chapters (used when AniSkip has nothing)."""
        try:
            chapters = self.mpv.chapter_list or []
        except Exception:
            return
        dur = self.mpv.duration or 0
        self.seekbar.chapters = [c.get("time", 0) for c in chapters]
        if self.marks or not chapters:
            return
        out = []
        for n, c in enumerate(chapters):
            name = (c.get("title") or "").lower()
            end = chapters[n + 1]["time"] if n + 1 < len(chapters) else dur
            if re.match(r"^(op|opening|intro)\b", name):
                out.append((c["time"], end, "Opening"))
            elif re.match(r"^(ed|ending|outro|credits)\b", name):
                out.append((c["time"], end, "Ending"))
            elif re.match(r"^(recap|previously|summary)\b", name):
                out.append((c["time"], end, "Recap"))
            elif re.match(r"^(preview|next ep|next time|next episode|yokoku)\b", name):
                out.append((c["time"], end, "Preview"))
        if out:
            self.marks = out
            self.seekbar.marks = out

    def tick(self):
        if self.live and self.live_tick():
            return
        if not self.current:
            return
        try:
            pos, dur, paused = self.mpv.time_pos, self.mpv.duration, self.mpv.pause
        except Exception:
            return
        self.check_tracks()
        self.check_sub_style()
        self.check_picture()
        if dur and not self.chapters_done:
            self.chapters_done = True
            self.chapter_marks()
        if self.resume_pending and dur:
            self.resume_pending = False
            saved = self._resume_state().get(self.current["path"])
            if saved and 30 < saved.get("pos", 0) < dur * 0.92:
                self.mpv.seek(saved["pos"], "absolute")
                self.show_osd(f"Resumed at {fmt_time(saved['pos'])}")
        hold = self.seek_hold
        if hold and (time.time() > hold[1] or (pos is not None and abs(pos - hold[0]) < 1.5
                                               and not self.seek_busy and self.seek_pending is None)):
            self.seek_hold = hold = None   # mpv got there (or gave up): follow mpv again
        if not self.seekbar.dragging and not hold:
            self.seekbar.duration, self.seekbar.position = dur or 0, pos or 0
            self.seekbar.update()
        shown = hold[0] if hold else pos
        self.set_times(shown, dur)
        if getattr(self, "shown_paused", None) != bool(paused):
            self.shown_paused = bool(paused)
            self.play_btn.setIcon(icon(PLAY if paused else PAUSE, "#0d0f14", px=22))
        speed = self.mpv.speed or 1
        self.speed_btn.setText(f"{speed:g}×")
        self.update_skip_button(pos)
        now = time.time()
        if pos is not None and now - self.last_save > 10 and not self.live:
            self.last_save = now
            self.save_resume(pos, dur)
        if now - self.last_emit > 2:
            self.last_emit = now
            self.progress.emit({**self.current, "pos": pos or 0, "duration": dur or 0, "paused": bool(paused)})
        # end of the episode
        if getattr(self, "force_play", False) and pos is not None and not self.mpv.eof_reached:
            self.force_play = False
            if self.mpv.pause:
                self.mpv.pause = False
        trial = getattr(self, "trial_sub", None)
        if trial and pos is not None and self.current and os.path.normcase(self.current["path"]) == trial[0]:
            self.trial_sub = None   # Subtitles → Download → Try: this subtitle, from its first line
            try:
                self.mpv.command("sub-add", trial[1], "select", "Trying (Anime Tosho)")
                self.mpv.command("seek", max(0.0, trial[2] - 2), "absolute+exact")
                self.show_osd("Trying the downloaded subtitles")
            except Exception:
                pass
        if self.mpv.eof_reached and dur and not self.live:
            if self.ended_at is None:
                self.ended_at = now
                self.save_resume(dur, dur)
                self.progress.emit({**self.current, "pos": dur, "duration": dur, "paused": False})
                nxt_i = self.next_index()
                if self.settings.get("player_autonext", True) and nxt_i is not None:
                    limit = int(self.settings.get("player_still_watching", 3) or 0)
                    if limit and self.auto_streak >= limit:   # nobody's touched anything for a while
                        self.still_card.ask(self.current.get("title") or "", self.auto_streak)
                        self.place_floating()
                        self.show_controls()
                    else:
                        nxt = self.playlist[nxt_i]
                        sub = nxt.get("subtitle") or ""
                        if nxt.get("mid") and nxt.get("mid") != self.current.get("mid"):
                            sub = f"Next season  ·  {sub}"   # the playlist runs on into the next season
                        skipped = nxt_i - self.index - 1
                        if skipped:
                            sub += f"  ·  {skipped} filler skipped"
                        self.next_card.start(nxt.get("title") or Path(nxt["path"]).stem, sub)
                        self.place_floating()
                        self.show_controls()

    def current_mark(self, pos):
        return next(((s, e, name) for s, e, name in self.marks if pos is not None and s <= pos < e - 1), None)

    def update_skip_button(self, pos):
        mark = self.current_mark(pos) if not self.live else None   # TV doesn't skip
        if not mark:
            self.skip_btn.hide()
            return
        s, e, name = mark
        auto = self.settings.get("player_autoskip_recap" if name in ("Recap", "Preview") else "player_autoskip", False)
        if auto and (s, name) not in self.skipped:
            self.skipped.add((s, name))
            self.skip_mark()
            self.show_osd(f"Skipped {name.lower()}")
            return
        nxt = name in ("Ending", "Preview") and self.index < len(self.playlist) - 1 and e >= (self.mpv.duration or 0) - 5
        self.skip_btn.setText("Next episode  ▸" if nxt else f"Skip {name}  ▸")
        if not self.skip_btn.isVisible():
            self.skip_btn.show()
            self.skip_btn.raise_()
        self.place_floating()

    def skip_mark(self):
        mark = self.current_mark(self.mpv.time_pos)
        if not mark:
            return
        if mark[2] in ("Ending", "Preview") and mark[1] >= (self.mpv.duration or 0) - 5 and self.index < len(self.playlist) - 1:
            self.step_episode(1)
        else:
            self.mpv.seek(mark[1], "absolute")

    # ---- actions
    def toggle_pause(self):
        self.mpv.pause = not self.mpv.pause
        self.show_controls()

    def skip(self, seconds):
        self.mpv.seek(seconds, "relative")
        self.show_osd(f"{'+' if seconds > 0 else '−'}{abs(seconds)}s")

    def on_seek(self, t, final):
        """From the seek bar: dragging (final=False, quick keyframe seeks) or let go / click
        (final=True, exact). Never waits for mpv, and skips positions it can't keep up with."""
        if self.live and self.live.get("prog"):   # Live TV: back in time yes, past live no
            t = min(t, time.time() - self.live["prog"]["start"])
        self.seek_hold = (t, time.time() + 3)
        self.seek_pending = (t, final)
        self.set_times(t, self.seekbar.duration)
        if not self.seek_busy:
            self._send_seek()

    def _send_seek(self):
        if self.seek_pending is None:
            return
        (t, final), self.seek_pending = self.seek_pending, None
        self.seek_busy = True
        try:
            # (python-mpv's seek() adds "keyframes" precision: the final seek landed seconds off)
            self.mpv.command_async("seek", t, "absolute+exact" if final else "absolute+keyframes",
                                   callback=lambda err, res: self.seek_done.emit())
        except Exception:
            self.seek_busy = False

    def _seek_finished(self):
        self.seek_busy = False
        if self.seek_pending is not None:
            self._send_seek()

    def episode_kind(self, item):
        if item.get("recap") is not None:
            return "recap"
        if FILLER is None or not item.get("mid") or not item.get("episode"):
            return None
        try:
            return FILLER(item["mid"], int(item["episode"]))
        except Exception:
            return None

    def next_index(self):
        """The next episode to play: the next one, or past filler when "Skip filler episodes" is on."""
        i = self.index + 1
        if self.settings.get("player_skip_filler", False):   # filler and recap files (8.5)
            while i < len(self.playlist) - 1 and self.episode_kind(self.playlist[i]) in ("filler", "recap"):
                i += 1
        return i if i < len(self.playlist) else None

    def up_next(self, automatic):
        """Up next's countdown ran out (automatic) or Play now was pressed."""
        if automatic:
            self.auto_streak += 1
        else:
            self.note_input()
        self.step_episode(1)

    def step_episode(self, delta):
        if self.live:
            self.channel_step(delta)
            return
        if delta == 1:
            i = self.next_index()
            if i is None:
                return
            if i > self.index + 1:
                self.show_osd(f"Skipped {i - self.index - 1} filler episode{'s' if i - self.index - 1 != 1 else ''}")
            return self.play_index(i)
        self.play_index(self.index + delta)

    def set_volume(self, v):
        if not self.vol_timer.isActive():   # mpv gets the latest value every 15 ms while it changes
            self.vol_timer.start()
        self.vol_btn.setIcon(icon(MUTE if v == 0 else VOLUME))
        self.settings["player_volume"] = v
        self.show_osd(f"Volume {v}%")

    # ---- picture (upscalers, deband, sharpen): your default, or this show's, or what you picked just now
    def show_key(self):
        if self.current and SHOW_KEY is not None:
            try:
                return SHOW_KEY(self.current)
            except Exception:
                pass
        folder = Path(self.current["path"]).parent if self.current else None
        return (str(folder), folder.name if folder else "")

    def picture_source(self):
        """("session" / "show" / "default", picture setting)"""
        if self.picture_session is not None:
            return "session", self.picture_session
        shows = self.settings.get("picture_shows") or {}
        key = self.show_key()[0]
        if key in shows:
            return "show", shows[key]
        return "default", self.settings.get("picture_default") or {}

    def check_picture(self):
        if PICTURE is None:
            return
        pic = {"mode": "off"} if self.comparing else self.picture_source()[1]
        key = json.dumps(PICTURE.full(pic), sort_keys=True)
        if key != self.picture_applied:
            self.picture_applied = key
            self.apply_picture(pic)

    def apply_picture(self, pic):
        opts = PICTURE.mpv_options(pic)
        try:
            if list(self.mpv["glsl-shaders"] or []) != opts["glsl-shaders"]:
                self.mpv["glsl-shaders"] = opts["glsl-shaders"]
            self.mpv["deband"] = opts["deband"]
            vf = ",".join(x for x in (opts["vf_extra"], self.base_vf) if x)
            if getattr(self, "vf_applied", self.base_vf) != vf:
                self.vf_applied = vf
                self.mpv["vf"] = vf
        except Exception as e:
            self.show_osd(f"Picture: {e}")
        if PICTURE.shader_files(PICTURE.full(pic)) and not opts["glsl-shaders"] and not self.comparing:
            self.show_osd("Upscalers not downloaded yet: ⋯ → Picture")

    def set_picture(self, change, scope=None):
        """Change the picture: just for now (scope None), for this show, or as your default."""
        source, pic = self.picture_source()
        pic = PICTURE.full({**pic, **change})
        if pic["mode"] != "off":
            self.last_mode = pic["mode"]
        if scope == "show":
            key, name = self.show_key()
            shows = dict(self.settings.get("picture_shows") or {})
            shows[key] = {**pic, "name": name}
            self.settings["picture_shows"] = shows
            self.picture_session = None
            self.save_settings(self.settings)
        elif scope == "default":
            self.settings["picture_default"] = pic
            self.picture_session = None
            self.save_settings(self.settings)
        else:   # just for what you're watching (until another show), nothing saved
            self.picture_session = pic if change else None
        self.check_picture()
        self.show_osd(f"Picture: {PICTURE.describe(pic)}")

    def forget_show_picture(self):
        shows = dict(self.settings.get("picture_shows") or {})
        shows.pop(self.show_key()[0], None)
        self.settings["picture_shows"] = shows
        self.picture_session = None
        self.save_settings(self.settings)
        self.check_picture()
        self.show_osd(f"Picture: your default ({PICTURE.describe(self.picture_source()[1])})")

    def picture_menu(self):
        if PICTURE is None:
            return
        source, pic = self.picture_source()
        pic = PICTURE.full(pic)
        key, name = self.show_key()
        where = {"session": "Just now (not saved)", "show": f"This show: {name}", "default": "Your default"}[source]
        items = [(f"{where}  ·  {PICTURE.describe(pic)}", None, None), None]
        if not PICTURE.installed():
            items += [("Download the upscalers (about 3 MB)…", self.download_shaders, None), None]
        for mode, label, desc in PICTURE.MODES:
            items.append((label, lambda m=mode: (self.set_picture({"mode": m}), self.picture_menu()),
                          pic["mode"] == mode))
        items.append(None)
        items.append(("High quality (off = faster)",
                      lambda: (self.set_picture({"quality": "fast" if pic["quality"] != "fast" else "hq"}),
                               self.picture_menu()), pic["quality"] != "fast"))
        items.append(("Deband (smooth gradients)", lambda: (self.set_picture({"deband": not pic["deband"]}),
                                                            self.picture_menu()), pic["deband"]))
        items.append(("Sharpen", lambda: (self.set_picture({"sharpen": not pic["sharpen"]}), self.picture_menu()),
                      pic["sharpen"]))
        items.append(None)
        items.append(("Only for this show", lambda: (self.set_picture({}, "show"), self.picture_menu()),
                      source == "show"))
        items.append(("Make this my default", lambda: (self.set_picture({}, "default"), self.picture_menu()), False))
        if source == "show":
            items.append(("Use my default again", lambda: (self.forget_show_picture(), self.picture_menu()), False))
        items.append(("Hold C to compare with the original", None, None))
        self.open_menu(items, self.more_btn)

    def download_shaders(self):
        if INSTALL_SHADERS is not None:
            self.show_osd("Downloading the upscalers…")
            INSTALL_SHADERS(lambda ok: (self.__setattr__("picture_applied", None), self.check_picture(),
                                        self.show_osd("Upscalers ready" if ok else "Couldn't download the upscalers")))

    def check_sub_style(self):
        """Restyle the subtitles when the file, the subtitle track or the style changed."""
        s = self.settings
        on = bool(s.get("player_sub_style_on") and SUB_OVERRIDES)
        try:
            sid = self.mpv.sid
        except Exception:
            return
        key = (self.current.get("path"), sid, on and json.dumps(s.get("player_sub_style") or {}, sort_keys=True))
        if key == getattr(self, "sub_style_key", None):
            return
        if self.apply_sub_style():
            self.sub_style_key = key   # else: the track isn't loaded yet, try again on the next tick

    def apply_sub_style(self):
        """Kura's subtitle style: the dialogue styles of ASS subtitles (signs keep their look) and mpv's own
        style for plain-text ones. Off = the file's own look."""
        s = self.settings
        style = s.get("player_sub_style") or {}
        on = bool(s.get("player_sub_style_on") and SUB_OVERRIDES)
        try:
            header = self.mpv.sub_ass_extradata if on and self.mpv.sid else None
        except Exception:
            header = None
        if on and header is None and self.mpv.sid:
            try:
                track = next((t for t in self.mpv.track_list or [] if t.get("type") == "sub" and t.get("selected")), {})
            except Exception:
                track = {}
            if track.get("codec") in ("ass", "ssa"):
                return False
        try:
            height = (self.mpv.video_params or {}).get("h") or 1080
        except Exception:
            height = 1080
        try:
            self.mpv["sub-ass-style-overrides"] = SUB_OVERRIDES(style, header, height) if header else []
        except Exception:
            pass
        plain = dict(PLAIN_SUB_DEFAULTS)
        if on:   # mpv sizes plain subtitles on a 720p picture
            st = {"text": "#FFFFFF", "outline": "#000000", "shadow_color": "#000000", "outline_w": 3.0, "shadow": 1.5,
                  "size": 100, "font": "", "bold": False, **style}
            plain.update({"sub-color": st["text"], "sub-outline-color": st["outline"],
                          "sub-outline-size": float(st["outline_w"]) * 720 / 1080,
                          "sub-shadow-offset": float(st["shadow"]) * 720 / 1080,
                          "sub-shadow-color": "#9F" + st["shadow_color"].lstrip("#"),
                          "sub-font-size": 38.0 * int(st["size"]) / 100, "sub-bold": bool(st["bold"])})
            if st["font"]:
                plain["sub-font"] = st["font"]
        for k, v in plain.items():
            try:
                self.mpv[k] = v
            except Exception:
                pass
        return True

    def apply_volume(self):
        self.mpv.volume = self.vol.value()
        if self.mpv.mute:
            self.mpv.mute = False

    def toggle_mute(self):
        self.mpv.mute = not self.mpv.mute
        self.vol_btn.setIcon(icon(MUTE if self.mpv.mute else VOLUME))
        self.show_osd("Muted" if self.mpv.mute else f"Volume {self.vol.value()}%")

    def toggle_fullscreen(self):
        win = self.window()   # Kura's window, or the pop-out
        if win.isFullScreen():
            win.showNormal()
            self.full_btn.setIcon(icon(FULL))
        else:
            win.showFullScreen()
            self.full_btn.setIcon(icon(UNFULL))

    def set_mode(self, mode, on_top=None):
        """embedded (inside Kura), popout, or mini (small, on top, in a corner); the pin (always on top)
        only applies to the pop-out."""
        self.mode = mode
        if on_top is not None:
            self.on_top = on_top
        for w in self.mini_hidden:
            w.setVisible(mode != "mini")
        self.mini_btn.setIcon(icon("open_in_full" if mode == "mini" else "picture_in_picture_alt"))
        self.mini_btn.setToolTip("Back to the full player" if mode == "mini" else "Mini player (keep watching "
                                                                                  "in a corner)")
        self.pop_btn.setVisible(mode != "mini")
        self.pop_btn.setIcon(icon(POPIN if mode == "popout" else POPOUT))
        self.pop_btn.setToolTip("Back into Kura" if mode == "popout" else "Pop out into its own window")
        self.pin_btn.setVisible(mode == "popout")
        size = 40 if mode == "mini" else 54
        self.play_btn.setFixedSize(size, size)
        self.play_btn.setStyleSheet(f"QPushButton#bigplay {{ border-radius: {size // 2 - 1}px; }}")
        if mode == "mini" and self.ep_panel.isVisible():
            self.close_episodes()
        self.ep_btn.setVisible(mode != "mini" and not self.live)
        self.resizeEvent(None)
        self.pin_btn.setIcon(icon(PINNED if self.on_top else PIN, ACCENT if self.on_top else TEXT))
        self.pin_btn.setToolTip("Always on top: on" if self.on_top else "Always on top: off")
        self.full_btn.setIcon(icon(UNFULL if self.window().isFullScreen() else FULL))

    def set_speed(self, speed):
        self.mpv.speed = speed
        self.show_osd(f"Speed {speed:g}×")

    def open_menu(self, items, anchor, **kw):
        if self.menu is not None:
            self.menu.close_menu()
        self.menu = OverlayMenu(self, items, anchor, **kw)
        self.show_controls()

    def speed_menu(self):
        cur = self.mpv.speed or 1
        self.open_menu([(f"{sp:g}×", lambda s=sp: self.set_speed(s), abs(cur - sp) < 0.01) for sp in SPEEDS],
                       self.speed_btn)

    def tracks(self, kind):
        return [t for t in (self.mpv.track_list or []) if t.get("type") == kind]

    @staticmethod
    def track_name(t):
        bits = [t.get("title") or "", (t.get("lang") or "").upper()]
        name = " · ".join(b for b in bits if b) or f"Track {t.get('id')}"
        return name + (" (forced)" if t.get("forced") else "")

    def track_menu(self, kind):
        prop = "aid" if kind == "audio" else "sid"
        items = []
        if kind == "sub":
            items.append(("Off", lambda: self.choose_track("sub", None),
                          not any(t.get("selected") for t in self.tracks("sub"))))
        for t in self.tracks(kind):
            items.append((self.track_name(t), lambda i=t["id"], tr=t: self.choose_track(kind, tr),
                          bool(t.get("selected"))))
        if not items:
            items.append(("No tracks", None, None))
        if kind == "sub" and self.current:
            items.append(("Find subtitles…", lambda: self.find_subs.emit(self.current["path"]), None))
        self.open_menu(items, self.audio_btn if kind == "audio" else self.sub_btn)

    def cycle_track(self, kind):
        tracks = self.tracks(kind)
        if not tracks:
            return
        cur = next((n for n, t in enumerate(tracks) if t.get("selected")), -1)
        nxt = tracks[(cur + 1) % len(tracks)]
        self.choose_track(kind, nxt)
        self.show_osd(f"{'Audio' if kind == 'audio' else 'Subtitles'}: {self.track_name(nxt)}")

    def choose_track(self, kind, track):
        """You picked an audio or subtitle track: use it, and keep using it for this show (every season)."""
        prop = "aid" if kind == "audio" else "sid"
        setattr(self.mpv, prop, track["id"] if track else "no")
        prefs = dict(self.settings.get("track_prefs") or {})
        key = self.show_key()[0]
        mine = dict(prefs.get(key) or {})
        mine[kind] = ({k: track.get(k) for k in ("lang", "title", "codec", "ff-index")} if track else "off")
        prefs[key] = mine
        self.settings["track_prefs"] = prefs

    def check_tracks(self):
        """Once per file, as soon as its tracks are known: the audio / subtitles you chose for this show."""
        if self.tracks_applied == (self.current or {}).get("path"):
            return
        try:
            tracks = self.mpv.track_list or []
        except Exception:
            return
        if not tracks:
            return
        self.tracks_applied = self.current["path"]
        mine = (self.settings.get("track_prefs") or {}).get(self.show_key()[0]) or {}
        for kind, prop in (("audio", "aid"), ("sub", "sid")):
            want = mine.get(kind)
            if want == "off":
                setattr(self.mpv, prop, "no")
                continue
            if not want:
                continue
            best, score = None, 0
            for t in (t for t in tracks if t.get("type") == kind):
                sc = ((t.get("lang") == want.get("lang")) * 4 + (t.get("title") == want.get("title")) * 3 +
                      (t.get("codec") == want.get("codec")) * 2 + (t.get("ff-index") == want.get("ff-index")))
                if sc > score:
                    best, score = t, sc
            if best is not None and score >= 3 and not best.get("selected"):
                setattr(self.mpv, prop, best["id"])

    def more_menu(self):
        st = self.settings
        self.open_menu([
            ("Picture…", self.picture_menu, None),
            ("Go to time…", self.go_to_time, None),
            ("Subtitle delay +0.1 s", lambda: self.sub_delay(0.1), None),
            ("Subtitle delay −0.1 s", lambda: self.sub_delay(-0.1), None),
            ("Auto-skip openings and endings",
             lambda: st.__setitem__("player_autoskip", not st.get("player_autoskip")), bool(st.get("player_autoskip"))),
            ("Auto-skip recaps and previews",
             lambda: st.__setitem__("player_autoskip_recap", not st.get("player_autoskip_recap")),
             bool(st.get("player_autoskip_recap"))),
            ("Skip filler episodes",
             lambda: st.__setitem__("player_skip_filler", not st.get("player_skip_filler")),
             bool(st.get("player_skip_filler"))),
            ("Play next episode automatically",
             lambda: st.__setitem__("player_autonext", not st.get("player_autonext", True)),
             bool(st.get("player_autonext", True))),
            None,
            ("Screenshot (F12)", lambda: self.screenshot(False), None),
            ("Screenshot without subtitles (Shift+F12)", lambda: self.screenshot(True), None),
            ("Make a clip… (X)", self.open_clip_bar, None),
            (f"Clip the last {int(capture.opt(st, 'replay_secs'))} seconds (Ctrl+F12)", self.clip_last, None),
            ("Contact sheet of this episode", self.contact_sheet, None),
            None,
            ("Show file in folder", self.reveal, None),
        ], self.more_btn)

    def go_to_time(self):
        self.open_menu([], self.more_btn, input_text="Go to time (12:30 or 750), then Enter",
                       on_input=self.go_to_time_text)

    def go_to_time_text(self, text):
        if text.strip():
            parts = [p for p in re.split(r"[:.]", text.strip()) if p.isdigit()]
            if parts:
                secs = 0
                for p in parts:
                    secs = secs * 60 + int(p)
                self.mpv.seek(secs, "absolute")

    def sub_delay(self, d):
        self.mpv.sub_delay = round((self.mpv.sub_delay or 0) + d, 2)
        self.show_osd(f"Subtitle delay {self.mpv.sub_delay:+.1f} s")

    def reveal(self):
        if self.current and os.name == "nt":
            import subprocess
            subprocess.Popen(["explorer", "/select,", self.current["path"]])

    def show_osd(self, text):
        self.osd.setText(text)
        self.osd.adjustSize()
        self.place_floating()
        self.osd.show()
        self.osd.raise_()
        self.osd_timer.start(1400)

    def mousePressEvent(self, e):
        self.setFocus()   # clicking the controls' background gives the keys back to the player
        super().mousePressEvent(e)

    def showEvent(self, e):
        super().showEvent(e)
        QTimer.singleShot(0, self.setFocus)

    def focusNextPrevChild(self, forward):
        return True   # Tab stays in the player (Kura's page behind it would take the keys)

    def keyPressEvent(self, e):
        self.note_input()
        if self.menu is not None and e.key() == Qt.Key_Escape:
            self.menu.close_menu()
            return
        if self.ep_panel.isVisible() and e.key() == Qt.Key_Escape:
            self.close_episodes()
            return
        if e.key() == Qt.Key_E and not e.modifiers() and not self.live:
            self.toggle_episodes()
            return
        k, shift = e.key(), bool(e.modifiers() & Qt.ShiftModifier)
        if k == Qt.Key_F12:   # screenshots and clips
            if e.modifiers() & Qt.ControlModifier:
                self.clip_last()
            else:
                self.screenshot(clean=shift)
            return
        if self.live and self.live_key(e):
            return
        if k == Qt.Key_X and not e.modifiers():
            self.close_clip_bar() if self.clip_bar.isVisible() else self.open_clip_bar()
            return
        if self.clip_bar.isVisible():
            bar = self.clip_bar
            keys = {Qt.Key_I: bar.set_start, Qt.Key_O: bar.set_end, Qt.Key_Comma: lambda: self.frame_step(-1),
                    Qt.Key_Period: lambda: self.frame_step(1), Qt.Key_Return: bar.save, Qt.Key_Enter: bar.save,
                    Qt.Key_Escape: bar.close_bar,
                    Qt.Key_L: lambda: (bar.loop_btn.toggle(), bar.toggle_loop())}
            if k in keys:
                keys[k]()
                return
        if e.modifiers() & Qt.ControlModifier and PICTURE is not None:
            modes = {Qt.Key_1: "a4k_a", Qt.Key_2: "a4k_b", Qt.Key_3: "a4k_c", Qt.Key_4: "a4k_aa",
                     Qt.Key_5: "a4k_bb", Qt.Key_6: "a4k_ca", Qt.Key_7: "fsrcnnx", Qt.Key_8: "artcnn"}
            pic = PICTURE.full(self.picture_source()[1])
            if k in modes:
                self.set_picture({"mode": modes[k]})
            elif k == Qt.Key_G:   # everything off / back on
                self.set_picture({"mode": "off" if pic["mode"] != "off" else self.last_mode})
            elif k == Qt.Key_D:
                self.set_picture({"deband": not pic["deband"]})
            elif k == Qt.Key_H:
                self.set_picture({"sharpen": not pic["sharpen"]})
            elif k == Qt.Key_Q:
                self.set_picture({"quality": "fast" if pic["quality"] != "fast" else "hq"})
            else:
                return super().keyPressEvent(e)
            return
        if k == Qt.Key_C and PICTURE is not None:   # hold C: the original picture
            if not e.isAutoRepeat():
                self.comparing = True
                self.check_picture()
                self.show_osd("Original picture (release C)")
            return
        short, long = int(self.settings.get("player_skip_short", 10)), int(self.settings.get("player_skip_long", 85))
        actions = {
            Qt.Key_Space: self.toggle_pause, Qt.Key_K: self.toggle_pause,
            Qt.Key_Right: lambda: self.skip(long if shift else short), Qt.Key_L: lambda: self.skip(short),
            Qt.Key_Left: lambda: self.skip(-(long if shift else short)), Qt.Key_J: lambda: self.skip(-short),
            Qt.Key_Up: lambda: self.vol.setValue(self.vol.value() + 5),
            Qt.Key_Down: lambda: self.vol.setValue(self.vol.value() - 5),
            Qt.Key_M: self.toggle_mute, Qt.Key_F: self.toggle_fullscreen,
            Qt.Key_N: lambda: self.step_episode(1), Qt.Key_P: lambda: self.step_episode(-1),
            Qt.Key_A: lambda: self.cycle_track("audio"), Qt.Key_S: lambda: self.cycle_track("sub"),
            Qt.Key_BracketLeft: lambda: self.set_speed(max(0.25, round((self.mpv.speed or 1) - 0.25, 2))),
            Qt.Key_BracketRight: lambda: self.set_speed(min(3.0, round((self.mpv.speed or 1) + 0.25, 2))),
            Qt.Key_O: self.skip_mark,
            Qt.Key_Escape: lambda: self.window().isFullScreen() and self.toggle_fullscreen(),
        }
        fn = actions.get(k)
        if fn:
            fn()
            self.show_controls()
        else:
            super().keyPressEvent(e)

    def keyReleaseEvent(self, e):
        if e.key() == Qt.Key_C and not e.isAutoRepeat() and self.comparing:
            self.comparing = False
            self.check_picture()
            self.show_osd(f"Picture: {PICTURE.describe(self.picture_source()[1])}")
            return
        super().keyReleaseEvent(e)

    # ---- Live TV
    def open_live(self, cid):
        """Tune in to a channel: whatever is on, from the second it's at."""
        self.end_live()
        self.live = {"cid": None, "prog": None, "delay": 0.0, "last": None, "typed": ""}
        self.show()
        self.raise_()
        self.activateWindow()
        if self.video.ctx is None:
            self.video.ready.connect(lambda: self.tune(cid), Qt.SingleShotConnection)
        else:
            self.tune(cid)

    def end_live(self):
        if self.live is not None:
            self.live = None
            self.live_card.hide()
            self.live_banner.hide()
            self.live_badge.hide()
            self.ep_btn.setVisible(self.mode != "mini")
            self.prev_btn.setToolTip("Previous episode (P)")
            self.next_btn.setToolTip("Next episode (N)")

    def tune(self, cid, ts=None):
        if LIVE is None or self.live is None:
            return
        prog, off = LIVE.now(cid, ts)
        prev = self.live.get("cid")
        if prev is not None and prev != cid:
            self.live["last"] = prev
        self.live.update(cid=cid, delay=0.0 if ts is None else max(0.0, time.time() - ts))
        if prog is None:
            ch = LIVE.channel(cid) or {}
            self.live["prog"] = None
            self.mpv.command("stop")
            self.live_card.show_card("Nothing on right now", f"{ch.get('number', '')}  {ch.get('name', '')}")
            return
        self.play_program(prog, off)
        ch = LIVE.channel(cid) or {}
        self.live_banner.show_for(ch, prog, (LIVE.upcoming(cid) or [None])[0])
        self.place_floating()

    def play_program(self, prog, off):
        """One programme of the channel, from `off` seconds into it."""
        cid = self.live["cid"]
        self.finish_current()
        item = LIVE.item_for(cid, prog)
        self.live["prog"] = prog
        self.playlist, self.index, self.current = [item], 0, dict(item)
        self.marks, self.chapters_done, self.skipped, self.ended_at = [], False, set(), None
        self.resume_pending, self.tracks_applied, self.thumb_cache = False, None, {}
        self.next_card.stop()
        self.still_card.hide()
        self.seekbar.marks, self.seekbar.chapters = [], []
        ch = LIVE.channel(cid) or {}
        self.ep_btn.hide()
        if self.ep_panel.isVisible():
            self.close_episodes()
        self.set_cover(None)
        self.title.setText(item.get("title") or "")
        self.subtitle.setText(f"{ch.get('number', '')}  {ch.get('name', '')}" +
                              (f"  ·  {item['subtitle']}" if item.get("subtitle") else ""))
        self.title_changed.emit(f"{self.title.text()} · {ch.get('name', '')} · Kura Live TV")
        self.prev_btn.setEnabled(True)
        self.next_btn.setEnabled(True)
        self.prev_btn.setToolTip("Channel down (Page Down)")
        self.next_btn.setToolTip("Channel up (Page Up)")
        kind = prog.get("kind")
        try:
            if list(self.mpv["audio-files"] or []):
                self.mpv["audio-files"] = []   # a trailer's sound mustn't stay for the next file
        except Exception:
            pass
        if kind in ("episode", "opening", "commercial") and item.get("path"):
            self.live_card.hide()
            self.mpv.pause = False
            opts = {"start": f"{max(0.0, off):.2f}"}
            if kind in ("opening", "commercial"):
                opts["end"] = f"{prog['dur']:.2f}"
            self.mpv.loadfile(item["path"], **opts)
            self.force_play = True
        else:
            try:
                self.mpv.command("stop")
            except Exception:
                pass
            nxt = LIVE.after(cid, prog)
            title = prog.get("title") or ""
            small = f"{ch.get('number', '')}  {ch.get('name', '')}"
            if kind == "card" and nxt and nxt.get("kind") == "episode":
                title, small = nxt.get("title") or title, f"Up next on {ch.get('name', '')}"
            self.live_card.show_card(title, small, LIVE.cover(prog if kind != "card" or not nxt else nxt),
                                     ch.get("colour"), prog["end"])
            if kind == "trailer":   # a "commercial": the trailer plays when yt-dlp can find it
                def fetch(p=prog, c=cid, o=off):
                    url = LIVE.trailer_url(p)
                    if url:
                        QTimer.singleShot(0, self, lambda: self._play_trailer(c, p, url, o))
                threading.Thread(target=fetch, daemon=True).start()
        self.place_floating()
        self.show_controls()

    def _play_trailer(self, cid, prog, url, off):
        if not self.live or self.live.get("prog") is not prog:
            return
        left = prog["end"] - (time.time() - self.live["delay"])
        if left < 8:
            return
        self.live_card.hide()
        self.mpv.pause = False
        try:   # YouTube sends picture and sound separately (a property: stream URLs can contain commas)
            self.mpv["audio-files"] = [url["audio"]] if url.get("audio") else []
        except Exception:
            pass
        self.mpv.loadfile(url["video"], start=f"{max(0.0, time.time() - self.live['delay'] - prog['start']):.2f}",
                          end=f"{prog['dur']:.2f}")
        self.current = dict(self.current, path=url["video"])
        self.force_play = True

    def live_tick(self):
        """Live TV every 250 ms. True = nothing else to do this tick (a card, or the next programme starting)."""
        prog = self.live.get("prog")
        if prog is None or LIVE is None:
            return True
        now = time.time()
        if self.live_banner.isVisible():
            self.live_banner.update_bar(prog)
        try:
            pos, eof, paused = self.mpv.time_pos, self.mpv.eof_reached, self.mpv.pause
        except Exception:
            pos = eof = paused = None
        showing_video = not self.live_card.isVisible()
        if showing_video and pos is not None and not eof:
            self.live["delay"] = max(0.0, (now - prog["start"]) - pos)
        delay = self.live["delay"]
        if not showing_video and paused is False and prog.get("kind") in ("card", "trailer"):
            pass
        ended = (showing_video and eof) or (not showing_video and now - delay >= prog["end"]) or \
                (now - delay >= prog["end"] + 5)
        if ended:
            self.live_advance()
            return True
        if delay > 3:
            self.live_badge.setText(f"−{int(delay) // 60}:{int(delay) % 60:02d}  ·  Jump to live")
            self.live_badge.setStyleSheet("QPushButton { background: rgba(255,255,255,0.14); color: white;"
                                          " border-radius: 10px; padding: 4px 10px; font-weight: 600; }")
        else:
            self.live_badge.setText("●  LIVE")
            self.live_badge.setStyleSheet("QPushButton { background: #e5484d; color: white; border-radius: 10px;"
                                          " padding: 4px 10px; font-weight: 700; }")
        self.live_badge.show()
        return not showing_video

    def live_advance(self):
        """The programme ended: the next one, as far behind live as you were."""
        prog, cid = self.live["prog"], self.live["cid"]
        t = max(time.time() - self.live["delay"], prog["end"] + 0.05)
        nxt, off = LIVE.now(cid, t)
        if nxt is None:
            self.tune(cid)
            return
        self.play_program(nxt, off)
        if nxt.get("kind") == "episode":
            ch = LIVE.channel(cid) or {}
            self.live_banner.show_for(ch, nxt, LIVE.after(cid, nxt), 4500)
            self.place_floating()

    def jump_live(self):
        if self.live:
            self.tune(self.live["cid"])
            self.show_osd("Live")

    def channel_step(self, step):
        if not self.live or LIVE is None:
            return
        chans = LIVE.channels()
        ids = [c["id"] for c in chans]
        if not ids:
            return
        i = ids.index(self.live["cid"]) if self.live["cid"] in ids else -1
        self.tune(ids[(i + step) % len(ids)])

    def tune_number(self, n):
        ch = next((c for c in LIVE.channels() if c.get("number") == n), None) if LIVE else None
        if ch:
            self.tune(ch["id"])
        else:
            self.show_osd(f"No channel {n}")

    def typed_done(self):
        if self.live and self.live.get("typed"):
            n, self.live["typed"] = int(self.live["typed"]), ""
            self.tune_number(n)

    def live_key(self, e):
        k = e.key()
        if k in (Qt.Key_PageUp, Qt.Key_PageDown):
            self.channel_step(1 if k == Qt.Key_PageUp else -1)
        elif Qt.Key_0 <= k <= Qt.Key_9 and not e.modifiers() & Qt.ControlModifier:
            self.live["typed"] = (self.live.get("typed") or "") + chr(k)
            self.show_osd(f"Channel {self.live['typed']}_")
            self.typed_timer.start()
        elif k == Qt.Key_Backspace and self.live.get("last"):
            self.tune(self.live["last"])
        elif k == Qt.Key_End:
            self.jump_live()
        elif k in (Qt.Key_N, Qt.Key_P):
            self.channel_step(1 if k == Qt.Key_N else -1)
        elif k == Qt.Key_I and not self.clip_bar.isVisible():   # what's on (the banner again)
            ch = LIVE.channel(self.live["cid"]) or {}
            if self.live.get("prog"):
                self.live_banner.show_for(ch, self.live["prog"], LIVE.after(self.live["cid"], self.live["prog"]))
                self.place_floating()
        else:
            return False
        self.show_controls()
        return True

    # ---- screenshots and clips
    def capture_info(self):
        info = dict(self.current or {})
        m = re.search(r"Season (\d+)", info.get("subtitle") or "")
        if m and not info.get("season"):
            info["season"] = int(m.group(1))
        if CAPTURE_INFO is not None and self.current:
            try:
                info.update(CAPTURE_INFO(self.current) or {})
            except Exception:
                pass
        info.setdefault("show", info.get("title") or Path(info.get("path") or "Kura").stem)
        return info

    def enhanced(self):
        """Upscalers / deband on: "as you see it" has to come from the window, not the decoded frame."""
        try:
            return bool(list(self.mpv["glsl-shaders"] or [])) or bool(self.mpv["deband"])
        except Exception:
            return False

    def screenshot(self, clean=False):
        if not self.current:
            return
        st = self.settings
        mode = "video" if clean else ("window" if capture.opt(st, "shot_enhanced") and self.enhanced() else "subtitles")
        try:
            img = self.mpv.screenshot_raw(mode)
        except Exception as e:
            self.show_osd(f"Couldn't take the screenshot ({e})")
            return
        if mode == "window":
            try:
                img = capture.fix_aspect(img, float(self.mpv.width) / float(self.mpv.height))
            except (TypeError, ZeroDivisionError):
                pass
        info = self.capture_info()
        at = float(self.mpv.time_pos or 0)
        ext = capture.SHOT_FORMATS.get(capture.opt(st, "shot_format"), capture.SHOT_FORMATS["png"])[1]
        dest = capture.out_path(st, info, ext, at)
        preview = QImage(img.tobytes("raw", "RGB"), img.width, img.height, img.width * 3, QImage.Format_RGB888).copy()
        self.cap_card.done("Screenshot" + (" (clean)" if clean else ""), str(dest), preview)
        self.show_controls()

        def save():
            try:
                capture.save_shot(img, dest, st)
                capture.remember(st, dest, info, at, "screenshot")
                if CAPTURE_SAVED is not None:
                    CAPTURE_SAVED(str(dest), "screenshot")
            except Exception as e:
                QTimer.singleShot(0, self, lambda: self.cap_card.done("Screenshot failed", None, None, str(e)))
        threading.Thread(target=save, daemon=True).start()
        if capture.opt(st, "shot_clipboard"):
            QApplication.clipboard().setImage(preview)

    def open_clip_bar(self):
        if not self.current or self.mode == "mini":
            return
        self.clip_bar.open()
        self.place_floating()

    def close_clip_bar(self):
        self.clip_bar.close_bar()

    def frame_step(self, step):
        try:
            self.mpv.command("frame-step" if step > 0 else "frame-back-step")
        except Exception:
            pass

    def set_loop(self, rng):
        try:
            if rng:
                self.mpv["ab-loop-a"], self.mpv["ab-loop-b"] = rng
                self.mpv.command("seek", rng[0], "absolute+exact")
                self.mpv.pause = False
            else:
                self.mpv["ab-loop-a"] = self.mpv["ab-loop-b"] = "no"
        except Exception:
            pass

    def clip_last(self):
        if not self.current:
            return
        pos = float(self.mpv.time_pos or 0)
        secs = float(capture.opt(self.settings, "replay_secs"))
        if pos < 1:
            return
        self.start_clip(max(0.0, pos - secs), pos)

    def shown_tracks(self):
        """(subtitle track as capture wants it, audio stream index) for what's playing now."""
        sub, audio = None, None
        try:
            for t in self.mpv.track_list or []:
                if not t.get("selected"):
                    continue
                if t.get("type") == "sub":
                    if t.get("external"):
                        sub = {"external": t.get("external-filename"), "codec": t.get("codec")}
                    elif t.get("ff-index") is not None:
                        sub = {"index": t["ff-index"], "codec": {"pgs": "hdmv_pgs_subtitle"}.get(t.get("codec"),
                                                                                                t.get("codec"))}
                elif t.get("type") == "audio" and t.get("ff-index") is not None:
                    audio = t["ff-index"]
        except Exception:
            pass
        return sub, audio

    def start_clip(self, start, end):
        if not capture.subs.ffmpeg_installed():
            self.show_osd("Clips need FFmpeg: Settings → Captures → Install FFmpeg")
            return
        st = self.settings
        info = self.capture_info()
        fmt = capture.opt(st, "clip_format")
        ext = capture.CLIP_FORMATS.get(fmt, capture.CLIP_FORMATS["h264"])[1]
        dest = capture.out_path(st, info, ext, start, end)
        sub, audio = self.shown_tracks()
        style = (st.get("player_sub_style") or {}) if (st.get("player_sub_style_on") and SUB_OVERRIDES) else None
        src = self.current["path"]
        frozen = dict(st)   # this clip keeps the options it was started with
        enhance, src_size, notes = None, None, []
        if PICTURE is not None and capture.opt(st, "clip_enhanced") and fmt in capture.VIDEO_FORMATS and \
                self.enhanced() and not self.comparing:
            o = PICTURE.mpv_options(self.picture_source()[1])   # what the player shows for this show right now
            enhance = {"shaders": o["glsl-shaders"], "deband": o["deband"], "cas": bool(o["vf_extra"])}
            try:
                src_size = (int(self.mpv.width), int(self.mpv.height))
            except (TypeError, ValueError):
                src_size = None
        self.clip_notes = notes

        def work(progress, cancelled):
            path = capture.render_clip(src, start, end, dest, frozen, sub_track=sub, audio_index=audio, style=style,
                                       progress=progress, cancelled=cancelled, enhance=enhance, src_size=src_size,
                                       notes=notes)
            capture.remember(frozen, path, info, start, "clip", end)
            if CAPTURE_SAVED is not None:
                CAPTURE_SAVED(str(path), "clip")
            return path
        try:
            preview = self.mpv.screenshot_raw("subtitles")
            preview = preview.resize((224, round(224 * preview.height / preview.width)))
            qimg = QImage(preview.tobytes("raw", "RGB"), preview.width, preview.height, preview.width * 3,
                          QImage.Format_RGB888).copy()
        except Exception:
            qimg = None
        title = f"Clip · {fmt_time(start)}–{fmt_time(end)}" + (" · enhanced" if enhance else "")
        self.clip_jobs.append((work, title, dest.name, qimg, ("Clip saved", notes)))
        self.next_clip()

    def contact_sheet(self):
        if not self.current:
            return
        if not capture.subs.ffmpeg_installed():
            self.show_osd("Contact sheets need FFmpeg: Settings → Captures → Install FFmpeg")
            return
        info = self.capture_info()
        st = dict(self.settings)
        src = self.current["path"]
        dest = capture.out_path(st, info, ".jpg", 0)
        dest = dest.with_name(dest.stem.rsplit(" - ", 1)[0] + " - contact sheet.jpg")
        title = f"{info.get('show')}" + (f"  ·  {self.current.get('subtitle')}" if self.current.get("subtitle") else "")

        def work(progress, cancelled):
            path = capture.contact_sheet(src, dest, title, Path(src).name, progress=progress, cancelled=cancelled)
            capture.remember(st, path, info, 0, "sheet")
            if CAPTURE_SAVED is not None:
                CAPTURE_SAVED(str(path), "sheet")
            return path
        self.clip_jobs.append((work, "Contact sheet", dest.name, None, ("Contact sheet saved", [])))
        self.next_clip()

    def next_clip(self):
        if self.clip_running is not None or not self.clip_jobs:
            if self.clip_running is not None and self.clip_jobs:
                self.cap_card.name.setText(f"{self.cap_card.name.text().split('  ·  ')[0]}  ·  {len(self.clip_jobs)} waiting")
            return
        work, title, name, qimg, done_title = self.clip_jobs.pop(0)
        job = self.clip_running = ClipJob(work)
        job.progressed.connect(self.cap_card.progress)
        job.finished_job.connect(lambda path, err, j=job, t=done_title, q=qimg: self.clip_done(j, path, err, t, q))
        self.cap_card.working(title, name + (f"  ·  {len(self.clip_jobs)} waiting" if self.clip_jobs else ""), job, qimg)
        job.start()

    def clip_done(self, job, path, err, title, qimg):
        job.wait()
        self.clip_running = None
        if err == "cancelled":
            self.cap_card.done("Cancelled", None, None, "Nothing was saved")
        elif err:
            self.cap_card.done("Couldn't make it", None, None, err[:120])
        else:
            done_title, notes = title
            img = QImage(path) if path.lower().endswith(".jpg") else qimg
            self.cap_card.done(done_title, path, img)
            if notes:   # saved, but not quite as asked (no picture enhancement)
                self.cap_card.name.setText(notes[0][:90])
                self.cap_card.name.setToolTip(notes[0])
        self.next_clip()

    def try_subtitle(self, video, sub_file, at):
        """Once `video` plays: add `sub_file` as the shown track and go to `at` seconds (its first line)."""
        self.trial_sub = (os.path.normcase(os.fspath(video)), os.fspath(sub_file), float(at or 0))

    # ---- controller
    def pad(self, name):
        """A controller button (from Kura's PadNavigator). Returns True when it was used."""
        self.note_input()
        if self.menu is not None:
            if name in ("DPAD_UP", "DPAD_DOWN"):
                self.menu.pad_move(-1 if name == "DPAD_UP" else 1)
            elif name == "A":
                self.menu.pad_activate()
            elif name in ("B", "START"):
                self.menu.close_menu()
            return True
        if self.ep_panel.isVisible():
            if name in ("DPAD_UP", "DPAD_DOWN"):
                self.ep_panel.pad_move(-1 if name == "DPAD_UP" else 1)
                return True
            if name == "A":
                self.ep_panel.pad_activate()
                return True
            if name == "B":
                self.close_episodes()
                return True
        if self.still_card.isVisible():
            if name == "A":
                self.still_card.keep_going.emit()
                self.still_card.hide()
            elif name == "B":
                self.still_card.hide()
                self.still_card.done.emit()
            return True
        if self.next_card.isVisible() and name in ("A", "B"):
            (self.next_card.finish if name == "A" else self.next_card.cancel)()
            return True
        short = int(self.settings.get("player_skip_short", 10))
        long = int(self.settings.get("player_skip_long", 85))
        acts = {
            "A": self.toggle_pause, "Y": self.toggle_fullscreen, "X": lambda: self.track_menu("sub"),
            "LB": lambda: self.step_episode(-1), "RB": lambda: self.step_episode(1),
            "LT": lambda: self.screenshot(False), "RT": self.clip_last,
            "DPAD_LEFT": lambda: self.skip(-short), "DPAD_RIGHT": lambda: self.skip(short),
            "DPAD_UP": lambda: self.vol.setValue(self.vol.value() + 5),
            "DPAD_DOWN": lambda: self.vol.setValue(self.vol.value() - 5),
            "START": self.more_menu, "BACK": self.picture_menu, "RS": lambda: self.skip(long),
            "LS": self.skip_mark,
        }
        if self.live and name in ("LB", "RB"):
            self.channel_step(-1 if name == "LB" else 1)
            self.show_controls()
            return True
        if name == "B":
            if self.window().isFullScreen():
                self.toggle_fullscreen()
            else:
                self.close_requested.emit()
            return True
        fn = acts.get(name)
        if fn:
            fn()
            self.show_controls()
            return True
        return False

    # ---- resume positions
    def _resume_state(self):
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save_resume(self, pos, dur):
        if not self.current or pos is None or self.current.get("live"):
            return
        state = self._resume_state()
        path = self.current["path"]
        if dur and pos >= dur * 0.92:
            state.pop(path, None)   # finished: start from the beginning next time
        else:
            state[path] = {"pos": round(pos, 1), "dur": round(dur or 0, 1), "t": int(time.time())}
        if len(state) > 500:   # keep the most recent ones
            state = dict(sorted(state.items(), key=lambda kv: -kv[1].get("t", 0))[:500])
        try:
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(state), encoding="utf-8")
            os.replace(tmp, self.state_file)
        except OSError:
            pass

    def finish_current(self):
        if not self.current:
            return
        try:
            pos, dur = self.mpv.time_pos, self.mpv.duration
        except Exception:
            pos = dur = None
        if pos is not None and self.ended_at is None:
            self.save_resume(pos, dur)
        self.item_finished.emit({**self.current, "pos": pos or 0, "duration": dur or 0})

    def stop(self):
        """Stop for good: remember where you were, save settings, shut mpv down."""
        self.end_live()
        for timer in (self.tick_timer, self.hide_timer, self.osd_timer):
            timer.stop()
        if self.seeker is not None:
            self.seeker.stop()
        self.next_card.stop()
        job, waiting = self.clip_running, [w[0] for w in self.clip_jobs]
        self.clip_jobs = []
        if job is not None:   # clips being made (and waiting) finish in the background: saved, no card
            for sig in (job.progressed, job.finished_job):
                try:
                    sig.disconnect()
                except (RuntimeError, TypeError):
                    pass
            job.finished_job.connect(background_done)
            BACKGROUND.append(job)
            job.finished.connect(lambda j=job: (BACKGROUND.remove(j) if j in BACKGROUND else None,
                                                run_background(waiting)))
            self.clip_running = None
        else:
            run_background(waiting)
        self.finish_current()
        self.current = None
        self.save_settings(self.settings)
        self.video.shutdown()


class PopoutWindow(QWidget):
    """The player in its own window. Closing it stops playback; popping back into Kura
    first takes the view out (take_view) so nothing stops."""
    closed = Signal()

    def __init__(self, view, on_top=False, window_icon=None, mini=False):
        super().__init__(None)
        self.view, self.mini = view, mini
        self.setWindowTitle("Kura Player")
        if window_icon:
            self.setWindowIcon(window_icon)
        self.setAttribute(Qt.WA_StyledBackground)
        self.setStyleSheet("PopoutWindow { background: black; }")   # only the window, not the labels in it
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(view)
        view.title_changed.connect(self.setWindowTitle)
        if mini:   # small, frameless, on top, in the bottom-right corner of the screen
            self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
            from PySide6.QtWidgets import QSizeGrip
            self.grip = QSizeGrip(self)
            self.grip.setFixedSize(16, 16)
            self.grip.setStyleSheet("background: transparent;")
            self.resize(480, 270)
        else:
            self.resize(1280, 760)
            self.set_on_top(on_top)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.mini:
            self.grip.move(self.width() - 16, self.height() - 16)
            self.grip.raise_()

    def place_in_corner(self, screen):
        r = screen.availableGeometry()
        self.move(r.right() - self.width() - 24, r.bottom() - self.height() - 24)

    def set_on_top(self, on):
        was_visible = self.isVisible()
        self.setWindowFlag(Qt.WindowStaysOnTopHint, on)   # changing flags hides the window
        if was_visible:
            self.show()

    def take_view(self):
        view, self.view = self.view, None
        if view is not None:
            view.title_changed.disconnect(self.setWindowTitle)
            self.layout().removeWidget(view)
        return view

    def closeEvent(self, e):
        if self.view is not None:
            self.closed.emit()
        super().closeEvent(e)
