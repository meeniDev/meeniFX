# -*- mode: python ; coding: utf-8 -*-
import os

from PyInstaller.utils.hooks import collect_all

assets = [(f'assets/mosaic/{name}', 'assets/mosaic') for name in ('black.png', 'white.png', 'whiteandblack.png')]
assets += [('flight-reacts-flight.gif', '.')]
# Brand artwork: the window icon, the taskbar sizes and the About wordmark.
assets += [('assets', 'assets')]
# The shipped animation and style libraries are data, not Python modules. Without these
# two folders the installed Animations and Styles tabs are empty.
assets += [('presets', 'presets'), ('looks', 'looks')]
# Default effect-preview reference and the sample project shipped with the app.
assets += [('figure_cafe.png', '.'), ('examples', 'examples')]
# Bundled user-facing release notes power the offline What's New dialog.
assets += [('docs/releases', 'docs/releases')]
datas, binaries, hiddenimports = list(assets), [], []
# Pillow draws text clips (fonts, stroke, shadow), imageio/cv2 handle media and Qt is the UI.
for package in ('PyQt6', 'cv2', 'imageio', 'imageio_ffmpeg', 'PIL', 'yt_dlp'):
    collected = collect_all(package)
    datas += collected[0]
    binaries += collected[1]
    hiddenimports += collected[2]

version_file = os.path.join('packaging', 'version_info.txt')

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'tkinterdnd2'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name='meeniFX',
    debug=False, bootloader_ignore_signals=False, strip=False, upx=True,
    console=False, disable_windowed_traceback=False, argv_emulation=False,
    icon='assets/icon.ico',
    version=version_file,
    target_arch=None, codesign_identity=None, entitlements_file=None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=True, upx_exclude=[], name='meeniFX')
