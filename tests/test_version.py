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

import re
import shutil
import subprocess

import pytest

from calibre_dedup import version


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A project folder of its own: pyproject.toml, the package folder, no git yet."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0.3.7"\n', encoding="utf-8")
    package = tmp_path / "calibre_dedup"
    package.mkdir()
    monkeypatch.setattr(version, "REPO", tmp_path)
    monkeypatch.setattr(version, "PACKAGE", package)
    monkeypatch.setattr(version, "VERSION_FILE", package / "_version.txt")
    version.app_version.cache_clear()
    yield tmp_path
    version.app_version.cache_clear()


def test_without_git_or_saved_version_it_is_pyprojects(repo):
    assert version.app_version() == "0.3.7"


def test_without_git_the_last_version_read_from_git_is_shown(repo):
    version.VERSION_FILE.write_text("0.3.42 (2026-10-01 16:01)\n", encoding="utf-8")
    assert version.app_version() == "0.3.42 (2026-10-01 16:01)"


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_commits_count_and_uncommitted_changes_are_the_next_number_dev(repo):
    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    git("init", "-q")
    git("-c", "user.name=t", "-c", "user.email=t@t", "add", "pyproject.toml")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "one")
    (repo / ".gitignore").write_text("calibre_dedup/_version.txt\n", encoding="utf-8")
    git("add", ".gitignore")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "two")
    assert re.fullmatch(r"0\.3\.2 \(\d{4}-\d\d-\d\d \d\d:\d\d\)", version.app_version())
    assert version.VERSION_FILE.read_text(encoding="utf-8").strip() == version.app_version()  # kept for later
    (repo / "new.py").write_text("x = 1\n", encoding="utf-8")
    version.app_version.cache_clear()
    assert re.fullmatch(r"0\.3\.3-dev \(\d{4}-\d\d-\d\d \d\d:\d\d\)", version.app_version())
