"""定位《High Sea Saga DX》的安装目录与存档目录。

可通过环境变量显式指定：
- ``HSSDX_GAME_ROOT``：游戏安装目录（包含 ``saves`` 子目录）。
- ``HSSDX_GAME_SAVES``：直接指定存档目录，优先级高于安装目录。

未设置时，从 Steam 注册表项及其 ``libraryfolders.vdf`` 中查找
``steamapps/common/High Sea Saga DX``。找不到时返回 ``None``。
"""

import os
import re
from pathlib import Path

GAME_DIR_NAME = "High Sea Saga DX"
GAME_ROOT_ENV = "HSSDX_GAME_ROOT"
GAME_SAVES_ENV = "HSSDX_GAME_SAVES"
SAVES_DIR_NAME = "saves"

_LIBRARY_PATH_PATTERN = re.compile(r'"path"\s+"((?:[^"\\]|\\.)*)"')
_REGISTRY_LOCATIONS = (
    (r"HKEY_CURRENT_USER", r"Software\Valve\Steam", "SteamPath"),
    (r"HKEY_LOCAL_MACHINE", r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
    (r"HKEY_LOCAL_MACHINE", r"SOFTWARE\Valve\Steam", "InstallPath"),
)


def _steam_roots():
    try:
        import winreg
    except ImportError:
        return []
    roots = []
    for hive_name, key_path, value_name in _REGISTRY_LOCATIONS:
        hive = getattr(winreg, hive_name, None)
        if hive is None:
            continue
        try:
            with winreg.OpenKey(hive, key_path) as key:
                value, _kind = winreg.QueryValueEx(key, value_name)
        except OSError:
            continue
        if isinstance(value, str) and value.strip():
            roots.append(Path(value))
    return roots


def _library_roots(steam_root):
    libraries = [steam_root]
    try:
        text = (steam_root / "steamapps" / "libraryfolders.vdf").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return libraries
    for raw in _LIBRARY_PATH_PATTERN.findall(text):
        path = Path(raw.replace("\\\\", "\\"))
        if path not in libraries:
            libraries.append(path)
    return libraries


def detect_game_root():
    """返回游戏安装目录，找不到或未配置时返回 ``None``。"""
    override = os.environ.get(GAME_ROOT_ENV, "").strip()
    if override:
        return Path(override).expanduser().resolve()
    for steam_root in _steam_roots():
        try:
            libraries = _library_roots(steam_root.resolve())
        except OSError:
            continue
        for library in libraries:
            candidate = library / "steamapps" / "common" / GAME_DIR_NAME
            try:
                if candidate.is_dir():
                    return candidate.resolve()
            except OSError:
                continue
    return None


def game_saves():
    """返回存档目录，找不到或未配置时返回 ``None``。"""
    override = os.environ.get(GAME_SAVES_ENV, "").strip()
    if override:
        return Path(override).expanduser().resolve()
    root = detect_game_root()
    if root is None:
        return None
    return root / SAVES_DIR_NAME
