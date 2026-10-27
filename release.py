"""
Publish a new Kura version on GitHub (the in-app updater picks it up).

    python release.py 1.1.0 --notes "What's new, in a few lines"
    python release.py 1.1.0 --notes-file changes.md
    python release.py 1.1.0 --dry-run          # build and package, publish nothing

Needs git and the GitHub CLI (gh, logged in). Close Kura first (it builds Kura.exe). Steps: set APP_VERSION,
build, zip the app (and the runtime, only when it changed since the last release), write manifest.json, commit +
tag + push, create the release with those files.
"""
import argparse
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import updater

HERE = Path(__file__).resolve().parent
OUT = HERE / "release"


def run(*cmd, capture=False):
    r = subprocess.run(cmd, cwd=HERE, check=True, text=True, capture_output=capture)
    return r.stdout.strip() if capture else ""


def set_version(version):
    p = HERE / "kura.pyw"
    text = p.read_bytes().decode("utf-8")   # bytes: keep the file's line endings as they are
    new, n = re.subn(r'^APP_VERSION = "[^"]*"', f'APP_VERSION = "{version}"', text, count=1, flags=re.M)
    if not n:
        sys.exit("APP_VERSION not found in kura.pyw")
    p.write_bytes(new.encode("utf-8"))


def zip_files(target, files):
    """files: [(path on disk, name in the zip)]."""
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for src, name in files:
            z.write(src, name)
    return {"name": target.name, "sha256": updater.sha256(target), "size": target.stat().st_size}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("version")
    ap.add_argument("--notes", default="")
    ap.add_argument("--notes-file")
    ap.add_argument("--no-build", action="store_true", help="use the Kura.exe that's already here")
    ap.add_argument("--dry-run", action="store_true", help="build and package only")
    a = ap.parse_args()
    version = a.version.lstrip("v")
    tag = f"v{version}"
    notes = Path(a.notes_file).read_text(encoding="utf-8") if a.notes_file else a.notes
    repo = run("gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner", capture=True)
    if not a.dry_run and run("git", "status", "--porcelain", capture=True):
        sys.exit("Commit (or stash) your changes first: a release is made from what's committed.")

    set_version(version)
    if not a.no_build:
        run(sys.executable, str(HERE / "build_exe.py"))
    rt = HERE / updater.RUNTIME
    rid = (rt / updater.RUNTIME_ID_FILE).read_text(encoding="utf-8").strip()
    OUT.mkdir(exist_ok=True)
    base = f"https://github.com/{repo}/releases/download/{tag}/"

    app_files = [(HERE / "Kura.exe", "Kura.exe")]
    app_files += [(rt / n, f"{updater.RUNTIME}/{n}") for n in updater.APP_ONLY]
    app_files += [(f, f"fonts/{f.name}") for f in sorted((HERE / "fonts").iterdir()) if f.is_file()]
    app_files += [(HERE / n, n) for n in updater.SCRIPTS]
    app = zip_files(OUT / f"Kura-{version}.zip", app_files)
    app["url"] = base + app["name"]

    try:   # the runtime is uploaded only when it isn't the same as the last release's
        prev = (updater.latest_release(repo) or {}).get("manifest", {}).get("runtime")
    except Exception:
        prev = None
    upload = [OUT / app["name"]]
    if prev and prev.get("id") == rid:
        runtime = prev
        print(f"Runtime unchanged ({rid}): the release points at the last one's.")
    else:
        files = [(f, f.relative_to(rt).as_posix()) for f in sorted(rt.rglob("*")) if f.is_file()
                 and f.relative_to(rt).as_posix() not in updater.APP_ONLY | {updater.RUNTIME_ID_FILE}]
        print(f"Packing the runtime ({len(files)} files)...")
        runtime = zip_files(OUT / f"Kura-runtime-{rid}.zip", files)
        runtime.update(id=rid, url=base + runtime["name"])
        upload.append(OUT / runtime["name"])

    manifest = {"version": version, "app": app, "runtime": runtime,
                "scripts": {n: updater.sha256(HERE / n) for n in updater.SCRIPTS}}
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    upload.append(OUT / "manifest.json")
    print("Packaged:", ", ".join(f"{p.name} ({p.stat().st_size / 2 ** 20:.1f} MB)" for p in upload))
    if a.dry_run:
        print("Dry run: nothing published. (kura.pyw now says", version, "- undo with git checkout kura.pyw)")
        return

    run("git", "add", "kura.pyw")
    run("git", "commit", "-m", f"Kura {version}")
    run("git", "tag", tag)
    run("git", "push", "--follow-tags")
    run("gh", "release", "create", tag, *map(str, upload), "--title", f"Kura {version}",
        "--notes", notes or f"Kura {version}")
    print(f"Published {tag}: https://github.com/{repo}/releases/tag/{tag}")


if __name__ == "__main__":
    main()
