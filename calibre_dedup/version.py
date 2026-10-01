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

"""The program's version, shown in the window title and written to the logs.

"<major.minor>.<commits> (<date of the last commit>)", read from the local git
repository at startup (nothing goes on the network): e.g. "0.1.18 (2026-10-01 16:01)".
With uncommitted changes it is the number the next commit will get, marked -dev,
dated by the newest changed file: "0.1.19-dev (2026-10-01 19:40)". Each version read
from git is kept in VERSION_FILE (not in git), shown when git can't be asked (a copy
without .git, no git installed); without either, the version in pyproject.toml.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from .calibre_env import CREATE_NO_WINDOW

log = logging.getLogger(__name__)

PACKAGE = Path(__file__).resolve().parent
REPO = PACKAGE.parent
VERSION_FILE = PACKAGE / "_version.txt"
DEFAULT = "0.1.0"
DATE_FORMAT = "%Y-%m-%d %H:%M"


def _project_version() -> str:
    """The version in pyproject.toml (or of the installed package), e.g. "0.1.0"."""
    try:
        m = re.search(r'^version\s*=\s*"([^"]+)"', (REPO / "pyproject.toml").read_text(encoding="utf-8"), re.M)
        if m:
            return m.group(1)
    except OSError:
        pass
    try:
        from importlib.metadata import version as installed
        return installed("calibre-dedup")
    except Exception:
        return DEFAULT


def _base() -> str:
    """major.minor, e.g. "0.1"."""
    return ".".join(_project_version().split(".")[:2])


def _git(*args: str) -> str:
    out = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, timeout=10,
                         creationflags=CREATE_NO_WINDOW, check=True)
    return out.stdout.strip()


def from_git() -> str | None:
    """The version as git describes it, or None (no repository, no git, an error)."""
    if not (REPO / ".git").exists() or shutil.which("git") is None:
        return None
    try:
        count = int(_git("rev-list", "--count", "HEAD"))
        changed = [line[3:].split(" -> ")[-1].strip('"') for line in _git("status", "--porcelain").splitlines()]
        if not changed:
            return f"{_base()}.{count} ({_git('log', '-1', '--format=%cd', f'--date=format:{DATE_FORMAT}')})"
        times = []
        for name in changed:
            try:
                times.append((REPO / name).stat().st_mtime)
            except OSError:  # deleted
                pass
        when = datetime.fromtimestamp(max(times)) if times else datetime.now()
        return f"{_base()}.{count + 1}-dev ({when.strftime(DATE_FORMAT)})"
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        log.debug("Version from git unavailable: %s", e)
        return None


def _save(version: str) -> None:
    """Keep it for runs without git; replaced in one step (two programs may start together)."""
    try:
        if VERSION_FILE.is_file() and VERSION_FILE.read_text(encoding="utf-8").strip() == version:
            return
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=PACKAGE, prefix="_version_", suffix=".tmp",
                                         delete=False) as tmp:
            tmp.write(version + "\n")
        os.replace(tmp.name, VERSION_FILE)
    except OSError as e:
        log.debug("Could not save %s: %s", VERSION_FILE, e)


@lru_cache(maxsize=1)
def app_version() -> str:
    version = from_git()
    if version is not None:
        _save(version)
        return version
    try:
        saved = VERSION_FILE.read_text(encoding="utf-8").strip()
        if saved:
            return saved
    except OSError:
        pass
    return _project_version()
