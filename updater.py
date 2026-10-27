"""Kura's updates from GitHub Releases.

Each release has a manifest.json and two zips: the app (Kura.exe, kura_runtime/base_library.zip, fonts, the
organizer script qBittorrent runs) and the runtime (the rest of kura_runtime: Qt, Python, libmpv). The runtime only
changes when the libraries do, so the manifest names it by an id (a hash of its files) and an update downloads it
only when yours is different. Your data files (settings, lists, caches) are never part of a release.

Applying: Kura downloads and checks everything into update/, then quits; a small PowerShell script waits for it to
close, swaps the files in (the old ones go to *.old-<time> and are removed after), and starts Kura again.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

USER_AGENT = "Kura-updater"
RUNTIME = "kura_runtime"
RUNTIME_ID_FILE = "runtime_id.txt"      # inside kura_runtime: which runtime this is
APP_ONLY = {"base_library.zip"}         # rebuilt every time (it holds compiled files): ships with the app zip
INSTALLED = "kura_installed.json"       # the manifest of the release installed here (scripts it put down)
SCRIPTS = ["anime_organizer.py"]        # qBittorrent's run-on-finish hook runs this one as a script


def sha256(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                return h.hexdigest()
            h.update(b)


def runtime_id(runtime_dir):
    """A short id for a kura_runtime folder: its files (not base_library.zip) and their contents."""
    runtime_dir = Path(runtime_dir)
    h = hashlib.sha256()
    for f in sorted(p for p in runtime_dir.rglob("*") if p.is_file()):
        rel = f.relative_to(runtime_dir).as_posix()
        if rel in APP_ONLY or rel == RUNTIME_ID_FILE:
            continue
        h.update(rel.encode() + b"\0" + sha256(f).encode() + b"\n")
    return h.hexdigest()[:16]


def installed_runtime_id(root):
    try:
        return (Path(root) / RUNTIME / RUNTIME_ID_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def version_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3]) or (0,)


def is_newer(latest, current):
    return version_tuple(latest) > version_tuple(current)


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def latest_release(repo):
    """The newest release of `owner/name`: {"version", "notes", "url", "manifest"}; None if it has no manifest."""
    rel = json.loads(_get(f"https://api.github.com/repos/{repo}/releases/latest"))
    asset = next((a for a in rel.get("assets") or [] if a.get("name") == "manifest.json"), None)
    if asset is None:
        return None
    manifest = json.loads(_get(asset["browser_download_url"]))
    return {"version": manifest.get("version") or rel.get("tag_name", ""), "notes": rel.get("body") or "",
            "url": rel.get("html_url", ""), "manifest": manifest}


def download_size(manifest, root):
    """Bytes an update needs: the app, plus the runtime when yours is different."""
    n = manifest["app"]["size"]
    if manifest["runtime"]["id"] != installed_runtime_id(root):
        n += manifest["runtime"]["size"]
    return n


def _download(item, dest, progress, done_before, total, cancelled):
    req = urllib.request.Request(item["url"], headers={"User-Agent": USER_AGENT})
    h = hashlib.sha256()
    got = 0
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        while True:
            if cancelled():
                raise InterruptedError("cancelled")
            b = r.read(1 << 20)
            if not b:
                break
            f.write(b)
            h.update(b)
            got += len(b)
            progress(done_before + got, total)
    if h.hexdigest() != item["sha256"]:
        raise ValueError(f"{Path(dest).name} didn't download correctly (checksum mismatch)")
    return got


def prepare(manifest, root, progress=lambda done, total: None, cancelled=lambda: False):
    """Download and check the update into root/update, unpacked as update/new. Returns the staging folder."""
    root = Path(root)
    stage = root / "update"
    shutil.rmtree(stage, ignore_errors=True)
    (stage / "new").mkdir(parents=True)
    total = download_size(manifest, root)
    done = _download(manifest["app"], stage / "app.zip", progress, 0, total, cancelled)
    with zipfile.ZipFile(stage / "app.zip") as z:
        z.extractall(stage / "new")
    if manifest["runtime"]["id"] != installed_runtime_id(root):
        _download(manifest["runtime"], stage / "runtime.zip", progress, done, total, cancelled)
        with zipfile.ZipFile(stage / "runtime.zip") as z:
            z.extractall(stage / "new" / RUNTIME)
        (stage / "new" / RUNTIME / RUNTIME_ID_FILE).write_text(manifest["runtime"]["id"], encoding="utf-8")
    (stage / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    for name in ("app.zip", "runtime.zip"):
        (stage / name).unlink(missing_ok=True)
    return stage


def scripts_to_replace(manifest, root):
    """The organizer script(s) to update: only ones you haven't changed since the last update put them there
    (a copy you edit, like the one in your own Kura folder, is never overwritten)."""
    root = Path(root)
    try:
        before = json.loads((root / INSTALLED).read_text(encoding="utf-8")).get("scripts", {})
    except (OSError, ValueError):
        before = {}
    out = []
    for name in manifest.get("scripts", {}):
        f = root / name
        if not f.exists() or (name in before and sha256(f) == before[name]):
            out.append(name)
    return out


def apply_script(root, stage, pid, exe, scripts, new_runtime):
    """The PowerShell script that swaps the files once Kura (pid) has closed, then starts it again."""
    q = lambda p: "'" + str(p).replace("'", "''") + "'"
    root, stage = Path(root), Path(stage)
    new = stage / "new"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    lines = [
        "$ErrorActionPreference = 'Stop'",
        f"$log = {q(stage / 'apply.log')}",
        "function Say($t) { Add-Content -Path $log -Value ((Get-Date -Format s) + '  ' + $t) }",
        f"try {{ Wait-Process -Id {int(pid)} -Timeout 60 -ErrorAction SilentlyContinue }} catch {{}}",
        "Start-Sleep -Milliseconds 800",
        "try {",
        f"  Move-Item -LiteralPath {q(root / 'Kura.exe')} -Destination {q(root / f'Kura.old-{stamp}.exe')}",
        f"  Move-Item -LiteralPath {q(new / 'Kura.exe')} -Destination {q(root / 'Kura.exe')}",
        "  Say 'Kura.exe replaced'",
    ]
    if new_runtime:
        lines += [
            f"  Move-Item -LiteralPath {q(root / RUNTIME)} -Destination {q(root / f'{RUNTIME}.old-{stamp}')}",
            f"  Move-Item -LiteralPath {q(new / RUNTIME)} -Destination {q(root / RUNTIME)}",
            "  Say 'runtime replaced'",
        ]
    else:   # same runtime: only the file that changes with every build
        for name in APP_ONLY:
            lines.append(f"  Copy-Item -LiteralPath {q(new / RUNTIME / name)} -Destination {q(root / RUNTIME / name)} -Force")
    lines.append(f"  New-Item -ItemType Directory -Force -Path {q(root / 'fonts')} | Out-Null")
    lines.append(f"  Copy-Item -Path {q(new / 'fonts' / '*')} -Destination {q(root / 'fonts')} -Force")
    for name in scripts:
        lines.append(f"  Copy-Item -LiteralPath {q(new / name)} -Destination {q(root / name)} -Force")
    lines += [
        f"  Copy-Item -LiteralPath {q(stage / 'manifest.json')} -Destination {q(root / INSTALLED)} -Force",
        "  Say 'update installed'",
        "} catch {",
        "  Say ('FAILED: ' + $_)",
        # put the old exe back if the new one didn't land, so Kura still starts
        f"  if (-not (Test-Path -LiteralPath {q(root / 'Kura.exe')}) -and (Test-Path -LiteralPath {q(root / f'Kura.old-{stamp}.exe')})) {{",
        f"    Move-Item -LiteralPath {q(root / f'Kura.old-{stamp}.exe')} -Destination {q(root / 'Kura.exe')} }}",
        f"  if (-not (Test-Path -LiteralPath {q(root / RUNTIME)}) -and (Test-Path -LiteralPath {q(root / f'{RUNTIME}.old-{stamp}')})) {{",
        f"    Move-Item -LiteralPath {q(root / f'{RUNTIME}.old-{stamp}')} -Destination {q(root / RUNTIME)} }}",
        "}",
        f"Start-Process -FilePath {q(root / 'Kura.exe')} -ArgumentList '--updated' -WorkingDirectory {q(root)}",
        "Start-Sleep -Seconds 5",
        # the old program files (not your data): gone once the new Kura is running
        f"Remove-Item -LiteralPath {q(root / f'Kura.old-{stamp}.exe')} -Force -ErrorAction SilentlyContinue",
        f"Remove-Item -LiteralPath {q(root / f'{RUNTIME}.old-{stamp}')} -Recurse -Force -ErrorAction SilentlyContinue",
        f"Remove-Item -LiteralPath {q(new)} -Recurse -Force -ErrorAction SilentlyContinue",
    ]
    path = stage / "apply_update.ps1"
    path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8-sig")
    return path


def launch_apply(script):
    """Run the swap script hidden, detached from Kura (which then quits)."""
    flags = (0x00000200 | 0x08000000) if os.name == "nt" else 0   # its own process group, no console window
    subprocess.Popen(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
                      "-File", str(script)], creationflags=flags, close_fds=True)


if __name__ == "__main__":   # python updater.py owner/repo: what the newest release is
    print(json.dumps(latest_release(sys.argv[1]), indent=1)[:2000])
