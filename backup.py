"""
Backups of Kura's own data (no Qt in here): one zip with your settings, resume positions, watch time, torrent
filters and history, Crunchyroll history and matches, AniList matches, sequel alerts and sign-ins (the .bin files
are DPAPI-encrypted: they only work for your Windows account). Caches that come back by themselves (covers,
AniList details, seasons, trailers...) and subtitle_backups (hundreds of MB of original subtitle tracks) aren't in it.

A restore never happens while Kura runs (it would save its own copies over the restored ones on quit): Kura writes
`restore_pending.json`, restarts, and `apply_pending` puts the files back before anything is loaded, after making a
"Before restore" backup of what was there.
"""

import json
import os
import time
import zipfile
from pathlib import Path

# (file, what it is); "reference" files are saved but never restored
FILES = [
    ("gui_settings.json", "Settings"),
    ("playback_state.json", "Resume positions"),
    ("kura_watch.json", "Time watched in Kura"),
    ("kura_watch_adult.json", "Time watched in Kura (18+)"),
    ("torrent_filters.json", "Torrent filters"),
    ("torrent_filters.backup.json", "Previous torrent filters"),
    ("torrent_history.json", "Torrent history"),
    ("crunchyroll_history.json", "Crunchyroll history"),
    ("crunchyroll_map.json", "Crunchyroll matches"),
    ("anilist_cache.json", "AniList matches for your folders"),
    ("anilist_list.json", "Your AniList list (offline copy)"),
    ("alerts_cache.json", "Sequel alerts already seen"),
    ("anilist_token.bin", "AniList sign-in"),
    ("crunchyroll_token.bin", "Crunchyroll sign-in"),
]
REFERENCE = [("anime_organizer.py", "Organizer script with your overrides (a copy to look at, not restored)")]
NAMES = dict(FILES + REFERENCE)
PENDING = "restore_pending.json"
PREFIX = "Kura backup "


def default_dir(data_dir):
    return Path(data_dir) / "backups"


def _read(path):
    """A file's bytes; a .json caught half-written is read again."""
    for attempt in range(3):
        data = path.read_bytes()
        if path.suffix != ".json":
            return data
        try:
            json.loads(data.decode("utf-8"))
            return data
        except ValueError:
            time.sleep(0.4)
    return data


def make(data_dir, dest_dir, reason="manual", now=None):
    """Zip Kura's data into dest_dir. Returns the zip's path."""
    data_dir, dest_dir = Path(data_dir), Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    now = time.time() if now is None else now
    stamp = time.strftime("%Y-%m-%d %H%M%S", time.localtime(now))
    out = dest_dir / f"{PREFIX}{stamp}.zip"
    n = 2
    while out.exists():   # two in the same second (a restore right after a backup)
        out = dest_dir / f"{PREFIX}{stamp} ({n}).zip"
        n += 1
    tmp = out.with_suffix(".zip.part")
    manifest = {"kura_backup": 1, "made": now, "reason": reason, "files": {}}
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for name, _ in FILES + REFERENCE:
            p = data_dir / name
            if p.is_file():
                data = _read(p)
                z.writestr(name, data)
                manifest["files"][name] = len(data)
        z.writestr("manifest.json", json.dumps(manifest, indent=1))
    os.replace(tmp, out)
    return out


def read_manifest(path):
    try:
        with zipfile.ZipFile(path) as z:
            m = json.loads(z.read("manifest.json").decode("utf-8"))
        return m if m.get("kura_backup") else None
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return None


def list_backups(dest_dir):
    """[(path, manifest)], newest first."""
    out = []
    d = Path(dest_dir)
    if d.is_dir():
        for p in d.glob(f"{PREFIX}*.zip"):
            m = read_manifest(p)
            if m:
                out.append((p, m))
    return sorted(out, key=lambda pm: -pm[1].get("made", 0))


def prune(dest_dir, keep):
    """Drop the oldest automatic backups past `keep` (ones you made yourself stay). Returns how many went."""
    autos = [p for p, m in list_backups(dest_dir) if m.get("reason") == "auto"]
    gone = 0
    for p in autos[max(1, keep):]:
        try:
            p.unlink()
            gone += 1
        except OSError:
            pass
    return gone


def due(last, days=7, now=None):
    return ((time.time() if now is None else now) - (last or 0)) >= days * 86400


def restorable(manifest):
    return [n for n in manifest.get("files", {}) if n in dict(FILES)]


def request_restore(data_dir, zip_path, names):
    """Ask for a restore at Kura's next start."""
    (Path(data_dir) / PENDING).write_text(json.dumps({"zip": str(zip_path), "names": list(names)}), encoding="utf-8")


def apply_pending(data_dir, dest_dir):
    """At startup: put back the files a restore asked for. Returns (restored names, error or None) or None."""
    data_dir = Path(data_dir)
    marker = data_dir / PENDING
    if not marker.is_file():
        return None
    try:
        job = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        job = {}
    try:
        marker.unlink()
    except OSError:
        pass
    src, names = Path(job.get("zip") or ""), [n for n in job.get("names") or [] if n in dict(FILES)]
    if not src.is_file() or not names:
        return [], "the backup file wasn't found"
    try:
        make(data_dir, dest_dir, reason="before restore")
    except OSError as e:
        return [], f"couldn't back up the current files first ({e})"
    done = []
    try:
        with zipfile.ZipFile(src) as z:
            for n in names:
                tmp = data_dir / (n + ".restoring")
                tmp.write_bytes(z.read(n))
                os.replace(tmp, data_dir / n)
                done.append(n)
    except (OSError, KeyError, zipfile.BadZipFile) as e:
        return done, str(e)
    return done, None
