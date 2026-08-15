#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
release_root="$project_root/dist/macos-release"
server_root="$release_root/Cloud Storage Server"
client_root="$release_root/Cloud Storage Client"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "macOS packages can only be created on macOS." >&2
  exit 2
fi

manager_app="$project_root/dist/Cloud Storage Server Manager.app"
client_app="$project_root/dist/Cloud Storage Client.app"
core_dir="$project_root/dist/CloudStorageServerCore"
test -d "$manager_app"
test -d "$client_app"
test -d "$core_dir"

rm -rf "$release_root"
mkdir -p "$server_root" "$client_root"
ditto "$manager_app" "$server_root/Cloud Storage Server Manager.app"
ditto "$core_dir" "$server_root/CloudStorageServerCore"
ditto "$client_app" "$client_root/Cloud Storage Client.app"
ln -s /Applications "$server_root/Applications"
ln -s /Applications "$client_root/Applications"

hdiutil create -quiet -volname "Cloud Storage Server" \
  -srcfolder "$server_root" -ov -format UDZO \
  "$release_root/CloudStorage-Server-0.9.14-macOS.dmg"
hdiutil create -quiet -volname "Cloud Storage Client" \
  -srcfolder "$client_root" -ov -format UDZO \
  "$release_root/CloudStorage-Client-0.9.14-macOS.dmg"

shasum -a 256 "$release_root"/*.dmg > "$release_root/SHA256SUMS.txt"
echo "macOS packages ready: $release_root"
