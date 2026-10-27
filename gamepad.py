"""
Controllers for Kura (no Qt in here): Windows' XInput (Xbox controllers, and PlayStation pads through Steam
Input or DS4Windows). `Gamepads.poll()` reads every connected pad and returns what happened since the last poll:
button presses ("A", "DPAD_UP", ...), direction repeats while a d-pad / stick is held, trigger presses and the
right stick's position. Disconnected slots are only re-checked every 2 s (asking an empty slot is slow).
"""

import ctypes
import os
import time

BUTTONS = {
    "DPAD_UP": 0x0001, "DPAD_DOWN": 0x0002, "DPAD_LEFT": 0x0004, "DPAD_RIGHT": 0x0008,
    "START": 0x0010, "BACK": 0x0020, "LS": 0x0040, "RS": 0x0080,
    "LB": 0x0100, "RB": 0x0200, "A": 0x1000, "B": 0x2000, "X": 0x4000, "Y": 0x8000,
}
DIRECTIONS = ("DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT")
STICK_DEAD = 14000          # left stick acts as a d-pad past this
RIGHT_DEAD = 7000           # right stick (scrolling) dead zone
TRIGGER_ON = 120
REPEAT_DELAY, REPEAT_EVERY = 0.38, 0.09


class _Pad(ctypes.Structure):
    _fields_ = [("wButtons", ctypes.c_ushort), ("bLeftTrigger", ctypes.c_ubyte), ("bRightTrigger", ctypes.c_ubyte),
                ("sThumbLX", ctypes.c_short), ("sThumbLY", ctypes.c_short),
                ("sThumbRX", ctypes.c_short), ("sThumbRY", ctypes.c_short)]


class _State(ctypes.Structure):
    _fields_ = [("dwPacketNumber", ctypes.c_uint), ("Gamepad", _Pad)]


def _load():
    if os.name != "nt":
        return None
    for name in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
        try:
            return getattr(ctypes.windll, name)
        except OSError:
            continue
    return None


class Gamepads:
    def __init__(self, reader=None):
        """reader(index) -> (buttons, lt, rt, lx, ly, rx, ry) or None; the default reads XInput (tests pass
        their own)."""
        self.dll = None if reader else _load()
        self.reader = reader or self._xinput
        self.connected = {}           # index -> previous (buttons, lt, rt, dirs)
        self.next_scan = 0.0
        self.held = {}                # direction -> (since, last repeat)

    @property
    def available(self):
        return self.reader is not self._xinput or self.dll is not None

    def _xinput(self, i):
        st = _State()
        if self.dll is None or self.dll.XInputGetState(i, ctypes.byref(st)) != 0:
            return None
        g = st.Gamepad
        return g.wButtons, g.bLeftTrigger, g.bRightTrigger, g.sThumbLX, g.sThumbLY, g.sThumbRX, g.sThumbRY

    def poll(self, now=None):
        """{"pressed": [names], "released": [names], "repeat": [directions], "scroll": (x, y) in -1..1,
        "any": bool, "pads": n}"""
        now = time.monotonic() if now is None else now
        out = {"pressed": [], "released": [], "repeat": [], "scroll": (0.0, 0.0), "any": False}
        indexes = list(self.connected)
        if now >= self.next_scan:
            self.next_scan = now + 2.0
            indexes = range(4)
        merged_buttons, lt, rt, rx, ry, dirs = 0, 0, 0, 0, 0, set()
        for i in indexes:
            st = self.reader(i)
            if st is None:
                self.connected.pop(i, None)
                continue
            b, l_, r_, lx, ly, rx_, ry_ = st
            self.connected[i] = True
            merged_buttons |= b
            lt, rt = max(lt, l_), max(rt, r_)
            if abs(rx_) > abs(rx):
                rx = rx_
            if abs(ry_) > abs(ry):
                ry = ry_
            if ly > STICK_DEAD:
                dirs.add("DPAD_UP")
            elif ly < -STICK_DEAD:
                dirs.add("DPAD_DOWN")
            if lx > STICK_DEAD:
                dirs.add("DPAD_RIGHT")
            elif lx < -STICK_DEAD:
                dirs.add("DPAD_LEFT")
        out["pads"] = len(self.connected)
        down = {n for n, mask in BUTTONS.items() if merged_buttons & mask} | dirs
        if lt > TRIGGER_ON:
            down.add("LT")
        if rt > TRIGGER_ON:
            down.add("RT")
        before = getattr(self, "down", set())
        out["pressed"] = sorted(down - before)
        out["released"] = sorted(before - down)
        self.down = down
        # held directions repeat (menus, grids)
        for d in DIRECTIONS:
            if d in down:
                since, last = self.held.get(d, (now, now))
                if d in out["pressed"]:
                    self.held[d] = (now, now)
                elif now - since > REPEAT_DELAY and now - last > REPEAT_EVERY:
                    self.held[d] = (since, now)
                    out["repeat"].append(d)
            else:
                self.held.pop(d, None)

        def axis(v):
            return 0.0 if abs(v) < RIGHT_DEAD else max(-1.0, min(1.0, v / 32767))
        out["scroll"] = (axis(rx), axis(ry))
        out["any"] = bool(out["pressed"] or out["repeat"] or any(out["scroll"]))
        return out
