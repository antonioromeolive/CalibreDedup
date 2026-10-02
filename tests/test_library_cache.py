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
from dataclasses import replace

from calibre_dedup import library_cache
from calibre_dedup.library_cache import FileChecks
from calibre_dedup.models import Book


def book(tmp_path, formats: dict[str, bytes], last_modified="2026-01-01") -> Book:
    folder = tmp_path / "b1"
    folder.mkdir(exist_ok=True)
    paths = {}
    for fmt, content in formats.items():
        paths[fmt] = folder / f"Dune.{fmt.lower()}"
        paths[fmt].write_bytes(content)
    return Book(1, "Dune", ["Frank Herbert"], None, None, set(), {f: str(p) for f, p in paths.items()}, "u1",
                "b1", str(tmp_path), last_modified=last_modified)


def counting(monkeypatch) -> list:
    checked = []
    real = library_cache.unreadable_formats
    monkeypatch.setattr(library_cache, "unreadable_formats", lambda f: checked.append(set(f)) or real(f))
    return checked


def test_a_book_checked_once_is_not_opened_again(tmp_path, monkeypatch):
    checked = counting(monkeypatch)
    b = book(tmp_path, {"PDF": b"PK\x03\x04 a zip, not a PDF"})
    first = FileChecks(tmp_path / "cache")
    bad = first.bad(b)
    assert "PDF" in bad
    first.save()
    assert FileChecks(tmp_path / "cache").bad(b) == bad and len(checked) == 1


def test_a_changed_book_or_other_formats_are_checked_again(tmp_path, monkeypatch):
    checked = counting(monkeypatch)
    b = book(tmp_path, {"PDF": b"%PDF-1.4"})
    checks = FileChecks(tmp_path / "cache")
    assert checks.bad(b) == {}
    assert checks.bad(replace(b, last_modified="2026-02-02")) == {}
    b2 = book(tmp_path, {"PDF": b"%PDF-1.4", "DOC": b"word"})
    assert "DOC" in checks.bad(b2)
    assert len(checked) == 3


def test_without_last_modified_or_a_folder_nothing_is_kept(tmp_path, monkeypatch):
    checked = counting(monkeypatch)
    b = book(tmp_path, {"PDF": b"%PDF-1.4"}, last_modified="")
    checks = FileChecks(tmp_path / "cache")
    checks.bad(b), checks.bad(b)
    checks.save()
    assert len(checked) == 2 and not (tmp_path / "cache").exists()
    FileChecks(None).bad(replace(b, last_modified="x"))


def test_file_hashes_are_kept_until_the_book_changes(tmp_path):
    from pathlib import Path
    from calibre_dedup.library_cache import FileHashes
    b = replace(book(tmp_path, {"EPUB": b"PK one"}), sizes={"EPUB": 6})
    hashes = FileHashes(tmp_path / "cache")
    first = hashes.digest(b, "EPUB")
    hashes.save()
    Path(b.formats["EPUB"]).write_bytes(b"PK two")  # the same size: only the cache can tell
    assert FileHashes(tmp_path / "cache").digest(b, "EPUB") == first
    assert FileHashes(tmp_path / "cache").digest(replace(b, last_modified="2026-02-02"), "EPUB") != first
    gone = replace(b, formats={"EPUB": str(tmp_path / "gone.epub")})
    assert FileHashes(None).digest(gone, "EPUB") is None  # a file that can't be read has none


def test_the_text_of_a_book_is_read_once(tmp_path):
    from calibre_dedup.library_cache import TextFacts
    calls = []

    class Reader:
        def text_profile(self, formats):
            calls.append(formats)
            return "ita", 1234

    b = book(tmp_path, {"EPUB": b"PK"})
    facts = TextFacts(tmp_path / "cache", Reader())
    assert facts.get(b) == facts.get(b) == ("ita", 1234) and len(calls) == 1
    facts.save()
    assert TextFacts(tmp_path / "cache", Reader()).get(b) == ("ita", 1234) and len(calls) == 1
