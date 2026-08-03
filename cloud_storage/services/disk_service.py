from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
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
    """Read-only physical storage discovery. It never formats, mounts, or ejects media."""

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
            serial = self._optional_text(info.get("serial"))
            model = self._optional_text(info.get("model"))
            device = partition.device or mountpoint
            disk_id = _stable_id(serial, device, canonical_mount)
            try:
                usage = psutil.disk_usage(mountpoint)
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
                    total_bytes=usage.total,
                    used_bytes=usage.used,
                    free_bytes=usage.free,
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
                    available=False,
                )
            disks.append(snapshot)
        return sorted(disks, key=lambda item: item.mountpoint.casefold())

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

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
        script = (
            "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
            "$items = Get-Partition -ErrorAction SilentlyContinue | Where-Object {$_.DriveLetter} | "
            "ForEach-Object {$p=$_; $d=$p | Get-Disk; "
            "$v=Get-Volume -DriveLetter $p.DriveLetter -ErrorAction SilentlyContinue; "
            "[pscustomobject]@{mount=($p.DriveLetter+':\\'); model=$d.FriendlyName; "
            "serial=$d.SerialNumber; interface=[string]$d.BusType; filesystem=$v.FileSystem; "
            "label=$v.FileSystemLabel}}; $items | ConvertTo-Json -Compress"
        )
        raw = _run_json(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script])
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
