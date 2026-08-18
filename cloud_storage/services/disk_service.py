from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import psutil

from cloud_storage.models import (
    AppSettings,
    DiskConfiguration,
    DiskMode,
    DiskRole,
    DiskSnapshot,
    DiskStatus,
)

_VIRTUAL_FILESYSTEMS = {
    "autofs",
    "cgroup",
    "cgroup2",
    "devfs",
    "devtmpfs",
    "overlay",
    "proc",
    "pstore",
    "squashfs",
    "sysfs",
    "tmpfs",
    "tracefs",
    "cloudstorage",
}


def _stable_id(*parts: str | None) -> str:
    material = "|".join((part or "").strip().casefold() for part in parts)
    return "disk-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _run_json(command: list[str], timeout: int = 5) -> Any:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if completed.returncode != 0 or not completed.stdout.strip():
            return None
        return json.loads(completed.stdout.lstrip("\ufeff"))
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None


class DiskService:
    """Discover physical storage and expose OS-backed diagnostics without guessing values."""

    def discover(self) -> list[DiskSnapshot]:
        metadata = self._platform_metadata()
        disks: list[DiskSnapshot] = []
        seen_mounts: set[str] = set()
        for partition in psutil.disk_partitions(all=False):
            mountpoint = partition.mountpoint
            canonical_mount = os.path.normcase(os.path.abspath(mountpoint))
            if canonical_mount in seen_mounts or not self._is_real_storage(partition):
                continue
            seen_mounts.add(canonical_mount)
            info = metadata.get(canonical_mount, {})
            if bool(info.get("virtual")):
                continue
            serial = self._optional_text(info.get("serial"))
            model = self._optional_text(info.get("model"))
            device = partition.device or mountpoint
            disk_id = _stable_id(serial, device, canonical_mount)
            try:
                usage = psutil.disk_usage(mountpoint)
                total_bytes = self._integer(info.get("total_bytes"), usage.total)
                free_bytes = min(total_bytes, self._integer(info.get("free_bytes"), usage.free))
                snapshot = DiskSnapshot(
                    id=disk_id,
                    mountpoint=mountpoint,
                    device=device,
                    label=self._optional_text(info.get("label"))
                    or Path(mountpoint).name
                    or mountpoint,
                    model=model,
                    serial=serial,
                    interface=self._optional_text(info.get("interface")),
                    filesystem=self._optional_text(info.get("filesystem"))
                    or partition.fstype
                    or None,
                    total_bytes=total_bytes,
                    used_bytes=max(0, total_bytes - free_bytes),
                    free_bytes=free_bytes,
                    temperature_c=self._number(info.get("temperature_c")),
                    read_speed_mbps=self._number(info.get("read_speed_mbps")),
                    write_speed_mbps=self._number(info.get("write_speed_mbps")),
                    utilization_percent=self._number(info.get("utilization_percent")),
                    power_on_hours=self._integer_or_none(info.get("power_on_hours")),
                    health_detail=self._optional_text(info.get("health_detail")),
                    disk_number=self._integer_or_none(info.get("disk_number")),
                    is_system=self._is_system_mount(mountpoint),
                )
            except OSError as exc:
                snapshot = DiskSnapshot(
                    id=disk_id,
                    mountpoint=mountpoint,
                    device=device,
                    label=self._optional_text(info.get("label")) or mountpoint,
                    model=model,
                    serial=serial,
                    interface=self._optional_text(info.get("interface")),
                    filesystem=partition.fstype or None,
                    health_detail=str(exc),
                    disk_number=self._integer_or_none(info.get("disk_number")),
                    is_system=self._is_system_mount(mountpoint),
                    available=False,
                )
            disks.append(snapshot)
        return sorted(disks, key=lambda item: item.mountpoint.casefold())

    @staticmethod
    def _windows_volume(snapshot: DiskSnapshot, *, destructive: bool = False) -> str:
        if os.name != "nt":
            raise OSError("Эта системная операция сейчас поддерживается только в Windows")
        mount = snapshot.mountpoint.strip()
        if not re.fullmatch(r"[A-Za-z]:[\\/]?", mount):
            raise OSError("Не удалось безопасно определить букву тома")
        if destructive and snapshot.is_system:
            raise PermissionError("Системный диск защищён от этой операции")
        return mount[:2].upper()

    def start_error_check(self, snapshot: DiskSnapshot) -> None:
        volume = self._windows_volume(snapshot)
        self._start_elevated("chkdsk.exe", [volume, "/scan"])

    def start_optimize(self, snapshot: DiskSnapshot) -> None:
        volume = self._windows_volume(snapshot, destructive=True)
        self._start_elevated("defrag.exe", [volume, "/O", "/U", "/V"])

    def start_format(self, snapshot: DiskSnapshot, *, filesystem: str = "NTFS") -> None:
        volume = self._windows_volume(snapshot, destructive=True)
        if filesystem not in {"NTFS", "exFAT"}:
            raise ValueError("Неподдерживаемая файловая система")
        drive_letter = volume[0]
        command = (
            f"Format-Volume -DriveLetter {drive_letter} -FileSystem {filesystem} "
            "-NewFileSystemLabel 'CloudStorage' -Force -Confirm:$false"
        )
        self._start_elevated(
            "powershell.exe",
            ["-NoProfile", "-NonInteractive", "-Command", command],
        )

    def apply_explorer_identity(
        self, snapshot: DiskSnapshot, configuration: DiskConfiguration
    ) -> None:
        if os.name != "nt":
            return
        import ctypes
        import winreg

        volume = self._windows_volume(snapshot)
        drive = volume[0]
        base = rf"Software\Microsoft\Windows\CurrentVersion\Explorer\DriveIcons\{drive}"
        if configuration.explorer_mark_enabled:
            label = configuration.explorer_label or configuration.display_name or "Cloud Storage"
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base + r"\DefaultLabel") as key:
                winreg.SetValueEx(key, "", 0, winreg.REG_SZ, label)
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base + r"\DefaultIcon") as key:
                winreg.SetValueEx(key, "", 0, winreg.REG_SZ, f"{sys.executable},0")
        else:
            for suffix in (r"\DefaultIcon", r"\DefaultLabel"):
                try:
                    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, base + suffix)
                except FileNotFoundError:
                    pass
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, base)
            except (FileNotFoundError, OSError):
                pass
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)

    @staticmethod
    def _start_elevated(executable: str, arguments: list[str]) -> None:
        # Arguments come only from validated constants/drive letters, never arbitrary paths.
        quoted = ",".join("'" + item.replace("'", "''") + "'" for item in arguments)
        script = (
            f"Start-Process -FilePath '{executable}' -ArgumentList @({quoted}) "
            "-Verb RunAs -Wait"
        )
        subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _number(value: Any) -> float | None:
        try:
            return round(float(value), 2) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _integer(value: Any, fallback: int = 0) -> int:
        try:
            return max(0, int(value)) if value is not None else max(0, int(fallback))
        except (TypeError, ValueError):
            return max(0, int(fallback))

    @classmethod
    def _integer_or_none(cls, value: Any) -> int | None:
        return cls._integer(value) if value is not None else None

    @staticmethod
    def _is_system_mount(mountpoint: str) -> bool:
        canonical = os.path.normcase(os.path.abspath(mountpoint)).rstrip("\\/")
        if os.name == "nt":
            system_drive = os.environ.get("SystemDrive", "C:").rstrip("\\/")
            return canonical == os.path.normcase(system_drive)
        return canonical in {"", "/"}

    @staticmethod
    def _is_real_storage(partition: Any) -> bool:
        fstype = (partition.fstype or "").casefold()
        if fstype in _VIRTUAL_FILESYSTEMS:
            return False
        mount = partition.mountpoint.replace("\\", "/")
        if mount.startswith(("/proc", "/sys", "/dev", "/run")):
            return False
        if os.name == "nt":
            try:
                import ctypes

                drive_type = ctypes.windll.kernel32.GetDriveTypeW(str(partition.mountpoint))
                return drive_type in {2, 3}  # removable or fixed; never a network share
            except (AttributeError, OSError):
                return "remote" not in (partition.opts or "").casefold()
        return bool(fstype)

    def _platform_metadata(self) -> dict[str, dict[str, Any]]:
        system = platform.system()
        if system == "Windows":
            return self._windows_metadata()
        if system == "Linux":
            return self._linux_metadata()
        return {}

    @staticmethod
    def _windows_metadata() -> dict[str, dict[str, Any]]:
        script = r"""
[Console]::OutputEncoding=[Text.Encoding]::UTF8
$perfItems = @(Get-CimInstance Win32_PerfFormattedData_PerfDisk_PhysicalDisk -ErrorAction SilentlyContinue)
$physicalDisks = @(Get-PhysicalDisk -ErrorAction SilentlyContinue)
$wmiDisks = @(Get-CimInstance Win32_DiskDrive -ErrorAction SilentlyContinue)
$partitionItems = @(Get-Partition -ErrorAction SilentlyContinue | Where-Object {$_.DriveLetter} | ForEach-Object {
  $p = $_
  $d = $p | Get-Disk -ErrorAction SilentlyContinue
  $v = Get-Volume -DriveLetter $p.DriveLetter -ErrorAction SilentlyContinue
  $number = [int]$d.Number
  $perf = $perfItems | Where-Object {$_.Name -match ('^' + $number + '\s')} | Select-Object -First 1
  $wmi = $wmiDisks | Where-Object {[int]$_.Index -eq $number} | Select-Object -First 1
  $physical = $physicalDisks | Where-Object {
    ($wmi.SerialNumber -and ([string]$_.SerialNumber).Trim() -eq ([string]$wmi.SerialNumber).Trim()) -or
    ($wmi.Model -and ([string]$_.FriendlyName).Trim() -eq ([string]$wmi.Model).Trim())
  } | Select-Object -First 1
  $reliability = $null
  if ($physical) {
    try { $reliability = $physical | Get-StorageReliabilityCounter -ErrorAction Stop } catch {}
  }
  $health = @($d.HealthStatus, ($d.OperationalStatus -join ', ')) | Where-Object {$_} | Select-Object -Unique
  [pscustomobject]@{
    mount = ($p.DriveLetter + ':\')
    model = if ($wmi.Model) {$wmi.Model.Trim()} else {$d.FriendlyName}
    serial = if ($wmi.SerialNumber) {$wmi.SerialNumber.Trim()} else {$d.SerialNumber}
    interface = [string]$d.BusType
    filesystem = $v.FileSystem
    label = $v.FileSystemLabel
    total_bytes = [uint64]$v.Size
    free_bytes = [uint64]$v.SizeRemaining
    disk_number = $number
    temperature_c = if ($reliability) {$reliability.Temperature} else {$null}
    power_on_hours = if ($reliability) {$reliability.PowerOnHours} else {$null}
    read_speed_mbps = if ($perf) {[math]::Round(([double]$perf.DiskReadBytesPersec / 1MB), 2)} else {$null}
    write_speed_mbps = if ($perf) {[math]::Round(([double]$perf.DiskWriteBytesPersec / 1MB), 2)} else {$null}
    utilization_percent = if ($perf) {[math]::Min(100, [math]::Round([double]$perf.PercentDiskTime, 2))} else {$null}
    health_detail = if ($health) {($health -join ' / ')} else {$null}
  }
})
$knownMounts = @($partitionItems | ForEach-Object {$_.mount})
$logicalFallback = @(Get-CimInstance Win32_LogicalDisk -ErrorAction SilentlyContinue | Where-Object {
  $_.DriveType -in @(2, 3) -and (($_.DeviceID + '\') -notin $knownMounts)
} | ForEach-Object {
  [pscustomobject]@{
    mount = ($_.DeviceID + '\')
    model = $null
    serial = $null
    interface = $null
    filesystem = $_.FileSystem
    label = $_.VolumeName
    total_bytes = [uint64]$_.Size
    free_bytes = [uint64]$_.FreeSpace
    disk_number = $null
    temperature_c = $null
    power_on_hours = $null
    read_speed_mbps = $null
    write_speed_mbps = $null
    utilization_percent = $null
    health_detail = $null
    virtual = ($_.VolumeName -match 'Google Drive|OneDrive|Dropbox')
  }
})
$items = @($partitionItems) + @($logicalFallback)
$items | ConvertTo-Json -Compress
"""
        raw = _run_json(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            timeout=12,
        )
        if isinstance(raw, dict):
            raw = [raw]
        result: dict[str, dict[str, Any]] = {}
        for item in raw or []:
            if isinstance(item, dict) and item.get("mount"):
                key = os.path.normcase(os.path.abspath(str(item["mount"])))
                result[key] = item
        return result

    @staticmethod
    def _linux_metadata() -> dict[str, dict[str, Any]]:
        raw = _run_json(
            [
                "lsblk",
                "--json",
                "--bytes",
                "--output",
                "NAME,PATH,MOUNTPOINT,FSTYPE,MODEL,SERIAL,TRAN,TYPE",
            ]
        )
        result: dict[str, dict[str, Any]] = {}

        def visit(node: dict[str, Any], inherited: dict[str, Any]) -> None:
            combined = {
                "model": node.get("model") or inherited.get("model"),
                "serial": node.get("serial") or inherited.get("serial"),
                "interface": node.get("tran") or inherited.get("interface"),
                "filesystem": node.get("fstype"),
                "label": node.get("name"),
            }
            mount = node.get("mountpoint")
            if mount:
                result[os.path.normcase(os.path.abspath(str(mount)))] = combined
            for child in node.get("children") or []:
                if isinstance(child, dict):
                    visit(child, combined)

        for device in (raw or {}).get("blockdevices", []):
            if isinstance(device, dict):
                visit(device, {})
        return result


def evaluate_status(snapshot: DiskSnapshot, configuration: DiskConfiguration | None) -> DiskStatus:
    if not snapshot.available:
        return DiskStatus.UNAVAILABLE
    if configuration is None or configuration.role == DiskRole.UNCONFIGURED:
        return DiskStatus.UNCONFIGURED
    if configuration.mode == DiskMode.WRITES_PAUSED:
        return DiskStatus.WRITES_PAUSED
    if configuration.mode == DiskMode.MAINTENANCE:
        return DiskStatus.MAINTENANCE
    if configuration.mode == DiskMode.DISCONNECTED:
        return DiskStatus.DISCONNECTED
    if snapshot.health_detail:
        return DiskStatus.ATTENTION
    if snapshot.total_bytes:
        low_by_percent = snapshot.used_percent >= configuration.max_fill_percent
        low_by_reserve = snapshot.free_bytes <= configuration.min_free_gib * 1024**3
        if low_by_percent or low_by_reserve:
            return DiskStatus.ALMOST_FULL
    return DiskStatus.HEALTHY


def mark_missing_disks(snapshots: list[DiskSnapshot], settings: AppSettings) -> list[DiskSnapshot]:
    """Keep configured missing media visible without inventing health metrics."""

    by_id = {item.id: item for item in snapshots}
    for disk_id, config in settings.disk_configurations.items():
        if disk_id in by_id or config.role == DiskRole.UNCONFIGURED:
            continue
        by_id[disk_id] = DiskSnapshot(
            id=disk_id,
            mountpoint="—",
            device="—",
            label=config.display_name or "Отключённый диск",
            available=False,
            health_detail="Накопитель не обнаружен системой",
        )
    return sorted(by_id.values(), key=lambda item: (not item.available, item.mountpoint.casefold()))


def with_usage(snapshot: DiskSnapshot, used: int, free: int) -> DiskSnapshot:
    """Small pure helper used by monitoring integrations and tests."""

    return replace(snapshot, used_bytes=used, free_bytes=free, total_bytes=used + free)
