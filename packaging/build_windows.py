"""Build the Windows release artefacts: bundle, installer, portable ZIP and checksums.

Run on Windows with the application requirements and PyInstaller installed; Inno Setup 6
is used when ISCC is available (the GitHub release workflow installs it). The script can
be re-run safely: version resources are regenerated from ``meenifx/version.py`` every time,
so an installer can never carry a version the app does not report.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from meenifx import release as R  # noqa: E402
from meenifx.version import VERSION  # noqa: E402

SPEC = ROOT / "meeniFX Studios.spec"
ISS = ROOT / "packaging" / "meenifx.iss"
DIST = ROOT / "dist"
APP_DIR = DIST / "meeniFX"
SETUP = DIST / f"meeniFX-Studios-{VERSION}-Windows-Setup.exe"


def find_iscc():
    """ISCC.exe from the environment, PATH, or Inno Setup's usual install folders."""
    configured = os.environ.get("ISCC", "").strip()
    if configured and Path(configured).is_file():
        return Path(configured)
    found = shutil.which("ISCC") or shutil.which("iscc")
    if found:
        return Path(found)
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
        Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
        Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
    ]
    return next((path for path in candidates if path.is_file()), None)


def run_pyinstaller(clean=True):
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm"]
    if clean:
        command.append("--clean")
    command.append(str(SPEC))
    subprocess.run(command, cwd=ROOT, check=True)


def run_inno(iscc):
    subprocess.run([str(iscc), str(ISS)], cwd=ROOT, check=True)
    return SETUP


def build(skip_installer=False, clean=True):
    R.write_version_files(ROOT)
    run_pyinstaller(clean=clean)
    if not APP_DIR.is_dir():
        raise SystemExit(f"PyInstaller did not create {APP_DIR}")
    portable = R.make_portable_zip(APP_DIR, DIST)
    setup = None
    iscc = None if skip_installer else find_iscc()
    if iscc is not None:
        setup = run_inno(iscc)
    elif skip_installer:
        print("Installer skipped on request.")
    else:
        print("ISCC.exe was not found; the portable ZIP was built without an installer.")
    assets = [path for path in (setup, portable) if path is not None and Path(path).is_file()]
    sums = R.write_checksums(assets, DIST / "SHA256SUMS.txt")
    print(f"Version: {VERSION}")
    for path in assets:
        print(f"Asset:   {path}")
    print(f"Sums:    {sums}")
    if setup is None:
        raise SystemExit("The Windows installer was not produced (ISCC is required).")
    return assets, sums


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-installer", action="store_true",
                        help="build only the bundle and portable ZIP")
    parser.add_argument("--no-clean", action="store_true",
                        help="reuse PyInstaller's cache instead of a clean build")
    args = parser.parse_args(argv)
    build(skip_installer=args.skip_installer, clean=not args.no_clean)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
