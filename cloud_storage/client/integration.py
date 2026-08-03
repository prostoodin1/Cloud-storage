from __future__ import annotations

import os
import platform
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

APP_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_RUN_NAME = "CloudStorageClient"


@dataclass(frozen=True, slots=True)
class IntegrationResult:
    location: Path
    detail: str


def client_start_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).resolve()), "--minimized"]
    executable = Path(sys.executable)
    if os.name == "nt" and executable.name.casefold() == "python.exe":
        pythonw = executable.with_name("pythonw.exe")
        if pythonw.exists():
            executable = pythonw
    return [str(executable.resolve()), "-m", "cloud_storage.client.main", "--minimized"]


def integrate_file_manager(
    cache_directory: Path,
    *,
    system: str | None = None,
    home: Path | None = None,
    config_home: Path | None = None,
) -> IntegrationResult:
    cache_directory = cache_directory.expanduser().resolve()
    cache_directory.mkdir(parents=True, exist_ok=True)
    system = system or platform.system()
    home = (home or Path.home()).resolve()
    if system == "Windows":
        return _integrate_windows(cache_directory, home)
    if system == "Linux":
        return _integrate_linux(cache_directory, home, config_home)
    raise OSError("Интеграция поддерживается только в Windows и Linux")


def _integrate_windows(cache_directory: Path, home: Path) -> IntegrationResult:
    try:
        import win32com.client
    except ImportError as exc:
        raise OSError("Компонент интеграции Windows отсутствует в сборке") from exc

    links = home / "Links"
    links.mkdir(parents=True, exist_ok=True)
    shortcut_path = links / "Cloud Storage.lnk"
    shell = win32com.client.Dispatch("WScript.Shell")
    shortcut = shell.CreateShortcut(str(shortcut_path))
    shortcut.TargetPath = str(cache_directory)
    shortcut.WorkingDirectory = str(cache_directory)
    shortcut.Description = "Cloud Storage — офлайн-файлы"
    shortcut.Save()

    pinned = False
    try:
        explorer = win32com.client.Dispatch("Shell.Application")
        namespace = explorer.Namespace(str(cache_directory))
        if namespace is not None:
            namespace.Self.InvokeVerb("pintohome")
            pinned = True
    except Exception:  # Windows versions expose different Explorer verbs
        pinned = False
    detail = (
        "Каталог закреплён в быстром доступе Проводника"
        if pinned
        else "Создана ссылка Cloud Storage в папке Links пользователя"
    )
    return IntegrationResult(shortcut_path, detail)


def _integrate_linux(
    cache_directory: Path,
    home: Path,
    config_home: Path | None,
) -> IntegrationResult:
    link = home / "Cloud Storage"
    if link.is_symlink():
        if link.resolve() != cache_directory:
            raise OSError(f"Ссылка {link} уже ведёт в другой каталог")
    elif link.exists():
        if link.resolve() != cache_directory:
            raise OSError(f"Путь {link} уже занят")
    else:
        link.symlink_to(cache_directory, target_is_directory=True)

    config_root = config_home or Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    bookmarks = config_root / "gtk-3.0" / "bookmarks"
    existing = bookmarks.read_text(encoding="utf-8").splitlines() if bookmarks.exists() else []
    bookmark = f"{cache_directory.as_uri()} Cloud Storage"
    if bookmark not in existing:
        existing.append(bookmark)
        _atomic_write_text(bookmarks, "\n".join(existing) + "\n")
    return IntegrationResult(link, "Каталог добавлен в домашнюю папку и закладки файлового менеджера")


def autostart_enabled(
    *,
    system: str | None = None,
    config_home: Path | None = None,
) -> bool:
    system = system or platform.system()
    if system == "Windows":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APP_RUN_KEY) as key:
                winreg.QueryValueEx(key, APP_RUN_NAME)
            return True
        except FileNotFoundError:
            return False
    if system == "Linux":
        return _linux_autostart_path(config_home).is_file()
    return False


def set_autostart(
    enabled: bool,
    *,
    command: list[str] | None = None,
    system: str | None = None,
    config_home: Path | None = None,
) -> None:
    system = system or platform.system()
    command = command or client_start_command()
    if system == "Windows":
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, APP_RUN_KEY) as key:
            if enabled:
                winreg.SetValueEx(
                    key,
                    APP_RUN_NAME,
                    0,
                    winreg.REG_SZ,
                    subprocess.list2cmdline(command),
                )
            else:
                try:
                    winreg.DeleteValue(key, APP_RUN_NAME)
                except FileNotFoundError:
                    pass
        return
    if system == "Linux":
        path = _linux_autostart_path(config_home)
        if enabled:
            entry = (
                "[Desktop Entry]\n"
                "Type=Application\n"
                "Name=Cloud Storage Client\n"
                f"Exec={shlex.join(command)}\n"
                "Terminal=false\n"
                "X-GNOME-Autostart-enabled=true\n"
            )
            _atomic_write_text(path, entry, mode=0o600)
        else:
            path.unlink(missing_ok=True)
        return
    raise OSError("Автозапуск поддерживается только в Windows и Linux")


def _linux_autostart_path(config_home: Path | None = None) -> Path:
    root = config_home or Path(
        os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
    )
    return root / "autostart" / "cloud-storage-client.desktop"


def _atomic_write_text(path: Path, value: str, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
