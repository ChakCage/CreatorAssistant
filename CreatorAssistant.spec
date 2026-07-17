# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


PROJECT_ROOT = Path(SPECPATH).resolve()
SOURCE_ROOT = PROJECT_ROOT / 'src'
BUILD_INFO = PROJECT_ROOT / 'build' / 'generated' / 'build_info.json'
DATA_FILES = [(
    str(SOURCE_ROOT / 'creator_assistant' / 'workers' / 'audio_separator_worker.py'),
    'creator_assistant\\workers',
)]
if BUILD_INFO.is_file():
    DATA_FILES.append((str(BUILD_INFO), 'creator_assistant'))

a = Analysis(
    [str(SOURCE_ROOT / 'creator_assistant' / 'main.py')],
    pathex=[str(SOURCE_ROOT)],
    binaries=[],
    datas=DATA_FILES,
    hiddenimports=['PySide6.QtMultimedia', 'PySide6.QtMultimediaWidgets'],
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
    name='CreatorAssistant',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
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
    upx=True,
    upx_exclude=[],
    name='CreatorAssistant',
)
