# Copyright (c) 2026 Antonio Romeo <antonioromeo@ilve.it>
# Author: Antonio Romeo (with Claude Code et al.)
# SPDX-License-Identifier: MIT
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

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


def _calibre_config_dir() -> Path:
    if sys.platform == "win32":
        cfg = Path(os.environ.get("APPDATA", "")) / "calibre"
    elif sys.platform == "darwin":
        cfg = Path.home() / "Library" / "Preferences" / "calibre"
    else:
        cfg = Path.home() / ".config" / "calibre"
    return Path(os.environ.get("CALIBRE_CONFIG_DIRECTORY", cfg))


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def known_libraries() -> list[str]:
    """Libraries Calibre has used, most used first: its current library (global.py.json),
    then its usage counts (gui.json; global.py.json in older versions). Paths that differ
    only in case or slashes are one library; those without a metadata.db are left out."""
    cfg = _calibre_config_dir()
    current = _read_json(cfg / "global.py.json")
    counts: dict[str, tuple[str, int]] = {}  # normalized path -> (path, uses)
    for data in (_read_json(cfg / "gui.json"), current):
        stats = data.get("library_usage_stats")
        for p, n in (stats.items() if isinstance(stats, dict) else ()):
            key = os.path.normcase(os.path.normpath(p))
            first, total = counts.get(key, (str(Path(p)), 0))
            counts[key] = (first, total + (n if isinstance(n, int) else 0))
    libs = [p for p, _ in sorted(counts.values(), key=lambda pn: -pn[1])]
    if current.get("library_path"):
        libs.insert(0, str(Path(current["library_path"])))
    out, seen = [], set()
    for p in libs:
        key = os.path.normcase(os.path.normpath(p))
        if key not in seen and Path(p, "metadata.db").is_file():
            out.append(p)
        seen.add(key)
    return out
