#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
install_root="$HOME/.local/opt/cloud-storage"
unit_root="$HOME/.config/systemd/user"

mkdir -p "$install_root" "$unit_root" "$HOME/.local/share/cloud-storage"
cp -a "$project_root/dist/CloudStorageServerCore/." "$install_root/"
install -m 0644 "$project_root/packaging/cloud-storage-core.service" "$unit_root/cloud-storage-core.service"
systemctl --user daemon-reload
systemctl --user enable --now cloud-storage-core.service
echo "Cloud Storage Server Core user service installed."
