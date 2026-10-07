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

"""Extract text from the first/last pages of an e-book.

* EPUB: read directly (spine order).
* PDF: Calibre's bundled pdftotext/pdfinfo; pdftoppm renders pages of scanned
  PDFs to images for vision models.
* TXT: read directly.
* Anything else (MOBI, AZW3, FB2, DOCX, ...): converted to text once with
  Calibre's ebook-convert.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import posixpath
import re
import subprocess
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree

from . import archive_tool
from .calibre_env import CREATE_NO_WINDOW, tool
from .language import detect_language
from .tempdirs import RunDir

log = logging.getLogger(__name__)
ARCHIVE_TOOL = Path(archive_tool.__file__)

# Formats whose own cover can be read, best first.
EMBEDDED_COVER_FORMATS = ["EPUB", "KEPUB", "AZW3", "MOBI", "AZW", "FB2"]
# Formats whose own title can be read, best first (see TextExtractor.file_title).
FILE_TITLE_FORMATS = EMBEDDED_COVER_FORMATS + ["PDF"]
# Preferred formats for extraction, best first.
FORMAT_PRIORITY = ["EPUB", "KEPUB", "AZW3", "MOBI", "AZW", "PDF", "FB2", "DOCX", "RTF", "HTMLZ", "TXT", "DJVU"]
# The one format of a book whose whole text is compared with another book's (same_text.py), best first:
# then the other FORMAT_PRIORITY formats, then any other Calibre reads (archives and comics never).
SAME_TEXT_FORMATS = ["EPUB", "KEPUB", "MOBI", "AZW3", "AZW", "PDF"]
NO_TEXT_FORMATS = {"ZIP", "RAR", "7Z", "CB7", "CBC", "CBR", "CBZ", "DJV", "DJVU"}
MIN_TEXT = 200  # below this a PDF is considered scanned (image only)
SAMPLE_CHARS = 20000  # text read from the middle of a book, for its language
PDF_SAMPLE_PAGES = 3  # pages read from the middle of a PDF, for its language and length


# What a file should start with, for the formats with a clear signature (others are not checked).
_EXPECTED = {
    "PDF": lambda h: b"%PDF" in h[:1024],
    "EPUB": lambda h: h.startswith(b"PK\x03\x04"),
    "KEPUB": lambda h: h.startswith(b"PK\x03\x04"),
    "DOCX": lambda h: h.startswith(b"PK\x03\x04"),
    "MOBI": lambda h: h[60:68] == b"BOOKMOBI",
    "AZW3": lambda h: h[60:68] == b"BOOKMOBI",
    "LIT": lambda h: h.startswith(b"ITOLITLS"),
    "DJVU": lambda h: h.startswith(b"AT&TFORM"),
    "RTF": lambda h: h.startswith(b"{\\rtf"),
}
# What a file really is, from its first bytes: for the note.
_CONTENT = [
    (lambda h: b"%PDF" in h[:1024], "a PDF"),
    (lambda h: h.startswith(b"PK\x03\x04"), "a ZIP archive"),
    (lambda h: h.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"), "a Word 97-2003 document"),
    (lambda h: h.startswith(b"ITOLITLS"), "a Microsoft Reader (LIT) book"),
    (lambda h: h[60:68] == b"BOOKMOBI", "a MOBI book"),
    (lambda h: h[60:68] == b"TEXtREAd", "a PalmDoc book"),
    (lambda h: h.startswith(b"{\\rtf"), "an RTF document"),
    (lambda h: h.startswith(b"AT&TFORM"), "a DjVu document"),
    (lambda h: h.startswith(b"Rar!"), "a RAR archive"),
    (lambda h: h.startswith(b"\xff\xd8\xff"), "a JPEG image"),
    (lambda h: h.startswith(b"\x89PNG"), "a PNG image"),
    (lambda h: h.lstrip()[:15].lower().startswith((b"<!doctype html", b"<html")), "an HTML page"),
]


def file_problem(fmt: str, path: str) -> str:
    """Why a book's file is not what its format says ("" if it is, or can't be told):
    empty, or e.g. a ".pdf" that holds a Word document. Missing files are not checked."""
    check = _EXPECTED.get(fmt)
    try:
        with open(path, "rb") as f:
            head = f.read(1024)
    except OSError:
        return ""
    if not head:
        return f"the {fmt} file is empty"
    if check is None or check(head):
        return ""
    what = next((name for test, name in _CONTENT if test(head)), None)
    return f"the {fmt} file is really {what}" if what else f"the {fmt} file is not a valid {fmt}"


# Formats Calibre can read (its input formats). Others (DOC, JPG, MBP...) are stored
# by Calibre but can't be opened or converted by it.
CALIBRE_INPUT_FORMATS = {
    "AZW", "AZW1", "AZW3", "AZW4", "CB7", "CBC", "CBR", "CBZ", "CHM", "DJV", "DJVU", "DOCM", "DOCX", "DOTX",
    "EPUB", "FB2", "FBZ", "HTM", "HTML", "HTMLZ", "KEPUB", "LIT", "LRF", "MARKDOWN", "MD", "MOBI", "ODT", "OEB",
    "OPF", "PDB", "PDF", "PML", "PMLZ", "PRC", "RB", "RECIPE", "RTF", "SHTM", "SHTML", "SNB", "TCR", "TEXT",
    "TEXTILE", "TPZ", "TXT", "TXTZ", "XHTM", "XHTML", "ZIP", "RAR", "7Z",
}


def openable_elsewhere(reason: str) -> bool:
    """Whether a format Calibre can't open (by the reason unreadable_formats or a failed
    reading gives) may still open in another program: Calibre doesn't read the format (a
    DOC), or only its converter failed (an RTF Word opens). Not an empty file, nor one that
    isn't what its format says: those can't be opened at all."""
    return reason.startswith("Calibre can't read")


def unreadable_formats(formats: dict[str, str]) -> dict[str, str]:
    """The book's formats Calibre can't open, with why: not an input format of
    Calibre, or a file that is not what its format says. Missing files and
    ORIGINAL_* formats (Calibre's own backups) are not counted."""
    bad = {}
    for fmt, path in formats.items():
        if fmt.startswith("ORIGINAL_") or not Path(path).is_file():
            continue
        if fmt not in CALIBRE_INPUT_FORMATS:
            bad[fmt] = f"Calibre can't read {fmt} files"
        elif problem := file_problem(fmt, path):
            bad[fmt] = problem
    return bad


@dataclass
class Excerpt:
    text: str = ""
    images: list[str] = field(default_factory=list)  # base64 PNGs
    source: str = ""  # e.g. "EPUB first 12000 chars"


class TextExtractor:
    def __init__(self, calibre_dir: Path, pdf_pages: int = 6, text_chars: int = 12000,
                 render_images: bool = False):
        self.calibre_dir = calibre_dir
        self.pdf_pages = pdf_pages
        self.text_chars = text_chars
        self.render_images = render_images
        self._converted: dict[str, str] = {}  # path -> full text (ebook-convert cache)
        self._conversion_failed: dict[str, str] = {}  # path -> the error, given again without converting
        self._covers: dict[str, bytes | None] = {}  # path -> cover inside the file
        # path -> why its text could not be read (corrupt, DRM, no Calibre reader...)
        self.failed: dict[str, str] = {}
        self._tmp = RunDir("read_")
        self._archives: list[str] = []  # folders of the archives extracted for this run

    def close(self) -> None:
        self._tmp.cleanup()

    @staticmethod
    def pick_format(formats: dict[str, str]) -> tuple[str, str] | None:
        for fmt in FORMAT_PRIORITY:
            if fmt in formats and Path(formats[fmt]).is_file():
                return fmt, formats[fmt]
        for fmt, path in formats.items():
            if Path(path).is_file():
                return fmt, path
        return None

    def excerpt(self, formats: dict[str, str], part: str) -> Excerpt:
        """`part` is 'start' or 'end'."""
        picked = self.pick_format(formats)
        if not picked:
            return Excerpt(source="no readable file")
        fmt, path = picked
        try:
            if fmt == "PDF":
                return self._pdf(path, part)
            if fmt in ("EPUB", "KEPUB"):
                text = _epub_text(path, part, self.text_chars)
            elif fmt == "TXT":
                text = _slice(Path(path).read_text(encoding="utf-8", errors="replace"), part, self.text_chars)
            else:
                text = _slice(self._convert(path), part, self.text_chars)
            return Excerpt(text=text, source=f"{fmt} {part} {self.text_chars} chars")
        except Exception as e:  # corrupt, DRM, unsupported...
            log.warning("Cannot extract text from %s: %s", path, e)
            if Path(path).is_file():  # not a file (or drive) that went away meanwhile
                self.failed[path] = f"Calibre can't read the {fmt} file ({_error_line(str(e))})"
            return Excerpt(source=f"{fmt} extraction failed: {e}")

    def whole_text(self, fmt: str, path: str) -> str:
        """The whole text of one file (see whole_text). Raises when it can't be read."""
        if fmt == "PDF":
            return _clean(self._run("pdftotext", ["-enc", "UTF-8", path, "-"], timeout=600)
                          .decode("utf-8", "replace"))
        if fmt in ("EPUB", "KEPUB", "TXT"):
            return whole_text(fmt, path)
        return self._convert(path)

    def failed_formats(self, formats: dict[str, str]) -> dict[str, str]:
        """The book's formats whose text could not be read so far, with why."""
        return {fmt: self.failed[path] for fmt, path in formats.items() if path in self.failed}

    def text_profile(self, formats: dict[str, str]) -> tuple[str | None, int | None]:
        """The language of the book's text (language.detect_language) and its length in
        characters, spaces not counted (as _visible_chars), from its best format with text:
        (None, None) when none has any (a scanned PDF, files that can't be read). The
        language is read in the middle of the
        book, since front or back matter may be in another one (a Project Gutenberg
        licence, a copyright page); a PDF's length is estimated from a few pages there.
        A file that fails here is not marked unreadable: only the AI's reading does that."""
        for fmt in FORMAT_PRIORITY:
            path = formats.get(fmt)
            if not path or not Path(path).is_file():
                continue
            try:
                sample, chars = self._sample(fmt, path)
            except Exception as e:  # corrupt, DRM, unsupported...
                log.info("Cannot read the text of %s: %s", path, _error_line(str(e)))
                continue
            if len(sample.strip()) >= MIN_TEXT:
                return detect_language(sample), chars
        return None, None

    def _sample(self, fmt: str, path: str) -> tuple[str, int | None]:
        """(text from the middle of the book, the whole text's length)."""
        if fmt in ("EPUB", "KEPUB"):
            return _epub_sample(path, SAMPLE_CHARS)
        if fmt == "PDF":
            total = self._pdf_pages(path)
            if total == 0:
                return "", None
            first = max(1, (total - PDF_SAMPLE_PAGES) // 2 + 1)
            last = min(total, first + PDF_SAMPLE_PAGES - 1)
            text = _clean(self._run("pdftotext", ["-f", str(first), "-l", str(last), "-enc", "UTF-8", path, "-"])
                          .decode("utf-8", "replace"))
            return text[:SAMPLE_CHARS], round(_letters(text) / (last - first + 1) * total)
        if fmt == "TXT":
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        else:
            text = self._convert(path)
        return _middle(text, SAMPLE_CHARS), _letters(text)

    def embedded_cover(self, formats: dict[str, str]) -> tuple[str, bytes] | None:
        """The cover image stored inside the book's file (not Calibre's cover.jpg,
        which "Download metadata" may have replaced): (file path, image bytes).
        EPUB is read directly; MOBI, AZW3 and FB2 with Calibre's ebook-meta."""
        for fmt in EMBEDDED_COVER_FORMATS:
            path = formats.get(fmt)
            if not path or not Path(path).is_file():
                continue
            if path not in self._covers:
                try:
                    # ebook-meta also finds covers only shown on a title page (slower: a process)
                    self._covers[path] = ((fmt in ("EPUB", "KEPUB") and _epub_cover(path))
                                          or self._ebook_meta_cover(path))
                except Exception as e:  # corrupt, DRM, unsupported...
                    log.warning("Cannot read the cover inside %s: %s", path, e)
                    self._covers[path] = None
            if self._covers[path]:
                return path, self._covers[path]
        return None

    def file_title(self, formats: dict[str, str]) -> str | None:
        """The title written inside the book's file (not Calibre's): EPUB is read directly
        (see epub_title); MOBI, AZW3, FB2 and PDF with Calibre's ebook-meta."""
        for fmt in FILE_TITLE_FORMATS:
            path = formats.get(fmt)
            if not path or not Path(path).is_file():
                continue
            try:
                if fmt in ("EPUB", "KEPUB"):
                    title = epub_title(path)
                else:
                    out = self._run("ebook-meta", [path]).decode("utf-8", "replace")
                    m = re.search(r"^Title\s*:\s*(.+?)\s*$", out, re.M)
                    title = m.group(1) if m else None
            except Exception as e:  # corrupt, DRM, unsupported...
                log.info("Cannot read the title inside %s: %s", path, _error_line(str(e)))
                continue
            if title:
                return title
        return None

    def _ebook_meta_cover(self, path: str) -> bytes | None:
        out = Path(self._tmp.name) / f"cover{len(self._covers)}.img"
        self._run("ebook-meta", [path, f"--get-cover={out}"])
        if not out.is_file():
            return None
        data = out.read_bytes()
        out.unlink(missing_ok=True)
        return data or None

    # --- PDF -------------------------------------------------------------------
    def _pdf_pages(self, path: str) -> int:
        out = self._run("pdfinfo", [path]).decode("utf-8", "replace")
        m = re.search(r"^Pages:\s+(\d+)", out, re.M)
        return int(m.group(1)) if m else 0

    def _pdf(self, path: str, part: str) -> Excerpt:
        total = self._pdf_pages(path)
        if total == 0:
            return Excerpt(source="PDF has no pages")
        n = min(self.pdf_pages, total)
        first, last = (1, n) if part == "start" else (total - n + 1, total)
        text = self._run("pdftotext", ["-f", str(first), "-l", str(last), "-enc", "UTF-8", path, "-"])
        # As much text as the other formats: a few dense pages can exceed a small model's context
        text = _slice(_clean(text.decode("utf-8", "replace")), part, self.text_chars)
        source = f"PDF pages {first}-{last}"
        if len(text) >= MIN_TEXT or not self.render_images:
            return Excerpt(text=text, source=source)
        # Scanned PDF: render pages for a vision model (max 4 to keep requests small).
        if part == "start":
            img_first, img_last = first, min(last, first + 3)
        else:
            img_first, img_last = max(first, last - 3), last
        prefix = Path(self._tmp.name) / f"p{abs(hash((path, part)))}"
        self._run("pdftoppm", ["-f", str(img_first), "-l", str(img_last), "-r", "110", "-png", path, str(prefix)])
        images = [base64.b64encode(p.read_bytes()).decode() for p in sorted(prefix.parent.glob(prefix.name + "*.png"))]
        for p in prefix.parent.glob(prefix.name + "*.png"):
            p.unlink(missing_ok=True)
        return Excerpt(text=text, images=images, source=f"{source} (scanned, {len(images)} images)")

    # --- other formats ---------------------------------------------------------
    def _convert(self, path: str) -> str:
        if path in self._conversion_failed:  # converting again would only fail again, slowly
            raise RuntimeError(self._conversion_failed[path])
        if path not in self._converted:
            out = Path(self._tmp.name) / f"c{len(self._converted)}.txt"
            try:
                self._run("ebook-convert", [path, str(out)], timeout=600)
            except Exception as e:
                self._conversion_failed[path] = str(e)
                raise
            self._converted[path] = _clean(out.read_text(encoding="utf-8", errors="replace"))
            out.unlink(missing_ok=True)
        return self._converted[path]

    # --- archives (RAR, ZIP, 7Z; see archives.py) ------------------------------------
    def archive_members(self, fmt: str, path: str) -> list[dict]:
        """The archive's files: [{"name", "size"}]. ZIP directly; RAR and 7Z with Calibre."""
        if fmt == "ZIP":
            return archive_tool.members(fmt, path)
        return self._archive_tool("list", fmt, path)

    def extract_archive(self, fmt: str, path: str, u) -> dict[str, str]:
        """Extract the archive into this run's temporary folder: format -> extracted file,
        for the files `u` (an archives.Unpack) adds. Each file gets the archive's date, so
        the AI's answers about it are found again at the next analysis."""
        folder = Path(self._tmp.name) / f"archive{len(self._archives)}"
        self._archives.append(str(folder))
        if fmt == "ZIP":
            archive_tool.extract(fmt, path, str(folder))
        else:
            self._archive_tool("extract", fmt, path, str(folder))
        files = {}
        for f, name in u.add.items():
            file = archive_tool.member_path(str(folder), name)
            if file is None or not Path(file).is_file():
                raise RuntimeError(f"{name} not found after extracting")
            os.utime(file, (u.mtime, u.mtime))
            files[f] = file
        return files

    def _archive_tool(self, *args: str):
        out = self._run("calibre-debug", [str(ARCHIVE_TOOL), *args], timeout=600).decode("utf-8", "replace")
        line = next((x for x in reversed(out.splitlines()) if x.startswith(archive_tool.MARK)), None)
        if line is None:
            raise RuntimeError(f"no answer from archive_tool: {out[-300:]}")
        result = json.loads(line[len(archive_tool.MARK):])
        if isinstance(result, dict) and "error" in result:
            raise RuntimeError(result["error"])
        return result

    def _run(self, name: str, args: list[str], timeout: int = 120) -> bytes:
        proc = subprocess.run(
            [str(tool(self.calibre_dir, name)), *args], capture_output=True, timeout=timeout,
            creationflags=CREATE_NO_WINDOW, check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"{name} failed: {proc.stderr.decode('utf-8', 'replace')[-500:]}")
        return proc.stdout


def same_text_format(formats: dict[str, str]) -> tuple[str, str] | None:
    """The one format of a book compared for the same text (SAME_TEXT_FORMATS): (format, path)."""
    order = SAME_TEXT_FORMATS + [f for f in FORMAT_PRIORITY if f not in SAME_TEXT_FORMATS]
    others = sorted(f for f in formats if f not in order and f in CALIBRE_INPUT_FORMATS)
    for fmt in order + others:
        path = formats.get(fmt)
        if path and fmt not in NO_TEXT_FORMATS and Path(path).is_file():
            return fmt, path
    return None


def whole_text(fmt: str, path: str) -> str | None:
    """The whole text of a file read without Calibre's tools (EPUB, TXT), else None."""
    if fmt in ("EPUB", "KEPUB"):
        return _epub_text(path, "start", 1 << 62)
    if fmt == "TXT":
        return _clean(Path(path).read_text(encoding="utf-8", errors="replace"))
    return None


def _error_line(error: str) -> str:
    """The telling line of a tool's error: its last non-empty line (tracebacks end there)."""
    lines = [line.strip() for line in error.splitlines() if line.strip()]
    return (lines[-1] if lines else error)[:150]


def _slice(text: str, part: str, chars: int) -> str:
    return text[:chars] if part == "start" else text[-chars:]


def _middle(text: str, chars: int) -> str:
    start = max(0, (len(text) - chars) // 2)
    return text[start:start + chars]


def _letters(text: str) -> int:
    """The length of a text without its spaces, as _visible_chars counts an EPUB's."""
    return len(text) - sum(1 for c in text if c.isspace())


def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


class _TextParser(HTMLParser):
    _SKIP = {"script", "style", "head"}
    _BLOCK = {"p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "section", "title"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip:
            self._skip -= 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def _html_to_text(markup: str) -> str:
    parser = _TextParser()
    parser.feed(markup)
    return "".join(parser.parts)


def _epub_spine(z: zipfile.ZipFile) -> list[str]:
    """The names of an EPUB's text documents, in reading order."""
    container = ElementTree.fromstring(z.read("META-INF/container.xml"))
    opf_path = next(el.get("full-path") for el in container.iter() if el.tag.endswith("rootfile"))
    opf = ElementTree.fromstring(z.read(opf_path))
    base = posixpath.dirname(opf_path)
    manifest = {el.get("id"): el.get("href") for el in opf.iter() if el.tag.endswith("}item")}
    names = set(z.namelist())
    spine = (posixpath.normpath(posixpath.join(base, unquote(manifest[el.get("idref")].split("#")[0])))
             for el in opf.iter() if el.tag.endswith("}itemref") and el.get("idref") in manifest)
    return [name for name in spine if name in names]


def _epub_text(path: str, part: str, chars: int) -> str:
    with zipfile.ZipFile(path) as z:
        spine = _epub_spine(z)
        if part == "end":
            spine.reverse()
        collected: list[str] = []
        total = 0
        for name in spine:
            text = _clean(_html_to_text(z.read(name).decode("utf-8", "replace")))
            if not text:
                continue
            collected.append(text)
            total += len(text)
            if total >= chars:
                break
    if part == "end":
        collected.reverse()
    return _slice("\n\n".join(collected), part, chars)


def _epub_sample(path: str, chars: int) -> tuple[str, int]:
    """About `chars` of text from the middle of an EPUB, and the length of its whole text
    (counted without parsing every page)."""
    with zipfile.ZipFile(path) as z:
        spine = _epub_spine(z)
        sizes = [_visible_chars(z.read(name)) for name in spine]
        total, seen, middle = sum(sizes), 0, 0
        for middle, size in enumerate(sizes):  # the page where the middle of the text is
            if seen + size >= total / 2:
                break
            seen += size
        texts: dict[int, str] = {}
        count = 0
        for i in list(range(middle, len(spine))) + list(range(middle - 1, -1, -1)):  # then before it, if short
            texts[i] = _clean(_html_to_text(z.read(spine[i]).decode("utf-8", "replace")))
            count += len(texts[i])
            if count >= chars:
                break
    return "\n\n".join(texts[i] for i in sorted(texts))[:chars], total


def epub_title(path: str) -> str | None:
    """The title in an EPUB's package (its first <dc:title>), or None."""
    with zipfile.ZipFile(path) as z:
        container = ElementTree.fromstring(z.read("META-INF/container.xml"))
        opf_path = next(el.get("full-path") for el in container.iter() if el.tag.endswith("rootfile"))
        opf = ElementTree.fromstring(z.read(opf_path))
    title = next((el.text for el in opf.iter() if el.tag.endswith("}title") and (el.text or "").strip()), None)
    return " ".join(title.split()) if title else None


def _epub_cover(path: str) -> bytes | None:
    """The cover image named in an EPUB's package: EPUB 3 "cover-image", else
    EPUB 2 <meta name="cover">, else an image whose id or name says "cover"."""
    with zipfile.ZipFile(path) as z:
        container = ElementTree.fromstring(z.read("META-INF/container.xml"))
        opf_path = next(el.get("full-path") for el in container.iter() if el.tag.endswith("rootfile"))
        opf = ElementTree.fromstring(z.read(opf_path))
        items = [el for el in opf.iter() if el.tag.endswith("}item")]
        images = [el for el in items if (el.get("media-type") or "").startswith("image/")]
        meta_id = next((el.get("content") for el in opf.iter()
                        if el.tag.endswith("}meta") and el.get("name") == "cover"), None)
        # (An element without children is false: test each against None.)
        for found in ((el for el in images if "cover-image" in (el.get("properties") or "").split()),
                      (el for el in images if el.get("id") == meta_id),
                      (el for el in images if "cover" in f"{el.get('id')} {el.get('href')}".lower())):
            cover = next(found, None)
            if cover is not None:
                break
        else:
            return None
        name = posixpath.normpath(posixpath.join(posixpath.dirname(opf_path), unquote(cover.get("href", ""))))
        return z.read(name) if name in z.namelist() else None


_NOT_TEXT = re.compile(rb"<(head|style|script)\b.*?</\1\s*>|<[^>]*>|&[#\w]+;|\s+", re.S | re.I)


def _visible_chars(markup: bytes) -> int:
    """Rough count of readable characters (bytes) in an HTML document: fast, and
    exact enough to tell a text page from an image page."""
    return len(_NOT_TEXT.sub(b"", markup))


def cover_png(path: str | Path | bytes, max_side: int = 512) -> str | None:
    """Return a cover image (a file, or the image's bytes) as a base64 PNG no
    larger than `max_side`, or None if unreadable."""
    # Imported here so the extractor stays usable without Qt.
    from PySide6.QtCore import QBuffer, QIODevice, Qt
    from PySide6.QtGui import QImage

    image = QImage.fromData(path) if isinstance(path, bytes) else QImage(str(path))
    if image.isNull():
        return None
    if max(image.width(), image.height()) > max_side:
        image = image.scaled(max_side, max_side, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    if not image.save(buf, "PNG"):
        return None
    return base64.b64encode(bytes(buf.data())).decode()
