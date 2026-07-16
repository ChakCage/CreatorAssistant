from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


DEFAULT_FONT_FAMILY = "Segoe UI"
FALLBACK_FONT_FAMILY = "Arial"
_QT_LOADED_FILES: dict[str, str] = {}


@dataclass(frozen=True)
class FontInfo:
    family: str
    display_name: str
    ass_font_name: str
    regular_file: Path | None = None
    bold_file: Path | None = None
    fallback: bool = False
    fallback_reason: str = ""

    def file_for_weight(self, bold: bool = True) -> Path | None:
        if bold and self.bold_file and self.bold_file.is_file():
            return self.bold_file
        if self.regular_file and self.regular_file.is_file():
            return self.regular_file
        if self.bold_file and self.bold_file.is_file():
            return self.bold_file
        return None


def resolve_font(family: str | None = None) -> FontInfo:
    requested = (family or DEFAULT_FONT_FAMILY).strip() or DEFAULT_FONT_FAMILY
    fonts = _windows_fonts()
    regular = _find_font_file(requested, bold=False, fonts=fonts)
    bold = _find_font_file(requested, bold=True, fonts=fonts)
    if regular or bold:
        return FontInfo(
            family=requested,
            display_name=requested,
            ass_font_name=requested,
            regular_file=regular,
            bold_file=bold,
        )

    fallback_regular = _find_font_file(FALLBACK_FONT_FAMILY, bold=False, fonts=fonts)
    fallback_bold = _find_font_file(FALLBACK_FONT_FAMILY, bold=True, fonts=fonts)
    return FontInfo(
        family=FALLBACK_FONT_FAMILY,
        display_name=FALLBACK_FONT_FAMILY,
        ass_font_name=FALLBACK_FONT_FAMILY,
        regular_file=fallback_regular,
        bold_file=fallback_bold,
        fallback=True,
        fallback_reason=f"Requested font '{requested}' was not found; using {FALLBACK_FONT_FAMILY}.",
    )


def drawtext_font_option(family: str | None, bold: bool = True) -> str:
    info = resolve_font(family)
    font_file = info.file_for_weight(bold)
    if font_file:
        return f"fontfile='{escape_filter_path(font_file)}'"
    return f"font='{_escape_drawtext_value(info.family)}'"


def resolved_qfont(family: str | None, *, bold: bool = True, pixel_size: int | None = None):
    """Build a Qt font from the same concrete file used by FFmpeg drawtext."""
    from PySide6.QtGui import QFont, QFontDatabase, QGuiApplication

    info = resolve_font(family)
    selected_family = info.family
    font_file = info.file_for_weight(bold)
    if QGuiApplication.instance() is not None and font_file and font_file.is_file():
        key = str(font_file.resolve()).casefold()
        loaded_family = _QT_LOADED_FILES.get(key)
        if loaded_family is None:
            font_id = QFontDatabase.addApplicationFont(str(font_file))
            families = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
            loaded_family = str(families[0]) if families else info.family
            _QT_LOADED_FILES[key] = loaded_family
        selected_family = loaded_family
    font = QFont(selected_family)
    font.setBold(bool(bold))
    if pixel_size is not None:
        font.setPixelSize(max(1, int(pixel_size)))
    return font


def fonts_dir_option() -> str:
    fonts_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    if fonts_dir.is_dir():
        return f":fontsdir='{escape_filter_path(fonts_dir)}'"
    return ""


def escape_filter_path(path: Path | str) -> str:
    value = str(path).replace("\\", "/")
    value = value.replace(":", r"\:")
    return value.replace("'", r"\'")


def _windows_fonts() -> dict[str, Path]:
    fonts_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    result: dict[str, Path] = {}
    if os.name != "nt":
        return result
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts",
        ) as key:
            index = 0
            while True:
                try:
                    name, file_name, _ = winreg.EnumValue(key, index)
                except OSError:
                    break
                index += 1
                path = Path(str(file_name))
                if not path.is_absolute():
                    path = fonts_dir / path
                result[_normalise_font_registry_name(str(name))] = path
    except OSError:
        pass
    return result


def _find_font_file(family: str, *, bold: bool, fonts: dict[str, Path]) -> Path | None:
    family_key = _normalise(family)
    preferred = [
        f"{family_key} bold",
        f"{family_key} semibold",
        family_key,
    ] if bold else [
        family_key,
        f"{family_key} regular",
    ]
    for key in preferred:
        path = fonts.get(key)
        if path and path.is_file():
            return path
    for key, path in fonts.items():
        if family_key in key and ("bold" in key) == bold and path.is_file():
            return path
    for key, path in fonts.items():
        if family_key in key and path.is_file():
            return path
    return None


def _normalise_font_registry_name(value: str) -> str:
    value = value.replace("(TrueType)", "").replace("(OpenType)", "")
    return _normalise(value)


def _normalise(value: str) -> str:
    return " ".join(value.strip().lower().split())


def _escape_drawtext_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace(":", r"\:").replace("'", r"\'")
