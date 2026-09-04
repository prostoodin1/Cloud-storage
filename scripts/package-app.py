"""Run PyInstaller without unrelated development-tool DLLs on Windows PATH."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    environment = os.environ.copy()
    if sys.platform == "win32":
        windows = Path(environment.get("SystemRoot", r"C:\Windows"))
        # In particular, Poppler ships an ICU DLL with versioned exports. Qt
        # imports Windows' unversioned ICU API; collecting Poppler's DLL makes
        # every frozen Qt application fail before its entry point can run.
        environment["PATH"] = os.pathsep.join(
            str(path)
            for path in (
                Path(sys.executable).parent,
                Path(sys.base_prefix),
                windows / "System32",
                windows,
                windows / "System32" / "Wbem",
            )
        )
    return subprocess.run(
        [sys.executable, "-m", "PyInstaller", *sys.argv[1:]],
        env=environment,
        check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
