"""Version files, checksums and the portable archive, shared by the build and the tests.

The version number is never copied by hand: the PyInstaller resource and the Inno Setup
include are generated from ``meenifx.version`` immediately before a build.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import zipfile
from pathlib import Path

from .version import VERSION, numeric_version, parse_version

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging"


def version_fields(version_text=VERSION):
    return numeric_version(version_text)


def numeric_text(version_text=VERSION):
    return ".".join(str(part) for part in version_fields(version_text))


def version_info_text(version_text=VERSION):
    """The PyInstaller ``version_info.txt`` resource for the executable."""
    if parse_version(version_text) is None:
        raise ValueError(f"not a meeniFX version: {version_text!r}")
    numeric = numeric_text(version_text)
    fields = version_fields(version_text)
    tuple_text = ", ".join(str(part) for part in fields)
    return f"""# Generated from meenifx/version.py - do not edit by hand.
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({tuple_text}),
    prodvers=({tuple_text}),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [StringStruct('CompanyName', 'meeniFX'),
         StringStruct('FileDescription', 'meeniFX Studios video editor'),
         StringStruct('FileVersion', '{numeric}'),
         StringStruct('InternalName', 'meeniFX'),
         StringStruct('LegalCopyright', 'meeniFX'),
         StringStruct('OriginalFilename', 'meeniFX.exe'),
         StringStruct('ProductName', 'meeniFX Studios'),
         StringStruct('ProductVersion', '{version_text}')])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def version_iss_text(version_text=VERSION):
    """The Inno Setup include generated beside the installer script."""
    if parse_version(version_text) is None:
        raise ValueError(f"not a meeniFX version: {version_text!r}")
    return (f'; Generated from meenifx/version.py - do not edit by hand.\n'
            f'#define MyAppName "meeniFX Studios"\n'
            f'#define MyAppVersion "{version_text}"\n'
            f'#define MyAppNumericVersion "{numeric_text(version_text)}"\n'
            f'#define MyAppExeName "meeniFX.exe"\n')


def write_version_files(root=ROOT):
    """Generate the packaging version files; returns (version_info, version.iss)."""
    packaging = Path(root) / "packaging"
    packaging.mkdir(parents=True, exist_ok=True)
    info = packaging / "version_info.txt"
    iss = packaging / "version.iss"
    info.write_text(version_info_text(), encoding="utf-8")
    iss.write_text(version_iss_text(), encoding="utf-8")
    return info, iss


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(256 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_checksums(paths, out_path):
    lines = [f"{sha256_file(path)}  {os.path.basename(str(path))}" for path in sorted(paths)]
    out = Path(out_path)
    out.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return out


def make_portable_zip(dist_dir, out_dir, version_text=VERSION):
    """Zip an already-built application folder (not the source tree)."""
    dist_dir = Path(dist_dir)
    if not dist_dir.is_dir():
        raise FileNotFoundError(f"no built application folder at {dist_dir}")
    if not any(dist_dir.glob("*.exe")):
        raise FileNotFoundError(f"{dist_dir} contains no executable; build the app first")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    target = out / f"meeniFX-Studios-{version_text}-Windows-Portable.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(dist_dir.rglob("*")):
            if path.is_dir() or "__pycache__" in path.parts:
                continue
            archive.write(path, Path("meeniFX") / path.relative_to(dist_dir))
    return target


def copy_release_notes(out_dir, version_text=VERSION):
    """Copy the bundled notes next to the assets for ``gh release create --notes-file``."""
    source = ROOT / "docs" / "releases" / f"{version_text}.md"
    if not source.is_file():
        return None
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    target = out / f"RELEASE_NOTES-{version_text}.md"
    shutil.copyfile(source, target)
    return target
