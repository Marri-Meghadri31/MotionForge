# -*- mode: python ; coding: utf-8 -*-

import os
import shutil
from pathlib import Path

from PyInstaller.utils.hooks import collect_all


manim_data, manim_binaries, manim_imports = collect_all('manim')
manimpango_data, manimpango_binaries, manimpango_imports = collect_all('manimpango')
pymunk_data, pymunk_binaries, pymunk_imports = collect_all('pymunk')

optional_binaries = []
ffmpeg = os.environ.get('MOTIONFORGE_FFMPEG') or shutil.which('ffmpeg')
if ffmpeg and Path(ffmpeg).is_file():
    optional_binaries.append((ffmpeg, 'resources/ffmpeg'))

a = Analysis(
    ['src/motionforge/__main__.py'],
    pathex=['src'],
    binaries=manim_binaries + manimpango_binaries + pymunk_binaries + optional_binaries,
    datas=manim_data + manimpango_data + pymunk_data,
    hiddenimports=manim_imports + manimpango_imports + pymunk_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='prompt-animator',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='prompt-animator',
)
