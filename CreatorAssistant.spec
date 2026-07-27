# -*- mode: python ; coding: utf-8 -*-

import os
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules


PROJECT_ROOT = Path(SPECPATH).resolve()
EDITION = os.environ.get('CREATOR_ASSISTANT_EDITION', 'developer').strip().lower()
if EDITION not in {'developer', 'commercial'}:
    raise RuntimeError(f'Unsupported CREATOR_ASSISTANT_EDITION={EDITION!r}')
IS_DEVELOPER = EDITION == 'developer'
BUILD_VARIANT = os.environ.get('CREATOR_ASSISTANT_BUILD_VARIANT', 'production').strip().lower()
if BUILD_VARIANT not in {'production', 'staging'}:
    raise RuntimeError(f'Unsupported CREATOR_ASSISTANT_BUILD_VARIANT={BUILD_VARIANT!r}')
if IS_DEVELOPER and BUILD_VARIANT == 'staging':
    raise RuntimeError('Developer staging package is not supported')
APP_NAME = ('CreatorAssistant-Developer' if IS_DEVELOPER else
            ('CreatorAssistant-Commercial-Staging' if BUILD_VARIANT == 'staging' else 'CreatorAssistant'))
SOURCE_ROOT = PROJECT_ROOT / 'src'
sys.path.insert(0, str(SOURCE_ROOT))
BUILD_INFO = PROJECT_ROOT / 'build' / 'generated' / 'build_info.json'
DATA_FILES = [(
    str(SOURCE_ROOT / 'creator_assistant' / 'workers' / 'audio_separator_worker.py'),
    'creator_assistant\\workers',
)]
DOC_NAMES = [
    'README_RU.md', 'beta-user-guide-ru.md', 'beta-quick-start-ru.md',
    'updating-ru.md', 'support-and-privacy-ru.md', 'beta-known-issues-ru.md',
    'privacy-policy-ru.md', 'eula-ru.md',
]
for name in DOC_NAMES:
    source = PROJECT_ROOT / ('docs' if name != 'README_RU.md' else '') / name
    if source.is_file():
        DATA_FILES.append((str(source), 'docs'))
if BUILD_INFO.is_file():
    DATA_FILES.append((str(BUILD_INFO), 'creator_assistant'))

HIDDEN_IMPORTS = ['PySide6.QtMultimedia', 'PySide6.QtMultimediaWidgets']
EXCLUDES = []
if IS_DEVELOPER:
    HIDDEN_IMPORTS += (
        collect_submodules('creator_assistant.services.automation')
        + collect_submodules('creator_assistant.services.publishing')
        + [
            'creator_assistant.ui.autopilot_tab',
            'creator_assistant.ui.publishing_queue',
            'creator_assistant.ui.publishing_accounts',
            'creator_assistant.infrastructure.automation_job_store',
            'creator_assistant.infrastructure.publishing_store',
            'creator_assistant.infrastructure.credential_store',
            'creator_assistant.infrastructure.publishing_startup',
        ]
    )
else:
    HIDDEN_IMPORTS += [
        'creator_assistant.services.licensing',
        'creator_assistant.ui.license_dialog',
        'creator_assistant.infrastructure.secure_credential_store',
        'creator_assistant.services.commercial_setup',
        'creator_assistant.ui.commercial_setup_wizard',
    ]
    EXCLUDES = [
        'creator_assistant.ui.autopilot_tab',
        'creator_assistant.ui.publishing_queue',
        'creator_assistant.ui.publishing_accounts',
        'creator_assistant.services.automation',
        'creator_assistant.services.publishing',
        'creator_assistant.infrastructure.automation_job_store',
        'creator_assistant.infrastructure.publishing_store',
        'creator_assistant.infrastructure.credential_store',
        'creator_assistant.infrastructure.publishing_startup',
        'creator_assistant.domain.automation',
        'creator_assistant.domain.publishing',
    ]

a = Analysis(
    [str(SOURCE_ROOT / 'creator_assistant' / 'main.py')],
    pathex=[str(SOURCE_ROOT)],
    binaries=[],
    datas=DATA_FILES,
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
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
    name=APP_NAME,
)
