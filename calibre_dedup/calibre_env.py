"""Locating the Calibre installation and its helper programs."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

_EXE = ".exe" if sys.platform == "win32" else ""
_DEFAULT_DIRS = [
    r"C:\Program Files\Calibre2",
    r"C:\Program Files (x86)\Calibre2",
    "/Applications/calibre.app/Contents/MacOS",
    "/opt/calibre",
    "/usr/bin",
]
# On Windows, keep child processes from flashing console windows.
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def find_calibre_dir(configured: str | None = None) -> Path | None:
    candidates = [configured] if configured else []
    found = shutil.which("calibre-debug")
    if found:
        candidates.append(str(Path(found).parent))
    candidates += _DEFAULT_DIRS
    for d in candidates:
        if d and (Path(d) / f"calibre-debug{_EXE}").is_file():
            return Path(d)
    return None


def tool(calibre_dir: Path, name: str) -> Path:
    # Poppler tools (pdftotext, ...) live in app/bin on Windows, bin/ on Linux.
    for folder in (calibre_dir, calibre_dir / "app" / "bin", calibre_dir / "bin"):
        path = folder / f"{name}{_EXE}"
        if path.is_file():
            return path
    raise FileNotFoundError(f"{name} not found in {calibre_dir}")


def calibre_is_running() -> bool:
    """True if the Calibre GUI or content server is running (they lock libraries)."""
    names = {f"calibre{_EXE}", f"calibre-server{_EXE}"}
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                creationflags=CREATE_NO_WINDOW, check=False,
            ).stdout
            running = {line.split('","')[0].strip('"').lower() for line in out.splitlines() if line}
        else:
            out = subprocess.run(["ps", "-A", "-o", "comm="], capture_output=True, text=True, check=False).stdout
            running = {Path(line.strip()).name.lower() for line in out.splitlines()}
    except OSError:
        return False
    return bool(names & running)


def known_libraries() -> list[str]:
    """Libraries Calibre has recently used, read from its global preferences."""
    if sys.platform == "win32":
        cfg = Path(os.environ.get("APPDATA", "")) / "calibre"
    elif sys.platform == "darwin":
        cfg = Path.home() / "Library" / "Preferences" / "calibre"
    else:
        cfg = Path.home() / ".config" / "calibre"
    cfg = Path(os.environ.get("CALIBRE_CONFIG_DIRECTORY", cfg))
    try:
        data = json.loads((cfg / "global.py.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    libs = list(data.get("library_usage_stats", {}))
    if data.get("library_path"):
        libs.insert(0, data["library_path"])
    return [str(Path(p)) for p in dict.fromkeys(libs) if Path(p, "metadata.db").is_file()]
