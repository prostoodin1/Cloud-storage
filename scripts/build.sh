#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python="$project_root/.venv/bin/python"

if [[ ! -x "$python" ]]; then
  echo "Virtual environment not found. Create .venv and install .[build] first." >&2
  exit 1
fi

"$python" -m pytest
"$python" "$project_root/scripts/generate_icon.py"
"$python" -m PyInstaller --noconfirm --clean "$project_root/packaging/CloudStorageServer.spec"
"$python" -m PyInstaller --noconfirm --clean "$project_root/packaging/CloudStorageCore.spec"
"$python" -m PyInstaller --noconfirm --clean "$project_root/packaging/CloudStorageClient.spec"
echo "Build ready: $project_root/dist"
