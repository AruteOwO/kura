"""
Picture subtitles (Blu-ray PGS, .sup) for Kura, no Qt in here:
  - `recolor`: change the colours in each subtitle's palette (text and outline), nothing else moves;
  - `events`: decode every subtitle picture with its start/end, for OCR (`ocr_events` turns them into text).
PGS = segments "PG" + PTS + DTS + type + size: PCS (0x16) says what's on screen, WDS (0x17) windows,
PDS (0x14) palettes (Y Cr Cb A), ODS (0x15) run-length pictures, END (0x80).
"""

import struct

PDS, ODS, PCS, WDS, END = 0x14, 0x15, 0x16, 0x17, 0x80


def segments(data):
    """[(pts seconds, type, payload start, payload end)]"""
    out, i, n = [], 0, len(data)
    while i + 13 <= n:
        if data[i:i + 2] != b"PG":
            nxt = data.find(b"PG", i + 1)   # damaged stream: find the next segment
            if nxt < 0:
                break
            i = nxt
            continue
        pts = struct.unpack_from(">I", data, i + 2)[0] / 90000
        kind = data[i + 10]
        size = struct.unpack_from(">H", data, i + 11)[0]
        out.append((pts, kind, i + 13, i + 13 + size))
        i += 13 + size
    return out


# ---------------------------------------------------------------- colours

def _ycc_to_rgb(y, cr, cb):
    """BT.709, limited range (what Blu-ray subtitles use)."""
    y, cb, cr = (y - 16) * 1.164, cb - 128, cr - 128
    r, g, b = y + 1.793 * cr, y - 0.213 * cb - 0.533 * cr, y + 2.112 * cb
    return tuple(max(0, min(255, int(round(v)))) for v in (r, g, b))


def _rgb_to_ycc(r, g, b):
    y = 16 + 0.183 * r + 0.614 * g + 0.062 * b
    cb = 128 - 0.101 * r - 0.339 * g + 0.439 * b
    cr = 128 + 0.439 * r - 0.399 * g - 0.040 * b
    return tuple(max(16, min(240, int(round(v)))) for v in (y, cr, cb))


def _hex(c):
    c = c.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def recolor(data, text="#FFFFFF", outline="#000000"):
    """New .sup bytes with the subtitles' colours changed: in each palette the brightest colour becomes your
    text colour, the darkest your outline colour, and the shades between (anti-aliasing) blend between the
    two by brightness. The original colour doesn't matter (white, yellow...). Transparency, shapes,
    positions and timing stay as they are."""
    out = bytearray(data)
    t_rgb, o_rgb = _hex(text), _hex(outline)
    for _, kind, a, b in segments(data):
        if kind != PDS:
            continue
        slots = [j for j in range(a + 2, b - 4, 5) if out[j + 4]]
        if not slots:
            continue
        lo = min(out[j + 1] for j in slots)
        hi = max(out[j + 1] for j in slots)
        span = max(1, hi - lo)
        for j in slots:
            t = (out[j + 1] - lo) / span
            rgb = tuple(o + (tc - o) * t for o, tc in zip(o_rgb, t_rgb))
            out[j + 1], out[j + 2], out[j + 3] = _rgb_to_ycc(*rgb)
    return bytes(out)


# ---------------------------------------------------------------- pictures

def _rle(data, w, h):
    """PGS run-length data → w*h palette indices."""
    px = bytearray()
    i, n = 0, len(data)
    while i < n:
        b = data[i]
        i += 1
        if b:
            px.append(b)
            continue
        if i >= n:
            break
        f = data[i]
        i += 1
        if f == 0:   # end of line: pad it
            rem = len(px) % w
            if rem:
                px.extend(bytes(w - rem))
            continue
        top = f >> 6
        if top == 0:
            px.extend(bytes(f & 0x3F))
        elif top == 1:
            px.extend(bytes(((f & 0x3F) << 8) | data[i]))
            i += 1
        elif top == 2:
            px.extend(bytes([data[i]]) * (f & 0x3F))
            i += 1
        else:
            px.extend(bytes([data[i + 1]]) * (((f & 0x3F) << 8) | data[i]))
            i += 2
    if len(px) < w * h:
        px.extend(bytes(w * h - len(px)))
    return px[:w * h]


def events(data):
    """Every subtitle shown: [{start, end, width, height, objects: [(x, y, w, h, indices, palette)]}].
    palette = {index: (Y, Cr, Cb, A)}."""
    segs = segments(data)
    palettes, objects, partial = {}, {}, {}
    shown, out = None, []
    screen = (1920, 1080)
    pending = None   # the display set being read: (pts, palette id, [(obj id, x, y)])
    for pts, kind, a, b in segs:
        seg = data[a:b]
        if kind == PCS and len(seg) >= 11:
            screen = struct.unpack_from(">HH", seg, 0)
            state, pal_id, count = seg[7], seg[9], seg[10]
            if state & 0x80:   # epoch start: everything is new
                palettes, objects = {}, {}
            comps, j = [], 11
            for _ in range(count):
                oid, _, crop, x, y = struct.unpack_from(">HBBHH", seg, j)
                comps.append((oid, x, y))
                j += 16 if crop & 0x40 else 8
            pending = (pts, pal_id, comps)
        elif kind == PDS and len(seg) >= 2:
            pal = palettes.setdefault(seg[0], {})
            for j in range(2, len(seg) - 4, 5):
                pal[seg[j]] = (seg[j + 1], seg[j + 2], seg[j + 3], seg[j + 4])
        elif kind == ODS and len(seg) >= 4:
            oid, flag = struct.unpack_from(">H", seg, 0)[0], seg[3]
            if flag & 0x80:   # first part: size, then width and height
                w, h = struct.unpack_from(">HH", seg, 7)
                partial[oid] = [w, h, bytearray(seg[11:])]
            elif oid in partial:
                partial[oid][2].extend(seg[4:])
            if flag & 0x40 and oid in partial:
                w, h, rle = partial.pop(oid)
                objects[oid] = (w, h, bytes(rle))
        elif kind == END and pending is not None:
            pts0, pal_id, comps = pending
            pending = None
            if shown is not None:
                shown["end"] = pts0
                out.append(shown)
                shown = None
            objs = []
            for oid, x, y in comps:
                if oid in objects:
                    w, h, rle = objects[oid]
                    objs.append((x, y, w, h, rle, dict(palettes.get(pal_id, {}))))
            if objs:
                shown = {"start": pts0, "end": None, "width": screen[0], "height": screen[1], "objects": objs}
    if shown is not None:
        shown["end"] = shown["start"] + 4
        out.append(shown)
    return [e for e in out if e["end"] > e["start"]]


def ink(obj):
    """One subtitle picture as dark text on white (what OCR reads best): (w, h, Gray8 bytes)."""
    x, y, w, h, rle, pal = obj
    idx = _rle(rle, w, h)
    lut = bytearray(256)
    for k in range(256):
        yy, cr, cb, alpha = pal.get(k, (16, 128, 128, 0))
        bright = max(0.0, min(1.0, (yy - 80) / (235 - 80)))
        lut[k] = 255 - int(255 * bright * alpha / 255)
    return w, h, idx.translate(bytes(lut))


def split_lines(w, h, gray, gap=3):
    """Cut a subtitle picture into its text lines (rows of white between them): [(y0, y1)]."""
    blank = [min(gray[r * w:(r + 1) * w]) > 235 for r in range(h)]
    lines, start, run = [], None, 0
    for r, empty in enumerate(blank + [True] * gap):
        if not empty:
            if start is None:
                start = r
            run = 0
        elif start is not None:
            run += 1
            if run >= gap:
                end = r - run + 1
                if end - start >= 6:
                    lines.append((start, end))
                start, run = None, 0
    return lines


def half(w, h, gray):
    """Half size (2×2 average): Windows OCR reads these subtitle fonts best at ~35 px line height
    ("I'm" came out as "11m" at full size, and a few lines not at all)."""
    w2, h2 = w // 2, h // 2
    out = bytearray(w2 * h2)
    for r in range(h2):
        a = gray[(2 * r) * w:(2 * r) * w + 2 * w2]
        b = gray[(2 * r + 1) * w:(2 * r + 1) * w + 2 * w2]
        row = r * w2
        for c in range(w2):
            k = 2 * c
            out[row + c] = (a[k] + a[k + 1] + b[k] + b[k + 1]) >> 2
    return w2, h2, out


# ---------------------------------------------------------------- OCR (Windows' built-in text recognition)

import re as _re

_FIXES = [(_re.compile(r"\b[1l|]{2}m\b"), "I'm"), (_re.compile(r"\b[1l|]'m\b"), "I'm"),
          (_re.compile(r"\b[1|]'(ll|ve|d)\b"), r"I'\1"), (_re.compile(r"(?<![\w'])[1|](?= [a-z])"), "I"),
          (_re.compile(r"\s+([?!,;:])"), r"\1"), (_re.compile(r"\s+\.(?!\.)"), "."), (_re.compile(r"\s{2,}"), " ")]


def clean(text):
    """Common OCR slips in subtitle fonts ("11m" for I'm, "Yes ?")."""
    for pattern, repl in _FIXES:
        text = pattern.sub(repl, text)
    return text.strip()


def _engine():
    from winrt.windows.globalization import Language
    from winrt.windows.media.ocr import OcrEngine
    eng = OcrEngine.try_create_from_language(Language("en-US")) or OcrEngine.try_create_from_user_profile_languages()
    if eng is None:
        raise OSError("Windows has no text recognition language installed (Settings → Time & language → "
                      "Language → add English with its optional features)")
    return eng


async def _read(eng, w, h, gray, pad=24):
    from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.storage.streams import DataWriter
    W, H = w + 2 * pad, h + 2 * pad
    buf = bytearray(b"\xff" * (W * H))
    for r in range(h):
        buf[(r + pad) * W + pad:(r + pad) * W + pad + w] = gray[r * w:(r + 1) * w]
    dw = DataWriter()
    dw.write_bytes(bytes(buf))
    bitmap = SoftwareBitmap.create_copy_from_buffer(dw.detach_buffer(), BitmapPixelFormat.GRAY8, W, H)
    result = await eng.recognize_async(bitmap)
    return " ".join(line.text for line in result.lines)


async def _read_line(eng, w, h, gray):
    """One line of text. Half size reads these fonts best; full size wins for very short lines ("Wha?") and
    when only the capitals differ."""
    small = await _read(eng, *half(w, h, gray)) if h >= 24 else ""
    if small and len(small) > 5:
        full = await _read(eng, w, h, gray)
        return full if full and full.lower() == small.lower() else small
    full = await _read(eng, w, h, gray)
    return full or small


def ocr_events(found, progress=None, cancelled=None):
    """[(start, end, text with \\N between lines, top of the screen)] for decoded subtitle pictures. Lines
    that read as nothing are left out; identical subtitles shown back to back become one."""
    import asyncio

    async def run():
        eng = _engine()
        out = []
        for n, e in enumerate(found):
            if cancelled and cancelled():
                break
            if progress:
                progress(n, len(found))
            texts, top = [], False
            for obj in e["objects"]:
                w, h, gray = ink(obj)
                top = top or (obj[1] + obj[3] / 2 < e["height"] * 0.4)
                for y0, y1 in split_lines(w, h, gray):
                    t = clean(await _read_line(eng, w, y1 - y0, gray[y0 * w:y1 * w]))
                    if t:
                        texts.append(t)
            if not texts:
                continue
            text = "\\N".join(texts)
            if out and out[-1][2] == text and e["start"] - out[-1][1] < 0.15:
                out[-1] = (out[-1][0], e["end"], text, top)
            else:
                out.append((e["start"], e["end"], text, top))
        return out

    try:   # this thread may not have a Windows apartment yet; WinRT needs one
        from winrt import init_apartment, MTA
        init_apartment(MTA)
    except Exception:
        pass
    return asyncio.run(run())
