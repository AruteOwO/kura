"""
Picture enhancement for Kura's player (no Qt in here): anime upscaling shaders run by mpv on the GPU,
plus debanding and sharpening. Shaders are downloaded once from their own projects into tools/shaders:
  - Anime4K (bloc97, MIT): restore + upscale CNNs, in the modes its authors recommend;
  - FSRCNNX (igv, GPL-3.0): a faithful upscaler (sharper, keeps the look);
  - ArtCNN (Artoriuz, MIT): the newest, sharpest; compute shaders (OpenGL 4.3+, Kura runs 4.6).
A picture setting is {"mode", "quality" ("hq"/"fast"), "deband", "sharpen"}; `mpv_options` turns it into
mpv's glsl-shaders / deband / extra video filter. Nvidia VSR isn't offered: mpv only does it with Direct3D 11
rendering, and Kura's player has to render with OpenGL (its controls are drawn over the video).
"""

import os
import urllib.request
import zipfile

import anime_organizer as org

SHADER_DIR = org.SCRIPT_DIR / "tools" / "shaders"
CACHE_DIR = org.SCRIPT_DIR / "cover_cache" / "shader_cache"   # compiled shaders: switching is quick after once

ANIME4K_ZIP = "https://github.com/bloc97/Anime4K/releases/download/v4.0.1/Anime4K_v4.0.zip"
FILES = {
    "FSRCNNX_x2_16-0-4-1.glsl": "https://github.com/igv/FSRCNN-TensorFlow/releases/download/1.1/FSRCNNX_x2_16-0-4-1.glsl",
    "FSRCNNX_x2_8-0-4-1.glsl": "https://github.com/igv/FSRCNN-TensorFlow/releases/download/1.1/FSRCNNX_x2_8-0-4-1.glsl",
    "ArtCNN_C4F32.glsl": "https://raw.githubusercontent.com/Artoriuz/ArtCNN/main/GLSL/ArtCNN_C4F32.glsl",
    "ArtCNN_C4F16.glsl": "https://raw.githubusercontent.com/Artoriuz/ArtCNN/main/GLSL/ArtCNN_C4F16.glsl",
}

DEFAULT = {"mode": "off", "quality": "hq", "deband": False, "sharpen": False}

# (key, name, what it's for); the Anime4K modes are its authors' recommended chains
MODES = [
    ("off", "Off", "The picture as it is"),
    ("a4k_a", "Anime4K · A", "Restore and upscale: most 1080p anime"),
    ("a4k_b", "Anime4K · B", "Softer restore: blurry or older sources"),
    ("a4k_c", "Anime4K · C", "Upscale and denoise: noisy, grainy sources"),
    ("a4k_aa", "Anime4K · A+A", "A, restored twice: strongest lines"),
    ("a4k_bb", "Anime4K · B+B", "B, restored twice"),
    ("a4k_ca", "Anime4K · C+A", "Denoise, then restore"),
    ("fsrcnnx", "FSRCNNX", "Faithful: sharper without changing the look"),
    ("artcnn", "ArtCNN", "Newest and sharpest (heaviest)"),
]
MODE_NAMES = {k: n for k, n, _ in MODES}


def _a4k(mode, hq):
    big, small = ("VL", "M") if hq else ("M", "S")
    restore = {"a": f"Restore_CNN_{big}", "b": f"Restore_CNN_Soft_{big}"}
    pre = ["AutoDownscalePre_x2", "AutoDownscalePre_x4"]
    chains = {
        "a": ["Clamp_Highlights", restore["a"], f"Upscale_CNN_x2_{big}", *pre, f"Upscale_CNN_x2_{small}"],
        "b": ["Clamp_Highlights", restore["b"], f"Upscale_CNN_x2_{big}", *pre, f"Upscale_CNN_x2_{small}"],
        "c": ["Clamp_Highlights", f"Upscale_Denoise_CNN_x2_{big}", *pre, f"Upscale_CNN_x2_{small}"],
        "aa": ["Clamp_Highlights", restore["a"], f"Upscale_CNN_x2_{big}", f"Restore_CNN_{small}", *pre,
               f"Upscale_CNN_x2_{small}"],
        "bb": ["Clamp_Highlights", restore["b"], f"Upscale_CNN_x2_{big}", *pre, f"Restore_CNN_Soft_{small}",
               f"Upscale_CNN_x2_{small}"],
        "ca": ["Clamp_Highlights", f"Upscale_Denoise_CNN_x2_{big}", *pre, f"Restore_CNN_{small}",
               f"Upscale_CNN_x2_{small}"],
    }
    return [f"Anime4K_{n}.glsl" for n in chains[mode]]


def shader_files(pic):
    """The shader files a picture setting uses, in order."""
    mode, hq = pic.get("mode", "off"), pic.get("quality", "hq") != "fast"
    if mode.startswith("a4k_"):
        return _a4k(mode[4:], hq)
    if mode == "fsrcnnx":
        return ["FSRCNNX_x2_16-0-4-1.glsl" if hq else "FSRCNNX_x2_8-0-4-1.glsl"]
    if mode == "artcnn":
        return ["ArtCNN_C4F32.glsl" if hq else "ArtCNN_C4F16.glsl"]
    return []


def full(pic):
    return {**DEFAULT, **(pic or {})}


def describe(pic):
    """'Anime4K · A (HQ) + deband' for labels."""
    p = full(pic)
    parts = [MODE_NAMES.get(p["mode"], "Off") + ("" if p["mode"] == "off" else
                                                 " (HQ)" if p["quality"] != "fast" else " (Fast)")]
    parts += [x for x, on in (("deband", p["deband"]), ("sharpen", p["sharpen"])) if on]
    return " + ".join(parts)


def installed():
    return all((SHADER_DIR / f).exists() for f in FILES) and (SHADER_DIR / "Anime4K_Clamp_Highlights.glsl").exists()


def install(progress=None):
    """Download every shader once (about 3 MB)."""
    SHADER_DIR.mkdir(parents=True, exist_ok=True)
    jobs = [("anime4k.zip", ANIME4K_ZIP)] + list(FILES.items())
    for n, (name, url) in enumerate(jobs):
        if progress:
            progress(n / len(jobs))
        tmp = SHADER_DIR / (name + ".download")
        req = urllib.request.Request(url, headers={"User-Agent": org.USER_AGENT})
        with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as f:
            f.write(resp.read())
        if name == "anime4k.zip":
            with zipfile.ZipFile(tmp) as z:
                for member in z.namelist():
                    base = member.rsplit("/", 1)[-1]
                    if base.endswith(".glsl"):
                        (SHADER_DIR / base).write_bytes(z.read(member))
            tmp.unlink()
        else:
            if tmp.stat().st_size < 10_000:
                tmp.unlink()
                raise OSError(f"{name} looks broken (too small)")
            os.replace(tmp, SHADER_DIR / name)
    if progress:
        progress(1.0)
    return installed()


def mpv_options(pic):
    """{"glsl-shaders": [...], "deband": bool, "vf_extra": str} for mpv; shaders that aren't downloaded are
    left out (the picture stays as it is)."""
    p = full(pic)
    files = [SHADER_DIR / f for f in shader_files(p)]
    shaders = [str(f) for f in files] if files and all(f.exists() for f in files) else []
    # CAS (AMD's contrast adaptive sharpening) as an ffmpeg filter; runs before Kura's format filter
    return {"glsl-shaders": shaders, "deband": bool(p["deband"]),
            "vf_extra": "lavfi=[cas=strength=0.5]" if p["sharpen"] else ""}
