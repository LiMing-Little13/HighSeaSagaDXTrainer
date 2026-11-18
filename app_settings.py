import copy
import json
import os
from pathlib import Path
import re
import tempfile

from game_paths import detect_game_root


DEFAULT_BACKUP_ROOT = Path(__file__).resolve().parent / "backups"
DEFAULT_SETTINGS_FILE = Path(__file__).resolve().parent / "trainer_settings.json"
DEFAULT_LOCK_INTERVAL_MS = 700
_GEOMETRY_PATTERN = re.compile(r"[1-9]\d*x[1-9]\d*(?:[+-]\d+[+-]\d+)?")


def _protect_game_directory(path):
    resolved = path.expanduser().resolve()
    game_root = detect_game_root()
    if game_root is not None:
        game_root = game_root.resolve()
        if resolved == game_root or game_root in resolved.parents:
            raise ValueError("禁止向游戏安装目录写入文件，请选择游戏目录之外的位置")
    return resolved


def validate_backup_root(value) -> Path:
    if not isinstance(value, (str, os.PathLike)):
        raise ValueError("备份位置必须是有效的目录路径")
    if not str(value).strip():
        raise ValueError("备份位置不能为空")
    path = _protect_game_directory(Path(value))
    for candidate in (path, *path.parents):
        if candidate.exists() and not candidate.is_dir():
            raise ValueError(f"备份路径不是目录：{candidate}")
    return path


def _normalize(data, strict=False):
    if not isinstance(data, dict):
        if strict:
            raise ValueError("设置内容必须是字典")
        data = {}
    result = copy.deepcopy(data)
    try:
        result["backup_root"] = str(validate_backup_root(result.get("backup_root", DEFAULT_BACKUP_ROOT)))
    except (OSError, RuntimeError, TypeError, ValueError):
        if strict:
            raise
        result["backup_root"] = str(DEFAULT_BACKUP_ROOT)

    interval = result.get("lock_interval_ms", DEFAULT_LOCK_INTERVAL_MS)
    if type(interval) is not int or not 200 <= interval <= 10000:
        if strict:
            raise ValueError("锁定间隔必须为 200 到 10000 毫秒的整数")
        interval = DEFAULT_LOCK_INTERVAL_MS
    result["lock_interval_ms"] = interval

    auto_connect = result.get("auto_connect", True)
    if not isinstance(auto_connect, bool):
        if strict:
            raise ValueError("自动连接设置必须为布尔值")
        auto_connect = True
    result["auto_connect"] = auto_connect

    if "window_geometry" in result:
        geometry = result["window_geometry"]
        if not isinstance(geometry, str) or not _GEOMETRY_PATTERN.fullmatch(geometry):
            if strict:
                raise ValueError("窗口尺寸与位置格式无效")
            result.pop("window_geometry")
    if "window_maximized" in result and not isinstance(result["window_maximized"], bool):
        if strict:
            raise ValueError("窗口最大化设置必须为布尔值")
        result.pop("window_maximized")

    legacy_columns = result.get("item_columns")
    if not isinstance(legacy_columns, dict):
        legacy_columns = {}
    columns_by_tab = result.get("item_columns_by_tab", {})
    if not isinstance(columns_by_tab, dict):
        if strict:
            raise ValueError("物品列宽设置必须是字典")
        columns_by_tab = {}
    for tab in ("0", "1", "2"):
        if not isinstance(columns_by_tab.get(tab), dict):
            columns_by_tab[tab] = copy.deepcopy(legacy_columns)
    result["item_columns_by_tab"] = columns_by_tab
    if not isinstance(result.get("card_columns"), dict):
        if strict and "card_columns" in result:
            raise ValueError("卡片列宽设置必须是字典")
        result["card_columns"] = {}
    return result


def _document_format(raw):
    encoding = "utf-8"
    for marker, candidate in (
        (b"\xff\xfe\x00\x00", "utf-32"),
        (b"\x00\x00\xfe\xff", "utf-32-be"),
        (b"\xef\xbb\xbf", "utf-8-sig"),
        (b"\xff\xfe", "utf-16"),
        (b"\xfe\xff", "utf-16-be"),
    ):
        if raw.startswith(marker):
            encoding = candidate
            break
    text = raw.decode(encoding, errors="replace")
    first_newline = re.search(r"\r\n|\r|\n", text)
    newline = first_newline.group() if first_newline else "\n"
    return encoding, newline, text.endswith(("\r", "\n"))


class SettingsStore:
    def __init__(self, path=DEFAULT_SETTINGS_FILE):
        self.path = Path(path).expanduser().resolve()

    def load(self) -> dict:
        try:
            raw = self.path.read_bytes()
            encoding, _, _ = _document_format(raw)
            data = json.loads(raw.decode(encoding).lstrip("\ufeff"))
        except (OSError, UnicodeError, ValueError):
            data = {}
        return _normalize(data)

    def save(self, data) -> None:
        destination = _protect_game_directory(self.path)
        normalized = _normalize(data, strict=True)
        try:
            raw = destination.read_bytes()
        except FileNotFoundError:
            raw = None
        encoding, newline, trailing_newline = (
            _document_format(raw) if raw is not None else ("utf-8", "\n", True)
        )
        text = json.dumps(normalized, ensure_ascii=False, indent=2, allow_nan=False)
        if trailing_newline:
            text += "\n"
        text = text.replace("\n", newline)
        if encoding in ("utf-16-be", "utf-32-be"):
            text = "\ufeff" + text
        payload = text.encode(encoding)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=f".{destination.name}.", suffix=".tmp",
                dir=destination.parent, delete=False,
            ) as stream:
                temporary = Path(stream.name)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
