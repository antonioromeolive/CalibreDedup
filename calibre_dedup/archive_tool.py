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


"""List or extract a book's archive (RAR, ZIP, 7Z) as it is: nothing is converted.

ZIP needs only Python. RAR and 7Z need Calibre's own libraries, so this file also
runs inside Calibre (calibre-debug), and bridge_script.py imports it there:

    calibre-debug archive_tool.py list <RAR|ZIP|7Z> <archive>
    calibre-debug archive_tool.py extract <RAR|ZIP|7Z> <archive> <folder>

Each prints one line "@@CDR <json>": the files (list) or {"ok": true} (extract),
or {"error": "..."}."""

import json
import os
import sys
import zipfile

MARK = "@@CDR "


def members(fmt, path):
    """The archive's files (not folders): [{"name": path inside, "size": bytes}].
    Raises ValueError for a password-protected archive."""
    if fmt == "ZIP":
        with zipfile.ZipFile(path) as z:
            infos = [i for i in z.infolist() if not i.is_dir()]
            if any(i.flag_bits & 0x1 for i in infos):
                raise ValueError("password-protected")
            return [{"name": i.filename, "size": i.file_size} for i in infos]
    if fmt == "RAR":
        from calibre.utils import unrar
        return [{"name": h["filename"], "size": h["unpack_size"]} for h in unrar.headers(path) if not h["is_dir"]]
    if fmt == "7Z":
        import py7zr
        with py7zr.SevenZipFile(path) as z:
            if z.needs_password():
                raise ValueError("password-protected")
            return [{"name": i.filename, "size": i.uncompressed} for i in z.list() if not i.is_directory]
    raise ValueError(f"not an archive format: {fmt}")


def extract(fmt, path, folder):
    """Extract every file of the archive into `folder` (which must not escape it)."""
    os.makedirs(folder, exist_ok=True)
    if fmt == "ZIP":
        with zipfile.ZipFile(path) as z:
            z.extractall(folder)  # names with ".." or a drive are made safe by zipfile
    elif fmt == "RAR":
        from calibre.utils import unrar
        unrar.extract(path, folder)
    elif fmt == "7Z":
        import py7zr
        with py7zr.SevenZipFile(path) as z:
            z.extractall(folder)
    else:
        raise ValueError(f"not an archive format: {fmt}")


def member_path(folder, name):
    """Where an extracted member is, or None if its name would lead outside `folder`."""
    folder = os.path.realpath(folder)
    full = os.path.realpath(os.path.join(folder, *name.replace("\\", "/").split("/")))
    return full if full.startswith(folder + os.sep) else None


def main(argv):
    try:
        if argv[0] == "list":
            result = members(argv[1], argv[2])
        elif argv[0] == "extract":
            extract(argv[1], argv[2], argv[3])
            result = {"ok": True}
        else:
            raise ValueError(f"unknown command {argv[0]!r}")
    except Exception as e:  # reported to the caller, which decides
        result = {"error": f"{type(e).__name__}: {e}"}
    sys.stdout.write(MARK + json.dumps(result) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main(sys.argv[1:])
