"""
Subtitle styles for Kura (no Qt in here). Two uses:
  - Kura's player: mpv restyles the dialogue of the file's own subtitles while it plays
    (`mpv_overrides`), nothing on disk changes;
  - the Subtitles tool: the ASS tracks inside MKV files are rewritten with the new
    style (`restyle_file`), so Jellyfin and every other player show it too. FFmpeg does the
    remuxing; the original tracks are kept in subtitle_backups/ so `restore_file` can undo it.
  - the same tool's track editor (`edit_tracks`: remove, rename, default/forced, add from a file or
    Anime Tosho). Its first edit also saves the file's whole original layout, so a restore rebuilds
    the subtitles exactly as they were whatever was removed or added.
Only dialogue styles are changed by default: signs and typesetting keep the fansubber's look.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

import anime_organizer as org
import pgs

TOOLS_DIR = org.SCRIPT_DIR / "tools"
FFMPEG_DIR = TOOLS_DIR / "ffmpeg"
FFMPEG = FFMPEG_DIR / "ffmpeg.exe"
FFPROBE = FFMPEG_DIR / "ffprobe.exe"
# gyan.dev's essentials build (~115 MB), from its GitHub releases: gyan.dev itself sends a certificate chain
# Python refuses ("certificate has expired"; browsers cope)
FFMPEG_RELEASES = "https://api.github.com/repos/GyanD/codexffmpeg/releases/latest"
FFMPEG_FALLBACK = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
# picture subtitles (Blu-ray / DVD): their text is an image, nothing to restyle
IMAGE_SUBS = {"hdmv_pgs_subtitle", "pgssub", "dvd_subtitle", "dvdsub", "dvb_subtitle", "dvbsub", "xsub"}
BACKUP_DIR = org.SCRIPT_DIR / "subtitle_backups"
MARK = "; Restyled by Kura"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# sizes are in pixels on a 1080p picture; `size` is % of the file's own size; font "" = the file's
DEFAULT_STYLE = {"text": "#FFFFFF", "outline": "#000000", "shadow_color": "#000000", "outline_w": 3.0,
                 "shadow": 1.5, "size": 100, "font": "", "bold": False, "dialogue_only": True}
PRESETS = [
    ("classic", "Classic white", {"text": "#FFFFFF", "outline": "#000000", "shadow_color": "#000000",
                                  "outline_w": 3.0, "shadow": 1.5}),
    ("yellow", "Yellow", {"text": "#FFE14D", "outline": "#000000", "shadow_color": "#000000",
                          "outline_w": 3.0, "shadow": 1.5}),
    ("soft", "Soft grey", {"text": "#E4E4E4", "outline": "#1E1E1E", "shadow_color": "#000000",
                           "outline_w": 2.2, "shadow": 0.0}),
    ("contrast", "High contrast", {"text": "#FFFFFF", "outline": "#000000", "shadow_color": "#000000",
                                   "outline_w": 5.0, "shadow": 3.0}),
]

SIGN_NAMES = re.compile(r"sign|(?<!sub)title|(?:^|[^a-z])(?:op|ed|ts)(?:[^a-z]|$)|opening|ending|song|lyric|karaoke|kara|"
                        r"romaji|kanji|insert|note|eyecatch|preview|logo|credit|staff|next|^ep|caption|hud|typeset|"
                        r"screener|lower|upper|left|right", re.I)
DIALOGUE_NAMES = re.compile(r"default|main|dialog|top|ital|flash|thought|narr|overlap|alt|internal|speech|subtitle",
                            re.I)
# whole tracks that only carry signs and songs ("Signs & Songs", "S&S", "Forced")
SIGN_TRACKS = re.compile(r"sign|song|s ?& ?s|forced|karaoke", re.I)
POSITIONED = re.compile(r"\\(?:pos|move)\(")


def full_style(style):
    return {**DEFAULT_STYLE, **(style or {})}


# ---------------------------------------------------------------- ASS

def ass_colour(hex_colour, alpha=0):
    """#RRGGBB → ASS &HAABBGGRR (alpha 0 = opaque)."""
    h = hex_colour.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}"


def parse(text):
    """{play_res_y, scaled_border, format: [fields], styles: {name: {field: value}}, events: {style: (n, positioned)}}"""
    play = re.search(r"^PlayResY:\s*(\d+)", text, re.M | re.I)
    scaled = re.search(r"^ScaledBorderAndShadow:\s*(\w+)", text, re.M | re.I)
    out = {"play_res_y": int(play.group(1)) if play and int(play.group(1)) > 0 else 288,
           "scaled_border": not (scaled and scaled.group(1).lower() == "no"),
           "format": [], "styles": {}, "events": {}}
    section, ev_format = "", []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("["):
            section = s.lower()
            continue
        if "styles]" in section:
            if s.lower().startswith("format:"):
                out["format"] = [f.strip() for f in s[7:].split(",")]
            elif s.lower().startswith("style:") and out["format"]:
                vals = [v.strip() for v in s[6:].split(",", len(out["format"]) - 1)]
                st = dict(zip(out["format"], vals))
                if st.get("Name"):
                    out["styles"][st["Name"]] = st
        elif section == "[events]":
            if s.lower().startswith("format:"):
                ev_format = [f.strip().lower() for f in s[7:].split(",")]
            elif s.lower().startswith("dialogue:") and ev_format:
                vals = s[9:].split(",", len(ev_format) - 1)
                ev = dict(zip(ev_format, (v.strip() for v in vals)))
                name = ev.get("style", "").lstrip("*")
                n, pos = out["events"].get(name, (0, 0))
                out["events"][name] = (n + 1, pos + bool(POSITIONED.search(ev.get("text", ""))))
    return out


def dialogue_styles(info):
    """The styles that carry the spoken lines (not signs, songs or typesetting)."""
    styles, events = info["styles"], info["events"]
    if events:
        total = sum(n for n, _ in events.values())
        # some groups put a few signs in Default (Liar Liar: 213 of 540 lines placed by hand), so a style
        # is dialogue unless most of its lines are positioned
        picked = [name for name in styles
                  if events.get(name, (0, 0))[0] and not SIGN_NAMES.search(name)
                  and events[name][1] <= events[name][0] * 0.5
                  and (events[name][0] >= max(2, total * 0.02) or DIALOGUE_NAMES.search(name))]
        if picked:
            return picked
        free = [(n - p, name) for name, (n, p) in events.items() if name in styles and not SIGN_NAMES.search(name)]
        return [max(free)[1]] if free else []
    picked = [name for name in styles if DIALOGUE_NAMES.search(name) and not SIGN_NAMES.search(name)]
    if picked:
        return picked
    return ["Default"] if "Default" in styles else list(styles)[:1]


def new_values(style, original, info, video_height=1080, for_mpv=False):
    """{field: value} to put in one ASS style. mpv's overrides (libass) take ScaleX/Y as a ratio (1.15),
    the file itself as a percentage (115)."""
    st = full_style(style)
    unit = (info["play_res_y"] if info["scaled_border"] else video_height) / 1080
    vals = {"PrimaryColour": ass_colour(st["text"]), "OutlineColour": ass_colour(st["outline"]),
            "BackColour": ass_colour(st["shadow_color"], 0x60),
            "Outline": f"{st['outline_w'] * unit:.2f}".rstrip("0").rstrip("."),
            "Shadow": f"{st['shadow'] * unit:.2f}".rstrip("0").rstrip("."), "BorderStyle": "1"}
    if st["font"]:
        vals["Fontname"] = st["font"]
    if st["bold"]:
        vals["Bold"] = "-1"
    if int(st["size"]) != 100:
        for f in ("ScaleX", "ScaleY"):
            try:
                base = float(original.get(f) or 100)
            except ValueError:
                base = 100.0
            scaled = base * int(st['size']) / 100
            vals[f] = f"{scaled / 100:.3f}" if for_mpv else f"{scaled:.1f}".rstrip("0").rstrip(".")
    return vals


def target_styles(style, info):
    return dialogue_styles(info) if full_style(style)["dialogue_only"] else list(info["styles"])


def mpv_overrides(style, header, video_height=1080):
    """mpv's sub-ass-style-overrides list for a track's ASS header (Kura's player)."""
    info = parse(header or "")
    out = []
    for name in target_styles(style, info):
        if "," in name or "=" in name:
            continue   # mpv's option list can't carry these
        for field, value in new_values(style, info["styles"].get(name, {}), info, video_height, True).items():
            out.append(f"{name}.{field}={value}")
    return out


def restyle(text, style):
    """The whole ASS file with the dialogue styles changed."""
    info = parse(text)
    names = set(target_styles(style, info))
    fmt = info["format"]
    lines, section = [], ""
    for line in text.splitlines(keepends=True):
        s = line.strip()
        if s.startswith("["):
            section = s.lower()
        elif "styles]" in section and s.lower().startswith("style:") and fmt:
            vals = [v.strip() for v in s[6:].split(",", len(fmt) - 1)]
            st = dict(zip(fmt, vals))
            if st.get("Name") in names:
                st.update(new_values(style, st, info))
                end = line[len(line.rstrip("\r\n")):]
                line = "Style: " + ",".join(st.get(f, "") for f in fmt) + end
        lines.append(line)
    out = "".join(lines)
    if MARK not in out:
        out = re.sub(r"(\[Script Info\][^\r\n]*\r?\n)", lambda m: m.group(1) + MARK + "\n", out, count=1, flags=re.I)
    return out


# ---------------------------------------------------------------- FFmpeg

def ffmpeg_installed():
    return FFMPEG.exists() and FFPROBE.exists()


def ffmpeg_version():
    if not ffmpeg_installed():
        return None
    try:
        r = subprocess.run([str(FFMPEG), "-version"], capture_output=True, text=True, timeout=30,
                           creationflags=_NO_WINDOW)
        m = re.search(r"ffmpeg version (\S+)", r.stdout)
        return m.group(1).split("-")[0] if m else None
    except (OSError, subprocess.SubprocessError):
        return None


def install_ffmpeg(progress=None):
    """Download FFmpeg (gyan.dev's full build, the "shared" one: ffmpeg.exe + ffprobe.exe + their DLLs, ~100 MB;
    the full build has libplacebo, which runs Anime4K etc. for clips) into tools/ffmpeg. Essentials if the full
    one isn't listed."""
    FFMPEG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = FFMPEG_DIR / "ffmpeg.download"
    url = FFMPEG_FALLBACK
    try:
        req = urllib.request.Request(FFMPEG_RELEASES, headers={"User-Agent": org.USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            assets = json.load(resp).get("assets") or []
        url = next((a["browser_download_url"] for a in assets if a["name"].endswith("full_build-shared.zip")),
                   None) or next(a["browser_download_url"] for a in assets if a["name"].endswith("essentials_build.zip"))
    except (OSError, ValueError, StopIteration, KeyError):
        pass   # GitHub's API busy or the names changed: BtbN's build (bigger, same programs)
    req = urllib.request.Request(url, headers={"User-Agent": org.USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as f:
        total, done = int(resp.headers.get("Content-Length") or 0), 0
        while True:
            chunk = resp.read(1 << 18)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if progress and total:
                progress(done / total)
    try:
        with zipfile.ZipFile(tmp) as z:
            for name in z.namelist():
                base = name.rsplit("/", 1)[-1]
                if (base in ("ffmpeg.exe", "ffprobe.exe") or base.lower().endswith(".dll")) and "/bin/" in name:
                    part = FFMPEG_DIR / (base + ".part")
                    with z.open(name) as src, open(part, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    os.replace(part, FFMPEG_DIR / base)
    except zipfile.BadZipFile as e:
        raise OSError("the download looks broken") from e
    finally:
        tmp.unlink(missing_ok=True)
    if not ffmpeg_installed():
        raise OSError("ffmpeg.exe wasn't in the download")
    return ffmpeg_version()


def _run(args, timeout=1800):
    r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, creationflags=_NO_WINDOW)
    if r.returncode != 0:
        raise OSError((r.stderr or "").strip().splitlines()[-1] if r.stderr.strip() else f"exit code {r.returncode}")
    return r.stdout


def probe(path):
    """Every stream of a file: [{index, type, codec, lang, title, default, forced}] + duration."""
    out = json.loads(_run([str(FFPROBE), "-v", "error", "-show_entries",
                           "stream=index,codec_type,codec_name:stream_tags=language,title,filename:"
                           "stream_disposition=default,forced:format=duration", "-of", "json", str(path)],
                          timeout=120) or "{}")
    streams = [{"index": s["index"], "type": s.get("codec_type"), "codec": s.get("codec_name"),
                "lang": (s.get("tags") or {}).get("language"), "title": (s.get("tags") or {}).get("title"),
                "default": (s.get("disposition") or {}).get("default", 0),
                "forced": (s.get("disposition") or {}).get("forced", 0)} for s in out.get("streams", [])]
    try:
        duration = float((out.get("format") or {}).get("duration") or 0)
    except ValueError:
        duration = 0.0
    return streams, duration


class PictureSubs(Exception):
    """The file only has picture subtitles Kura can't handle (DVD), or none it can change."""


PGS = {"hdmv_pgs_subtitle", "pgssub"}
OCR_MARK = "OCR by Kura"


def picture_subs_only(streams):
    """The file's subtitles are all pictures (Blu-ray PGS / DVD)."""
    subs = [s for s in streams if s["type"] == "subtitle"]
    return bool(subs) and all(s["codec"] in IMAGE_SUBS for s in subs)


def ass_tracks(streams):
    """The ASS tracks to restyle: not the ones that only carry signs and songs."""
    return [s for s in streams if s["type"] == "subtitle" and s["codec"] == "ass"
            and not SIGN_TRACKS.search(s.get("title") or "")]


def pgs_tracks(streams):
    """Blu-ray picture subtitle tracks (recolourable, readable by OCR)."""
    return [s for s in streams if s["type"] == "subtitle" and s["codec"] in PGS]


TEXT_EXT = {"ass": ".ass", "ssa": ".ssa", "subrip": ".srt", "srt": ".srt", "webvtt": ".vtt", "mov_text": ".srt"}


def _ext(track):
    return ".sup" if track["codec"] in PGS else ".ass"


def track_ext(track):
    """The file extension a track is saved out with (Save as file)."""
    if track["codec"] in PGS:
        return ".sup"
    if track["codec"] in ("dvd_subtitle", "dvdsub"):
        return ".mks"   # VobSub pictures only travel inside Matroska
    return TEXT_EXT.get(track["codec"], ".mks")


def extract_track(path, index, dest):
    """Save one subtitle track as a file (text tracks as text; mov_text becomes SubRip)."""
    codec = ["-c", "copy"] if not str(dest).lower().endswith(".srt") else ["-c:s", "srt"]
    _run([str(FFMPEG), "-v", "error", "-y", "-copyts", "-i", str(path), "-map", f"0:{index}", *codec, str(dest)])
    return dest


def extract(path, tracks, outdir):
    """Pull subtitle tracks out of a file in one pass: {stream index: file}. -copyts here and in _remux: ffmpeg
    otherwise shifts each input to start at 0, and picture subtitles came back 0.3 s early or worse."""
    args, files = [str(FFMPEG), "-v", "error", "-y", "-copyts", "-i", str(path)], {}
    for t in tracks:
        dest = outdir / f"track_{t['index']}{_ext(t)}"
        args += ["-map", f"0:{t['index']}", "-c", "copy", str(dest)]
        files[t["index"]] = dest
    _run(args)
    return files


def extract_slice(path, index, start, seconds, dest):
    """A few minutes of one picture subtitle track with its real timestamps (for the preview); only that part
    of the file is read."""
    _run([str(FFMPEG), "-v", "error", "-y", "-copyts", "-ss", f"{start:.2f}", "-i", str(path),
          "-to", f"{start + seconds:.2f}", "-map", f"0:{index}", "-c", "copy", str(dest)], timeout=120)
    return dest


# ---------------------------------------------------------------- backups

def backup_folder(path):
    return BACKUP_DIR / hashlib.sha1(os.path.normcase(os.path.abspath(path)).encode("utf-8")).hexdigest()[:24]


def _load_backup(path):
    """What Kura changed in this file, or None (never, or the file was replaced since: then it's forgotten)."""
    bdir = backup_folder(path)
    try:
        saved = json.loads((bdir / "info.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if saved.get("size_after") != os.path.getsize(path):
        shutil.rmtree(bdir, ignore_errors=True)   # a new download with the same name: its tracks are the originals
        return None
    return saved


def has_backup(path):
    """Kura changed this file's subtitles (and it hasn't been replaced since)."""
    try:
        saved = json.loads((backup_folder(path) / "info.json").read_text(encoding="utf-8"))
        return saved.get("size_after") == os.path.getsize(path)
    except (OSError, ValueError):
        return False


def _originals(path, tracks, saved, work):
    """Each track as it was before Kura first touched it: {index: file}. Tracks seen for the first time are
    extracted now and kept in the backup folder, with their name, language and flags."""
    bdir = backup_folder(path)
    saved.setdefault("tracks", {})
    saved.setdefault("meta", {})
    new = [t for t in tracks if str(t["index"]) not in saved["tracks"]]
    if new:
        got = extract(path, new, work)
        bdir.mkdir(parents=True, exist_ok=True)
        # never over a file the backup already holds: after the track editor moved tracks, a new track can have
        # the number an original had
        used = set(saved["tracks"].values()) | {x.get("file") for x in saved.get("layout") or []}
        for t in new:
            name, k = f"track_{t['index']}{_ext(t)}", 2
            while name in used:
                name, k = f"track_{t['index']}_{k}{_ext(t)}", k + 1
            used.add(name)
            shutil.copy2(got[t["index"]], bdir / name)
            saved["tracks"][str(t["index"])] = name
            saved["meta"].setdefault(str(t["index"]), {k: t.get(k) for k in ("lang", "title", "default", "forced")})
    return {t["index"]: bdir / saved["tracks"][str(t["index"])] for t in tracks}


def _save_backup(path, saved):
    saved.update(path=os.fspath(path), size_after=os.path.getsize(path))
    saved.setdefault("saved", time.time())
    bdir = backup_folder(path)
    bdir.mkdir(parents=True, exist_ok=True)
    (bdir / "info.json").write_text(json.dumps(saved, indent=1), encoding="utf-8")


# ---------------------------------------------------------------- remux

def _plan(streams):
    """What the new file holds, in order: a copy of every stream to start with."""
    return [dict(s, file=None, set_meta=False, set_disp=False) for s in streams]


def _remux(path, plan, dest):
    """Write `plan` to `dest`: streams copied from `path` or taken from new files, nothing re-encoded."""
    args = [str(FFMPEG), "-v", "error", "-y", "-copyts", "-i", str(path)]
    inputs = {}
    for n, spec in enumerate(plan):
        if spec["file"] is not None:
            inputs[n] = len(inputs) + 1
            args += ["-i", str(spec["file"])]
    meta = []
    for n, spec in enumerate(plan):
        args += ["-map", f"{inputs[n]}:0" if spec["file"] is not None else f"0:{spec['index']}"]
        if spec["file"] is not None or spec["set_meta"]:
            for key, tag in (("lang", "language"), ("title", "title")):
                if spec.get(key) or spec["set_meta"]:
                    meta += [f"-metadata:s:{n}", f"{tag}={spec.get(key) or ''}"]
        if spec["file"] is not None or spec["set_disp"]:
            meta += [f"-disposition:{n}", "+".join(k for k in ("default", "forced") if spec.get(k)) or "0"]
    # strict interleaving: without it ffmpeg wrote the subtitle packets megabytes after their video, and
    # players streaming the file skipped most lines
    args += ["-map_metadata", "0", "-map_chapters", "0", "-c", "copy", "-max_interleave_delta", "0", *meta,
             str(dest)]
    _run(args)


def _swap(path, plan, duration):
    """Remux next to the original, check the result, then replace the original. The modified time is the
    new one on purpose: Jellyfin caches extracted subtitles per file and date, and would keep the old look."""
    path = os.fspath(path)
    size = os.path.getsize(path)
    folder = os.path.dirname(path)
    if shutil.disk_usage(folder).free < size * 1.05 + 50_000_000:
        raise OSError("not enough free space on the drive for a temporary copy")
    tmp = os.path.join(folder, f".kura-subs-{os.getpid()}-{int(time.time())}.mkv")
    try:
        _remux(path, plan, tmp)
        got, got_duration = probe(tmp)
        if [s["type"] for s in got] != [s["type"] for s in plan]:
            raise OSError("the new file's tracks don't match (nothing was changed)")
        if duration and abs(got_duration - duration) > 2:
            raise OSError("the new file's length doesn't match the original (nothing was changed)")
        if os.path.getsize(tmp) < size * 0.9:
            raise OSError("the new file is much smaller than the original (nothing was changed)")
        os.replace(tmp, path)   # fails if another app has the file open; then the original stays
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# ---------------------------------------------------------------- restyle / recolour

def restyle_file(path, style):
    """Restyle every ASS track (its dialogue styles) and recolour every Blu-ray picture track (text and
    outline colours) in a video file, always starting from the original tracks. Returns how many changed."""
    streams, duration = probe(path)
    st = full_style(style)
    ass, pics = ass_tracks(streams), pgs_tracks(streams)
    if not ass and not pics:
        if picture_subs_only(streams):
            raise PictureSubs()
        return 0
    saved = _load_backup(path) or {}
    with tempfile.TemporaryDirectory(prefix="kura_subs_") as td:
        work = Path(td)
        # Kura's OCR text track starts from the text it read (kept in the backup), the others from their originals
        ocr_text = backup_folder(path) / (saved.get("ocr") or {}).get("text", "-")
        made = [t for t in ass if OCR_MARK in (t.get("title") or "") and ocr_text.exists()]
        originals = _originals(path, [t for t in ass + pics if t not in made], saved, work)
        originals.update({t["index"]: ocr_text for t in made})
        plan = _plan(streams)
        by_index = {spec["index"]: spec for spec in plan}
        for t in ass + pics:
            src = originals[t["index"]]
            if t in ass:
                dest = work / f"new_{t['index']}.ass"
                dest.write_text(restyle(src.read_text(encoding="utf-8-sig", errors="replace"), st), encoding="utf-8")
            else:
                dest = work / f"new_{t['index']}.sup"
                dest.write_bytes(pgs.recolor(src.read_bytes(), st["text"], st["outline"]))
            by_index[t["index"]]["file"] = dest
        _swap(path, plan, duration)
        saved["style"] = st
        _save_backup(path, saved)
    return len(ass) + len(pics)


# ---------------------------------------------------------------- OCR

def _ass_time(t):
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def ocr_ass(lines, width, height, title="English"):
    """An ASS file from OCR'd subtitles: lines = [(start, end, text with \\N, top of the screen)]."""
    k = height / 1080
    head = ("[Script Info]\n; Made by Kura: text read from Blu-ray picture subtitles\n"
            f"Title: {title}\nScriptType: v4.00+\nPlayResX: {width}\nPlayResY: {height}\n"
            "ScaledBorderAndShadow: yes\nWrapStyle: 0\n\n[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
            "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding\n"
            f"Style: Default,Arial,{round(64 * k)},&H00FFFFFF,&H000000FF,&H00000000,&H60000000,-1,0,0,0,100,100,0,0,"
            f"1,{3 * k:.1f},{1.5 * k:.1f},2,{round(60 * k)},{round(60 * k)},{round(50 * k)},1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    top_tag = "{\\an8}"
    body = [f"Dialogue: 0,{_ass_time(a)},{_ass_time(b)},Default,,0,0,0,,{top_tag if top else ''}{text}\n"
            for a, b, text, top in lines]
    return head + "".join(body)


def ocr_source(streams, originals):
    """The picture track to read: the biggest English (or untagged) one; small ones are signs / forced."""
    pics = pgs_tracks(streams)
    english = [t for t in pics if (t.get("lang") or "und").lower() in ("eng", "en", "und")] or pics
    return max(english, key=lambda t: originals[t["index"]].stat().st_size)


def ocr_file(path, replace=False, progress=None, cancelled=None):
    """Read a Blu-ray picture subtitle track with Windows' OCR and put it in the file as a text track (ASS):
    next to the pictures (default; the text track becomes the one players pick) or instead of them.
    Returns the number of lines read (0 = nothing to do)."""
    streams, duration = probe(path)
    if any(OCR_MARK in (s.get("title") or "") for s in streams):
        return 0   # done before
    pics = pgs_tracks(streams)
    if not pics:
        if picture_subs_only(streams):
            raise PictureSubs()
        return 0
    saved = _load_backup(path) or {}
    with tempfile.TemporaryDirectory(prefix="kura_ocr_") as td:
        work = Path(td)
        originals = _originals(path, pics, saved, work)
        src = ocr_source(streams, originals)
        found = pgs.events(originals[src["index"]].read_bytes())
        if not found:
            return 0
        lines = pgs.ocr_events(found, progress=progress, cancelled=cancelled)
        if cancelled and cancelled():
            return 0
        if duration:   # a last subtitle without an end mark mustn't make the file longer
            lines = [(a, min(b, duration), t, top) for a, b, t, top in lines if a < duration]
        lang = (src.get("lang") or "eng").lower()
        name = "English" if lang in ("eng", "en", "und") else lang
        ass_file = work / "ocr.ass"
        ass_file.write_text(ocr_ass(lines, found[0]["width"], found[0]["height"], name), encoding="utf-8")
        plan = _plan(streams)
        title = f"{name} (text, {OCR_MARK})"
        if replace:
            spec = next(p for p in plan if p["index"] == src["index"])
            spec.update(file=ass_file, title=title, codec="ass", default=1)
            saved["ocr"] = {"mode": "replace", "src": src["index"]}
        else:
            for p in plan:   # the text track becomes the one players pick
                if p["type"] == "subtitle" and p.get("default"):
                    saved["meta"].setdefault(str(p["index"]), {k: p.get(k) for k in
                                                               ("lang", "title", "default", "forced")})
                    p.update(default=0, set_disp=True)
            last_sub = max(i for i, p in enumerate(plan) if p["type"] == "subtitle")
            plan.insert(last_sub + 1, {"type": "subtitle", "index": None, "codec": "ass", "file": ass_file,
                                       "lang": src.get("lang") or "eng", "title": title, "default": 1, "forced": 0,
                                       "set_meta": True, "set_disp": True})
            saved["ocr"] = {"mode": "add", "src": src["index"]}
        _swap(path, plan, duration)
        shutil.copy2(ass_file, backup_folder(path) / "ocr.ass")
        saved["ocr"]["text"] = "ocr.ass"
        _save_backup(path, saved)
    return len(lines)


# ---------------------------------------------------------------- restore

def restore_file(path):
    """Undo everything Kura did to this file's subtitles: original tracks, names and flags back, Kura's OCR
    text track removed. Returns how many tracks changed."""
    saved = _load_backup(path)
    bdir = backup_folder(path)
    if not saved:
        return 0
    streams, duration = probe(path)
    if saved.get("layout"):
        return _restore_layout(path, streams, duration, saved, bdir)
    backed, meta = saved.get("tracks") or {}, saved.get("meta") or {}
    added = (saved.get("ocr") or {}).get("mode") == "add"
    plan, changed = [], 0
    for s in streams:
        key = str(s["index"])
        if added and OCR_MARK in (s.get("title") or ""):
            changed += 1
            continue   # Kura's OCR text track goes
        spec = dict(s, file=None, set_meta=False, set_disp=False)
        if key in backed and (bdir / backed[key]).exists():
            spec.update(meta.get(key) or {}, file=bdir / backed[key], set_meta=True, set_disp=True)
            changed += 1
        elif key in meta:
            spec.update(meta[key], set_disp=True)
            changed += 1
        plan.append(spec)
    if changed:
        _swap(path, plan, duration)
    shutil.rmtree(bdir, ignore_errors=True)
    return changed


# ---------------------------------------------------------------- track editor

def _ensure_layout(path, streams, saved, work):
    """Before the first edit that may remove, add or reorder tracks: the file's original order of streams, with
    every subtitle track's original content saved. Restyle / OCR never reorder anything, so the current file
    minus Kura's added OCR track is the original layout; their backed-up tracks are the originals."""
    if saved.get("layout"):
        return
    added_ocr = (saved.get("ocr") or {}).get("mode") == "add"
    subs = [t for t in streams if t["type"] == "subtitle" and not (added_ocr and OCR_MARK in (t.get("title") or ""))]
    _originals(path, subs, saved, work)   # tracks not saved yet are extracted now (as they are: unchanged)
    layout = []
    for t in streams:
        if t["type"] != "subtitle":
            layout.append({"type": t["type"], "codec": t["codec"]})
        elif t in subs:
            key = str(t["index"])
            layout.append({"type": "subtitle", "codec": t["codec"], "file": saved["tracks"][key],
                           "meta": saved["meta"].get(key) or {k: t.get(k) for k in ("lang", "title", "default",
                                                                                     "forced")}})
    saved["layout"] = layout


def _restore_layout(path, streams, duration, saved, bdir):
    """Rebuild the original subtitles from the saved layout: the other streams (never touched) are copied in
    their order, the original subtitle tracks come from the backup."""
    others = [s for s in streams if s["type"] != "subtitle"]
    plan, k = [], 0
    for item in saved["layout"]:
        if item["type"] != "subtitle":
            if k >= len(others):
                raise OSError("this file's other tracks changed since; can't restore it")
            plan.append(dict(others[k], file=None, set_meta=False, set_disp=False))
            k += 1
        else:
            src = bdir / item["file"]
            if not src.exists():
                raise OSError(f"the saved original track {item['file']} is missing")
            m = item.get("meta") or {}
            plan.append({"type": "subtitle", "index": None, "codec": item["codec"], "file": src,
                         "lang": m.get("lang"), "title": m.get("title"), "default": m.get("default", 0),
                         "forced": m.get("forced", 0), "set_meta": True, "set_disp": True})
    _swap(path, plan, duration)
    shutil.rmtree(bdir, ignore_errors=True)
    return sum(1 for s in streams if s["type"] == "subtitle") or 1


def edit_tracks(path, wanted):
    """Make the file's subtitle tracks `wanted`, in that order: [{"src": stream index of a track that's there, or
    None with "file": a subtitle file to add, "lang", "title", "default", "forced", "codec"}]. Tracks not listed
    are removed. Other streams aren't touched. Returns the number of subtitle tracks in the new file."""
    streams, duration = probe(path)
    saved = _load_backup(path) or {}
    with tempfile.TemporaryDirectory(prefix="kura_tracks_") as td:
        _ensure_layout(path, streams, saved, Path(td))
        by_index = {t["index"]: t for t in streams}
        subs = []
        for n, w in enumerate(wanted):
            base = dict(by_index[w["src"]]) if w.get("src") is not None else {"type": "subtitle", "index": None}
            file = Path(w["file"]) if w.get("file") else None
            fmt = {".ass": "ass", ".ssa": "ass", ".srt": "srt"}.get(file.suffix.lower()) if file else None
            if fmt and duration:   # lines past the end would make the file longer (another release's subtitles)
                fitted = Path(td) / f"added_{n}{file.suffix.lower()}"
                fitted.write_text(clip_text(file.read_text(encoding="utf-8-sig", errors="replace"), fmt, duration),
                                  encoding="utf-8")
                file = fitted
            base.update(file=file, codec=w.get("codec") or base.get("codec"),
                        lang=w.get("lang") or "", title=w.get("title") or "", default=int(bool(w.get("default"))),
                        forced=int(bool(w.get("forced"))), set_meta=True, set_disp=True)
            subs.append(base)
        # the subtitle block goes where the file had it (else after the video and audio)
        others = [dict(t, file=None, set_meta=False, set_disp=False) for t in streams if t["type"] != "subtitle"]
        first = next((i for i, t in enumerate(streams) if t["type"] == "subtitle"), None)
        if first is None:
            at = sum(1 for t in streams if t["type"] in ("video", "audio"))
        else:
            at = sum(1 for t in streams[:first] if t["type"] != "subtitle")
        plan = others[:at] + subs + others[at:]
        _swap(path, plan, duration)
        # what Kura saved per track follows the tracks to their new places (restyle starts from them)
        tracks, meta = saved.get("tracks") or {}, saved.get("meta") or {}
        new_tracks, new_meta = {}, {}
        for n, spec in enumerate(plan):
            old = spec.get("index")
            if spec["type"] == "subtitle" and old is not None and str(old) in tracks and spec.get("file") is None:
                new_tracks[str(n)] = tracks[str(old)]
                if str(old) in meta:
                    new_meta[str(n)] = meta[str(old)]
        saved["tracks"], saved["meta"] = new_tracks, new_meta
        if saved.get("ocr"):
            saved["ocr"].pop("src", None)
        _save_backup(path, saved)
    return len(subs)


# ---------------------------------------------------------------- downloaded subtitles

ASS_TIME = re.compile(r"^(Dialogue|Comment):(\s*\d+,)(\d+:\d\d:\d\d[.,]\d\d),(\d+:\d\d:\d\d[.,]\d\d)", re.M)
SRT_TIME = re.compile(r"(\d\d):(\d\d):(\d\d)[,.](\d{3})")


def _ass_secs(t):
    h, m, rest = t.replace(",", ".").split(":")
    return int(h) * 3600 + int(m) * 60 + float(rest)


def shift_text(text, seconds, fmt):
    """Move every line of an ASS or SRT subtitle by `seconds` (a release with a different intro / cut)."""
    if not seconds:
        return text
    if fmt == "ass":
        def one(m):
            a = max(0.0, _ass_secs(m.group(3)) + seconds)
            b = max(0.0, _ass_secs(m.group(4)) + seconds)
            return f"{m.group(1)}:{m.group(2)}{_ass_time(a)},{_ass_time(b)}"
        return ASS_TIME.sub(one, text)

    def srt(m):
        ms = max(0, round(((int(m.group(1)) * 60 + int(m.group(2))) * 60 + int(m.group(3)) + seconds) * 1000
                          + int(m.group(4))))
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"
    return SRT_TIME.sub(srt, text)


def clip_text(text, fmt, duration):
    """Drop lines that start after the video ends and end the rest by then."""
    if fmt == "ass":
        out = []
        for line in text.splitlines(keepends=True):
            m = ASS_TIME.match(line)
            if m:
                a, b = _ass_secs(m.group(3)), _ass_secs(m.group(4))
                if a >= duration:
                    continue
                if b > duration:
                    line = line[:m.start(4)] + _ass_time(duration - 0.01) + line[m.end(4):]
            out.append(line)
        return "".join(out)
    blocks, out = re.split(r"\r?\n\s*\r?\n", text.strip()), []
    for block in blocks:
        times = SRT_TIME.findall(block)
        if len(times) >= 2:
            secs = [int(h) * 3600 + int(m) * 60 + int(s_) + int(ms) / 1000 for h, m, s_, ms in times[:2]]
            if secs[0] >= duration:
                continue
            if secs[1] > duration:
                end = round((duration - 0.01) * 1000)
                stamp = f"{end // 3600000:02d}:{end // 60000 % 60:02d}:{end // 1000 % 60:02d},{end % 1000:03d}"
                second = list(SRT_TIME.finditer(block))[1]
                block = block[:second.start()] + stamp + block[second.end():]
        out.append(block)
    return "\n\n".join(out) + "\n"


def first_line_time(text, fmt):
    """When the first dialogue line starts (seconds), for "try it in the player"."""
    if fmt == "ass":
        m = ASS_TIME.search(text)
        return _ass_secs(m.group(3)) if m else 0.0
    m = SRT_TIME.search(text)
    return (int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + int(m.group(4)) / 1000) if m else 0.0


def sidecar_path(video, lang, ext):
    """"Show - 05.mkv" -> "Show - 05.eng.ass" (Jellyfin and mpv pick it up by that name); never over another file."""
    video = Path(video)
    base = video.with_name(f"{video.stem}.{lang or 'und'}{ext}")
    n = 2
    while base.exists():
        base = video.with_name(f"{video.stem}.{lang or 'und'}.{n}{ext}")
        n += 1
    return base
