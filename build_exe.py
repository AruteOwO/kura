"""
Build Kura.exe with PyInstaller.

    pip install pyinstaller
    python build_exe.py

Puts Kura.exe and its kura_runtime folder next to this file, so the app uses the same
settings, cache and AniList files as the script version. Close Kura before building.
"""

import shutil
import subprocess
import sys
import tempfile
import time
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent


def make_icon(target):
    """Render Kura's icon (the same one the app draws) into a multi-size .ico."""
    from PIL import Image
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    gui = SourceFileLoader("kura_gui", str(HERE / "kura.pyw")).load_module()
    gui.ICON_FONT = gui.find_icon_font()
    png = target.with_suffix(".png")
    gui.app_icon().pixmap(256, 256).save(str(png))
    Image.open(png).save(target, sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
    png.unlink()
    del app


def prune(qt):
    """PyInstaller bundles all of Qt's QML modules (pulled in by QQuickWidget) and every
    translation. Kura uses no QML, and the trailer player only needs its English locale."""
    shutil.rmtree(qt / "qml", ignore_errors=True)
    tr = qt / "translations"
    for f in tr.glob("*.qm"):
        f.unlink()
    for f in (tr / "qtwebengine_locales").glob("*.pak"):
        if f.name != "en-US.pak":
            f.unlink()
    # Web developer tools for the player, and libraries only those QML modules used
    unused = ["resources/qtwebengine_devtools_resources*.pak", "Qt6Quick3D*.dll", "Qt6QuickControls2*.dll",
              "Qt6QuickTemplates2*.dll", "Qt6QuickDialogs2*.dll", "Qt6QuickParticles*.dll", "Qt6QuickTimeline*.dll",
              "Qt6QuickVectorImage*.dll", "Qt6QuickEffects*.dll", "Qt6Pdf*.dll", "Qt6Charts*.dll", "Qt6DataVisualization*.dll",
              "Qt6Graphs*.dll", "Qt6Location*.dll", "Qt6Multimedia*.dll", "Qt6Sensors*.dll", "Qt6VirtualKeyboard*.dll"]
    for pattern in unused:
        for f in qt.glob(pattern):
            f.unlink()
    # WebEngine's resources for debug builds of Qt (the release library never names them), and the QML
    # debugger's plugins (only loaded when a QML app is started for debugging)
    for pattern in ["resources/*.debug.pak", "resources/*.debug.bin"]:
        for f in qt.glob(pattern):
            f.unlink()
    shutil.rmtree(qt / "plugins" / "qmltooling", ignore_errors=True)
    unlinked_qt(qt.parent)


def unlinked_qt(runtime):
    """Delete Qt libraries nothing in the bundle can load: no module, plugin, program or other library imports
    them, and no file even contains their name (so nothing loads them by name either). Needs pefile; without it
    nothing is removed."""
    try:
        import pefile
    except ImportError:
        print("pefile isn't installed: keeping every Qt library")
        return
    files = {}
    for f in runtime.rglob("*"):
        if f.suffix.lower() in (".dll", ".pyd", ".exe"):
            files.setdefault(f.name.lower(), f)

    def imports(f):
        try:
            pe = pefile.PE(str(f), fast_load=True)
            pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
                                                   pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"]])
        except Exception:
            return []
        return [e.dll.decode(errors="replace").lower() for a in ("DIRECTORY_ENTRY_IMPORT", "DIRECTORY_ENTRY_DELAY_IMPORT")
                for e in getattr(pe, a, [])]
    # everything that isn't a plain Qt library is a starting point (Python modules, plugins, programs, mpv...)
    todo = [f for n, f in files.items() if not n.startswith("qt6") or "plugins" in f.parts]
    reached = set()
    while todo:
        f = todo.pop()
        if f.name.lower() in reached:
            continue
        reached.add(f.name.lower())
        todo += [files[d] for d in imports(f) if d in files and d not in reached]
    unused = [f for n, f in files.items() if n not in reached]
    if not unused:
        return
    names = [f.stem.encode() for f in unused]
    for f in runtime.rglob("*"):
        if f.is_file() and f not in unused:
            data = f.read_bytes()
            named = [n for n in names if n in data or n.decode().encode("utf-16-le") in data]
            if named:   # something mentions it: keep it
                unused = [u for u in unused if u.stem.encode() not in named]
                names = [n for n in names if n not in named]
    size = sum(f.stat().st_size for f in unused)
    for f in unused:
        f.unlink()
    print(f"Removed {len(unused)} Qt libraries nothing uses ({size / 1e6:.0f} MB)")


def main():
    work = Path(tempfile.mkdtemp(prefix="kura_build_"))
    icon = HERE / "kura.ico"
    make_icon(icon)
    cmd = [sys.executable, "-m", "PyInstaller", str(HERE / "kura.pyw"),
           "--name", "Kura", "--windowed", "--noconfirm", "--icon", str(icon),
           "--contents-directory", "kura_runtime",     # the exe's support files, next to it
           "--paths", str(HERE),                        # anime_organizer, anilist_account, torrents, player...
           "--add-binary", f"{HERE / 'mpv' / 'libmpv-2.dll'};.",   # the built-in player's engine
           "--exclude-module", "tkinter",
           "--distpath", str(work / "dist"), "--workpath", str(work / "build"), "--specpath", str(work)]
    print("Building (this takes a few minutes)...")
    subprocess.run(cmd, check=True)

    built = work / "dist" / "Kura"
    prune(built / "kura_runtime" / "PySide6")
    # Swap by renaming first: if anything is in use, nothing has been deleted yet
    stamp = time.strftime("%H%M%S")
    moved = []
    try:
        for old in (HERE / "Kura.exe", HERE / "kura_runtime"):
            if old.exists():
                aside = old.with_name(f"{old.stem}.old-{stamp}{old.suffix}")
                old.rename(aside)
                moved.append((aside, old))
    except PermissionError:
        for aside, old in reversed(moved):
            aside.rename(old)
        sys.exit(f"Kura.exe (or something using its files) is running. Quit it (tray icon > Quit Kura) and "
                 f"run this again.\nThe new build is waiting in {built}")
    shutil.move(str(built / "Kura.exe"), HERE / "Kura.exe")
    # copy, then tidy up the build folder: a file still held by something (antivirus scanning it) mustn't fail
    # the build after the copy is complete
    shutil.copytree(built / "kura_runtime", HERE / "kura_runtime")
    shutil.rmtree(built / "kura_runtime", ignore_errors=True)
    # which runtime this is: updates download the big runtime zip only when it differs (updater.py)
    import updater
    (HERE / "kura_runtime" / updater.RUNTIME_ID_FILE).write_text(updater.runtime_id(HERE / "kura_runtime"),
                                                                encoding="utf-8")
    for aside in [*HERE.glob("Kura.old-*.exe"), *HERE.glob("kura_runtime.old-*")]:   # this and earlier swaps
        if aside.is_dir():
            shutil.rmtree(aside, ignore_errors=True)   # a file another program holds stays until next time
        else:
            try:
                aside.unlink()
            except OSError:
                pass
    shutil.rmtree(work, ignore_errors=True)
    size = sum(f.stat().st_size for f in (HERE / "kura_runtime").rglob("*") if f.is_file()) / 2 ** 20
    print(f"Done: {HERE / 'Kura.exe'}  (+ kura_runtime, {size:.0f} MB)")


if __name__ == "__main__":
    main()
