#!/usr/bin/env bash
set -euo pipefail

unit_root="$HOME/.config/systemd/user"
systemctl --user disable --now cloud-storage-core.service || true
rm -f "$unit_root/cloud-storage-core.service"
systemctl --user daemon-reload
echo "Service removed. Data in ~/.local/share/cloud-storage was preserved."
