"""
Screenshots and clips from Kura's player (no Qt in here).

- Where they go: `capture_dir` (default Pictures\\Kura), a folder per show (`capture_subfolders`), names from
  `capture_name` ("{show} - {ep} - {time}"). `captures.json` in that folder remembers which show / episode / moment
  each file came from (the Captures tool and "use as banner / folder icon" need it).
- Clips (`render_clip`): FFmpeg from the episode file, re-encoded so they start on the exact frame (or a fast
  copy that snaps to keyframes). The subtitles shown in the player are burned in: text tracks with your Style
  (subtitles.restyle, dialogue only) and the release's own fonts, picture tracks (Blu-ray PGS) overlaid.
  "Fit under N MB" picks the bitrate from the length and checks the result (one more try if it's over).
  GPU encoding (NVENC) when the card has it; AV1 on the CPU (RTX 30 cards can't encode AV1).
- Picture enhancement in clips: the player's upscalers (Anime4K / FSRCNNX / ArtCNN), deband and sharpen run in
  FFmpeg's libplacebo filter on the graphics card (Vulkan), the shaders joined into one file in the player's order,
  upscaled to `clip_enh_size`; subtitles are burned in after it, so they stay sharp. Needs the full FFmpeg build
  (`has_placebo`); without it, or if Vulkan fails, the clip is made without and a note says why.
- Contact sheets: frames spread over an episode in one image (`contact_sheet`).
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import subtitles as subs

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SHOT_FORMATS = {"png": ("PNG", ".png"), "jpg": ("JPEG", ".jpg"), "webp": ("WEBP", ".webp"), "avif": ("AVIF", ".avif")}
CLIP_FORMATS = {   # key: (name, extension, has audio, video codec choice)
    "h264": ("MP4 · H.264 (plays everywhere)", ".mp4", True),
    "h265": ("MP4 · H.265 (smaller)", ".mp4", True),
    "av1": ("MP4 · AV1 (smallest, slower)", ".mp4", True),
    "webm": ("WebM · VP9", ".webm", True),
    "gif": ("GIF", ".gif", False),
    "webp": ("Animated WebP", ".webp", False),
    "copy": ("Fast copy · MKV (keeps subtitle tracks, cuts at keyframes)", ".mkv", True),
}
SIZES = {"original": None, "2160": 2160, "1440": 1440, "1080": 1080, "720": 720, "480": 480}
QUALITY = {"high": (19, 30, 32), "medium": (23, 34, 36), "small": (28, 38, 42)}   # x264/NVENC cq, VP9 crf, AV1 crf
DEFAULTS = {
    "capture_dir": "", "capture_subfolders": True, "capture_name": "{show} - {ep} - {time}",
    "shot_format": "png", "shot_quality": 92, "shot_size": "original", "shot_enhanced": True, "shot_clipboard": False,
    "clip_format": "h264", "clip_size": "1080", "clip_limit": 0, "clip_quality": "high", "clip_subs": True,
    "clip_audio": True, "clip_gpu": True, "gif_fps": 15, "gif_width": 480, "replay_secs": 30,
    "clip_enhanced": True, "clip_enh_size": "1440",
}
VIDEO_FORMATS = ("h264", "h265", "av1", "webm")   # the ones picture enhancement is used for
INDEX = "captures.json"
_index_lock = threading.Lock()


def opt(settings, key):
    v = settings.get(key)
    return DEFAULTS[key] if v is None or v == "" and key != "capture_dir" else v


def default_dir():
    pics = Path(os.environ.get("USERPROFILE") or Path.home()) / "Pictures"
    return pics / "Kura"


def root_dir(settings):
    return Path(settings.get("capture_dir") or default_dir())


def safe(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", str(name or "")).strip(" .")
    return re.sub(r"\s+", " ", name)[:120] or "Kura"


def stamp(seconds):
    s = int(seconds or 0)
    return f"{s // 3600}h{s // 60 % 60:02d}m{s % 60:02d}s" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"


def out_path(settings, info, ext, at, end=None):
    """Where a capture of `info` ({show, season, episode, title}) at `at` seconds goes (never over a file)."""
    show = safe(info.get("show") or info.get("title") or "Kura")
    season, ep = info.get("season"), info.get("episode")
    if ep is not None:
        ep_text = (f"S{int(season):02d}E{int(ep):02d}" if season else f"E{int(ep):02d}") \
            if float(ep).is_integer() else f"E{ep}"
    else:
        ep_text = safe(Path(info.get("path") or "").stem)[:40]
    when = stamp(at) + (f"-{stamp(end)}" if end is not None else "")
    pattern = opt(settings, "capture_name") or DEFAULTS["capture_name"]
    try:
        name = pattern.format(show=show, ep=ep_text, time=when, date=time.strftime("%Y-%m-%d"),
                              season=season or "", episode=ep if ep is not None else "")
    except (KeyError, IndexError, ValueError):
        name = DEFAULTS["capture_name"].format(show=show, ep=ep_text, time=when)
    folder = root_dir(settings) / (show if opt(settings, "capture_subfolders") else "")
    folder.mkdir(parents=True, exist_ok=True)
    dest, n = folder / (safe(name) + ext), 2
    while dest.exists():
        dest, n = folder / f"{safe(name)} ({n}){ext}", n + 1
    return dest


# ---------------------------------------------------------------- index (which show / moment a file is from)

def _index_file(settings):
    return root_dir(settings) / INDEX


def load_index(settings):
    try:
        return json.loads(_index_file(settings).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def remember(settings, dest, info, at, kind, end=None):
    with _index_lock:
        idx = load_index(settings)
        key = os.path.relpath(dest, root_dir(settings))
        idx[key] = {"kind": kind, "source": info.get("path"), "show": info.get("show"), "folder": info.get("folder"),
                    "mid": info.get("mid"), "season": info.get("season"), "episode": info.get("episode"),
                    "at": at, "end": end, "made": time.time()}
        f = _index_file(settings)
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            tmp = f.with_suffix(".part")
            tmp.write_text(json.dumps(idx, indent=1), encoding="utf-8")
            os.replace(tmp, f)
        except OSError:
            pass


def forget(settings, paths):
    with _index_lock:
        idx = load_index(settings)
        for p in paths:
            idx.pop(os.path.relpath(p, root_dir(settings)), None)
        try:
            _index_file(settings).write_text(json.dumps(idx, indent=1), encoding="utf-8")
        except OSError:
            pass


# ---------------------------------------------------------------- screenshots

def save_shot(image, dest, settings):
    """A PIL image (from mpv) saved as the chosen format / size. `dest` already has the extension."""
    from PIL import Image
    img = image.convert("RGB")
    h = SIZES.get(opt(settings, "shot_size"))
    if h and img.height > h:
        img = img.resize((round(img.width * h / img.height), h), Image.LANCZOS)
    fmt = Path(dest).suffix.lower()
    q = int(opt(settings, "shot_quality"))
    if fmt == ".png":
        img.save(dest, "PNG", compress_level=6)
    elif fmt == ".jpg":
        img.save(dest, "JPEG", quality=q, subsampling=0 if q >= 90 else 2, optimize=True)
    elif fmt == ".webp":
        img.save(dest, "WEBP", quality=q, method=5)
    else:
        img.save(dest, "AVIF", quality=q)
    return dest


def fix_aspect(image, aspect):
    """mpv's "window" screenshot (with the upscalers) comes out at the window's shape; put the video's back."""
    from PIL import Image
    if not aspect:
        return image
    w = round(image.height * aspect)
    if abs(w - image.width) <= 2:
        return image
    return image.resize((w, image.height), Image.LANCZOS)


# ---------------------------------------------------------------- clips

_enc_cache = {}


def _run(args, cwd=None, timeout=3600):
    r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=cwd,
                       timeout=timeout, creationflags=NO_WINDOW)
    if r.returncode != 0:
        tail = [x for x in (r.stderr or "").strip().splitlines() if x.strip()]
        raise OSError(tail[-1] if tail else f"FFmpeg stopped (exit {r.returncode})")
    return r


_placebo = {}


def has_placebo():
    """This FFmpeg can run mpv's shaders (libplacebo: the full build, not essentials)."""
    try:
        key = os.path.getmtime(subs.FFMPEG)
    except OSError:
        return False
    if key not in _placebo:
        try:
            r = _run([str(subs.FFMPEG), "-hide_banner", "-filters"], timeout=30)
            _placebo[key] = "libplacebo" in (r.stdout or "")
        except (OSError, subprocess.SubprocessError):
            _placebo[key] = False
    return _placebo[key]


def gpu_ok(codec):
    """The graphics card can encode this (checked once with a tiny test encode)."""
    if codec not in _enc_cache:
        try:
            _run([str(subs.FFMPEG), "-v", "error", "-f", "lavfi", "-i", "color=black:s=256x144:d=0.2", "-c:v",
                  f"{codec}_nvenc", "-f", "null", "-"], timeout=30)
            _enc_cache[codec] = True
        except (OSError, subprocess.SubprocessError):
            _enc_cache[codec] = False
    return _enc_cache[codec]


def video_args(fmt, settings, bitrate=None):
    """Encoder options for a clip format (bitrate = kbit/s when it has to fit a size)."""
    q = QUALITY.get(opt(settings, "clip_quality"), QUALITY["high"])
    gpu = bool(opt(settings, "clip_gpu"))
    if fmt in ("h264", "h265"):
        name = "h264" if fmt == "h264" else "hevc"
        if gpu and gpu_ok(name):
            args = ["-c:v", f"{name}_nvenc", "-preset", "p6", "-tune", "hq"]
            args += (["-rc", "vbr", "-b:v", f"{bitrate}k", "-maxrate", f"{int(bitrate * 1.25)}k",
                      "-bufsize", f"{bitrate * 2}k"] if bitrate else ["-rc", "vbr", "-cq", str(q[0] + 1), "-b:v", "0"])
        else:
            args = ["-c:v", "libx264" if fmt == "h264" else "libx265", "-preset", "medium"]
            args += (["-b:v", f"{bitrate}k", "-maxrate", f"{int(bitrate * 1.25)}k", "-bufsize", f"{bitrate * 2}k"]
                     if bitrate else ["-crf", str(q[0] + (0 if fmt == "h264" else 3))])
        if fmt == "h265":
            args += ["-tag:v", "hvc1"]   # so Apple / Discord previews accept it
        return args + ["-pix_fmt", "yuv420p", "-movflags", "+faststart"]
    if fmt == "av1":
        return ["-c:v", "libaom-av1", "-cpu-used", "6", "-row-mt", "1", "-tiles", "2x2"] + \
               (["-b:v", f"{bitrate}k"] if bitrate else ["-crf", str(q[2]), "-b:v", "0"]) + \
               ["-pix_fmt", "yuv420p", "-movflags", "+faststart"]
    if fmt == "webm":
        return ["-c:v", "libvpx-vp9", "-row-mt", "1", "-deadline", "good", "-cpu-used", "3"] + \
               (["-b:v", f"{bitrate}k"] if bitrate else ["-crf", str(q[1]), "-b:v", "0"]) + ["-pix_fmt", "yuv420p"]
    if fmt == "webp":
        return ["-c:v", "libwebp_anim", "-loop", "0", "-q:v", "72", "-compression_level", "4"]
    return []


def _ff_path(p):
    """A path inside an FFmpeg filter (relative names with forward slashes avoid Windows' drive colon)."""
    return str(p).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def prepare_subs(src, track, start, end, style, work):
    """The shown subtitle track, ready to burn in: ("ass", file name, fonts dir) for text, ("pgs", stream index)
    for pictures, or None. Text: restyled (your Style), moved so the clip starts at 0, fonts dumped next to it."""
    if not track:
        return None
    codec = track.get("codec") or ""
    if codec in subs.IMAGE_SUBS or codec in ("hdmv_pgs_subtitle", "pgs"):
        return ("pgs", track["index"])
    ass = work / "sub.ass"
    if track.get("external"):
        ext = Path(track["external"]).suffix.lower()
        raw = Path(track["external"]).read_text(encoding="utf-8-sig", errors="replace")
        if ext == ".srt":
            tmp = work / "ext.srt"
            tmp.write_text(raw, encoding="utf-8")
            _run([str(subs.FFMPEG), "-v", "error", "-y", "-i", str(tmp), str(ass)])
            raw = ass.read_text(encoding="utf-8-sig", errors="replace")
    else:
        subs.extract_track(src, track["index"], ass)   # SubRip etc. come out as ASS
        raw = ass.read_text(encoding="utf-8-sig", errors="replace")
    if style:
        raw = subs.restyle(raw, subs.full_style(style))
    raw = subs.shift_text(raw, -start, "ass")
    ass.write_text(raw, encoding="utf-8")
    fonts = work / "fonts"
    fonts.mkdir(exist_ok=True)
    try:   # the release's own fonts (signs and styled dialogue need them)
        subprocess.run([str(subs.FFMPEG), "-v", "quiet", "-y", "-dump_attachment:t", "", "-i", str(src)],
                       cwd=fonts, capture_output=True, timeout=120, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        pass
    return ("ass", "sub.ass", "fonts")


def clip_args(src, start, end, fmt, settings, sub, audio_index, dest, bitrate=None, width=None, passlog=None,
              enh=None):
    """The FFmpeg command for one clip. enh = {"w", "h", "shaders": file name or None, "deband", "cas"}."""
    dur = max(0.1, end - start)
    args = [str(subs.FFMPEG), "-hide_banner", "-v", "error", "-y", "-progress", "pipe:1", "-nostats"]
    if enh:
        args += ["-init_hw_device", "vulkan=vk", "-filter_hw_device", "vk"]
    args += ["-ss", f"{start:.3f}", "-i", str(src), "-t", f"{dur:.3f}"]
    if fmt == "copy":
        args += ["-map", "0:v:0", "-map", "0:a?", "-map", "0:s?", "-map", "0:t?", "-c", "copy",
                 "-avoid_negative_ts", "make_zero", str(dest)]
        return args
    gif_like = fmt in ("gif", "webp")
    h = SIZES.get(opt(settings, "clip_size"))
    if gif_like:
        scale = f"scale={width or int(opt(settings, 'gif_width'))}:-2:flags=lanczos"
        pre = f"fps={int(opt(settings, 'gif_fps'))},"
    else:
        scale = f"scale=-2:'min({h},ih)':flags=lanczos" if h else "null"
        pre = ""
    if enh:   # the player's picture: shaders / deband / sharpen on the graphics card, then the subtitles
        placebo = ("format=yuv420p10le" + (",cas=strength=0.5" if enh.get("cas") else "") +
                   f",hwupload,libplacebo=w={enh['w']}:h={enh['h']}:upscaler=ewa_lanczos"
                   + (f":custom_shader_path={enh['shaders']}" if enh.get("shaders") else "")
                   + (":deband=1" if enh.get("deband") else "") + ":format=yuv420p,hwdownload,format=yuv420p")
        if sub and sub[0] == "pgs":
            chain = f"[0:v][0:{sub[1]}]overlay=eof_action=pass,{placebo}"
        elif sub:
            chain = f"[0:v]{placebo},subtitles={sub[1]}:fontsdir={sub[2]}"
        else:
            chain = f"[0:v]{placebo}"
    elif sub and sub[0] == "pgs":
        chain = f"[0:v][0:{sub[1]}]overlay=eof_action=pass,{pre}{scale}"
    elif sub:
        chain = f"[0:v]subtitles={sub[1]}:fontsdir={sub[2]},{pre}{scale}"
    else:
        chain = f"[0:v]{pre}{scale}"
    if fmt == "gif":
        chain += ",split[a][b];[a]palettegen=max_colors=256:stats_mode=diff[p];" \
                 "[b][p]paletteuse=dither=sierra2_4a:diff_mode=rectangle[v]"
    else:
        chain += "[v]"
    args += ["-filter_complex", chain, "-map", "[v]"]
    has_audio = CLIP_FORMATS[fmt][2] and opt(settings, "clip_audio") and audio_index is not None
    if has_audio:
        args += ["-map", f"0:{audio_index}"]
        args += ["-c:a", "libopus", "-b:a", "128k"] if fmt == "webm" else ["-c:a", "aac", "-b:a", "160k"]
    else:
        args += ["-an"]
    args += ["-sn", "-dn", "-map_metadata", "-1", "-map_chapters", "-1"]
    args += video_args(fmt, settings, bitrate)
    if fmt == "gif":
        args += ["-loop", "0"]
    return args + [dest.name if passlog else str(dest)]


def _render(args, dur, progress, cancelled, cwd):
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                         errors="replace", cwd=cwd, creationflags=NO_WINDOW)
    err = []
    t = threading.Thread(target=lambda: err.extend(p.stderr.read().splitlines()), daemon=True)
    t.start()
    for line in p.stdout:
        if cancelled and cancelled():
            p.kill()
            break
        if line.startswith("out_time_us=") and progress:
            try:
                progress(min(1.0, int(line.split("=")[1]) / 1e6 / dur))
            except ValueError:
                pass
    p.wait()
    t.join(5)
    if cancelled and cancelled():
        raise InterruptedError("cancelled")
    if p.returncode != 0:
        tail = [x for x in err if x.strip()]
        raise OSError(tail[-1] if tail else f"FFmpeg stopped (exit {p.returncode})")


def enhance_size(settings, src_size, upscaled=True):
    """(w, h) for an enhanced clip: the chosen height (never below the video's) at the video's shape."""
    sw, sh = src_size or (1920, 1080)
    h = max(int(opt(settings, "clip_enh_size")), int(sh)) if upscaled else int(sh)
    return int(round(h * sw / sh / 2) * 2), h


def render_clip(src, start, end, dest, settings, sub_track=None, audio_index=None, style=None,
                progress=None, cancelled=None, enhance=None, src_size=None, notes=None):
    """Make the clip. Returns its path. sub_track = the shown subtitle track ({index, codec} or {external}).
    enhance = the player's picture ({"shaders": [files], "deband", "cas"}) or None; src_size = video (w, h).
    notes gets a line when the picture enhancement couldn't be used."""
    fmt = opt(settings, "clip_format")
    if fmt not in CLIP_FORMATS:
        fmt = "h264"
    dur = max(0.1, end - start)
    limit = float(opt(settings, "clip_limit") or 0) * 1024 * 1024
    dest = Path(dest)
    with tempfile.TemporaryDirectory(prefix="kura_clip_") as td:
        work = Path(td)
        sub = None
        if fmt != "copy" and opt(settings, "clip_subs"):
            sub = prepare_subs(src, sub_track, start, end, style, work)
        out = work / ("out" + dest.suffix)
        bitrate, width = None, None
        enh = None
        if enhance and fmt in VIDEO_FORMATS and (enhance.get("shaders") or enhance.get("deband") or enhance.get("cas")):
            if not has_placebo():
                if notes is not None:
                    notes.append("Without picture enhancement: it needs the full FFmpeg (Settings → Captures)")
            else:
                w, h = enhance_size(settings, src_size, bool(enhance.get("shaders")))
                enh = {"w": w, "h": h, "deband": enhance.get("deband"), "cas": enhance.get("cas")}
                if enhance.get("shaders"):
                    with open(work / "shaders.glsl", "w", encoding="utf-8") as f:
                        for path in enhance["shaders"]:
                            f.write(Path(path).read_text(encoding="utf-8", errors="replace") + "\n")
                    enh["shaders"] = "shaders.glsl"
        if limit and fmt not in ("gif", "webp", "copy"):
            audio_k = 160 if (opt(settings, "clip_audio") and audio_index is not None) else 0
            bitrate = max(150, int(limit * 8 / 1000 / dur * 0.94) - audio_k)
        for attempt in range(4):
            try:
                _render(clip_args(src, start, end, fmt, settings, sub, audio_index, out, bitrate, width,
                                  passlog=True, enh=enh), dur, progress, cancelled, work)
            except OSError as e:
                if not enh:
                    raise
                enh = None   # the graphics card path failed (Vulkan): the plain clip instead
                if notes is not None:
                    notes.append(f"Without picture enhancement ({str(e)[:80]})")
                _render(clip_args(src, start, end, fmt, settings, sub, audio_index, out, bitrate, width,
                                  passlog=True), dur, progress, cancelled, work)
            size = out.stat().st_size
            if not limit or size <= limit or fmt == "copy":
                break
            if fmt in ("gif", "webp"):   # no bitrate to set: smaller pictures
                width = max(160, int((width or int(opt(settings, "gif_width"))) * (limit / size) ** 0.5 * 0.92))
            else:
                bitrate = max(100, int(bitrate * limit / size * 0.93))
            if progress:
                progress(0.0)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(out), str(dest))
    return dest


def estimate(settings, dur, height=1080, source_rate=None, enhanced=False):
    """A rough size in MB before rendering (shown while you pick the range). source_rate = the episode's MB/s
    (fast copy keeps it). Measured on a 1080p SubsPlease episode with NVENC (Oct 2026)."""
    fmt = opt(settings, "clip_format")
    limit = float(opt(settings, "clip_limit") or 0)
    h = SIZES.get(opt(settings, "clip_size")) or height
    q = opt(settings, "clip_quality")
    per_sec = {"h264": 0.6, "h265": 0.45, "av1": 0.1, "webm": 0.18, "copy": source_rate or 1.0}.get(fmt)
    if fmt in ("gif", "webp"):
        w = int(opt(settings, "gif_width"))
        mb = dur * int(opt(settings, "gif_fps")) * (w * w * 9 / 16) * (0.24 if fmt == "gif" else 0.05) / 1e6
    else:
        if fmt == "copy":
            return dur * per_sec
        if enhanced and fmt in VIDEO_FORMATS:
            height = max(int(opt(settings, "clip_enh_size")), height)
            h = height
        mb = dur * per_sec * (min(h, height) / 1080) ** 1.6 * {"high": 1, "medium": 0.6, "small": 0.35}.get(q, 1)
    return min(mb, limit) if limit and fmt not in ("gif", "webp") else mb


# ---------------------------------------------------------------- contact sheet

def contact_sheet(src, dest, title="", subtitle="", cols=4, rows=6, width=1920, progress=None, cancelled=None):
    """Frames spread over the whole episode in a grid, with the time under each, in one image."""
    from PIL import Image, ImageDraw, ImageFont
    streams, duration = subs.probe(src)
    if not duration:
        raise OSError("couldn't read the episode's length")
    n = cols * rows
    gap, pad, head = 12, 24, 96
    cell_w = (width - pad * 2 - gap * (cols - 1)) // cols
    frames = []
    with tempfile.TemporaryDirectory(prefix="kura_sheet_") as td:
        for i in range(n):
            if cancelled and cancelled():
                raise InterruptedError("cancelled")
            t = duration * (i + 0.5) / n
            f = Path(td) / f"f{i:03d}.jpg"
            _run([str(subs.FFMPEG), "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(src), "-frames:v", "1",
                  "-vf", f"scale={cell_w}:-2:flags=lanczos", "-q:v", "3", str(f)], timeout=120)
            frames.append((t, Image.open(f).convert("RGB")))
            if progress:
                progress((i + 1) / n)
        cell_h = frames[0][1].height
        label_h = 30
        height = head + pad + rows * (cell_h + label_h) + (rows - 1) * gap + pad
        sheet = Image.new("RGB", (width, height), (15, 17, 21))
        draw = ImageDraw.Draw(sheet)

        def font(size, bold=False):
            for name in (("segoeuib.ttf" if bold else "segoeui.ttf"), "arial.ttf"):
                try:
                    return ImageFont.truetype(os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", name), size)
                except OSError:
                    continue
            return ImageFont.load_default()
        draw.text((pad, 22), title or Path(src).stem, fill=(231, 233, 239), font=font(34, True))
        info = subtitle or f"{len(frames)} frames  ·  {stamp(duration)}"
        draw.text((pad, 64), info, fill=(138, 146, 166), font=font(20))
        small = font(18)
        for i, (t, img) in enumerate(frames):
            r, c = divmod(i, cols)
            x = pad + c * (cell_w + gap)
            y = head + pad + r * (cell_h + label_h + gap)
            sheet.paste(img, (x, y))
            label = f"{int(t) // 3600}:{int(t) // 60 % 60:02d}:{int(t) % 60:02d}" if t >= 3600 else                 f"{int(t) // 60}:{int(t) % 60:02d}"
            draw.text((x + 2, y + cell_h + 5), label, fill=(138, 146, 166), font=small)
        sheet.save(dest, quality=92) if str(dest).lower().endswith(".jpg") else sheet.save(dest)
    return dest
