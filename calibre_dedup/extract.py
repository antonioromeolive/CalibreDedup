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
import hashlib
import logging
import posixpath
import re
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree

from .calibre_env import CREATE_NO_WINDOW, tool

log = logging.getLogger(__name__)

# Formats whose own cover can be read, best first.
EMBEDDED_COVER_FORMATS = ["EPUB", "KEPUB", "AZW3", "MOBI", "AZW", "FB2"]
# Preferred formats for extraction, best first.
FORMAT_PRIORITY = ["EPUB", "KEPUB", "AZW3", "MOBI", "AZW", "PDF", "FB2", "DOCX", "RTF", "HTMLZ", "TXT", "DJVU"]
MIN_TEXT = 200  # below this a PDF is considered scanned (image only)
MIN_EPUB_TEXT = 2000  # below this an EPUB is taken as images only (no text fingerprint)


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
        self._conversion_failed: set[str] = set()
        self._covers: dict[str, bytes | None] = {}  # path -> cover inside the file
        self._tmp = tempfile.TemporaryDirectory(prefix="cdr_")

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
            return Excerpt(source=f"{fmt} extraction failed: {e}")

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
        text = _clean(text.decode("utf-8", "replace"))
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
        if path in self._conversion_failed:
            return ""
        if path not in self._converted:
            out = Path(self._tmp.name) / f"c{len(self._converted)}.txt"
            try:
                self._run("ebook-convert", [path, str(out)], timeout=600)
            except Exception:
                self._conversion_failed.add(path)
                raise
            self._converted[path] = _clean(out.read_text(encoding="utf-8", errors="replace"))
            out.unlink(missing_ok=True)
        return self._converted[path]

    def _run(self, name: str, args: list[str], timeout: int = 120) -> bytes:
        proc = subprocess.run(
            [str(tool(self.calibre_dir, name)), *args], capture_output=True, timeout=timeout,
            creationflags=CREATE_NO_WINDOW, check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"{name} failed: {proc.stderr.decode('utf-8', 'replace')[-500:]}")
        return proc.stdout


def _slice(text: str, part: str, chars: int) -> str:
    return text[:chars] if part == "start" else text[-chars:]


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


def _epub_text(path: str, part: str, chars: int) -> str:
    with zipfile.ZipFile(path) as z:
        container = ElementTree.fromstring(z.read("META-INF/container.xml"))
        opf_path = next(el.get("full-path") for el in container.iter() if el.tag.endswith("rootfile"))
        opf = ElementTree.fromstring(z.read(opf_path))
        base = posixpath.dirname(opf_path)
        manifest = {el.get("id"): el.get("href") for el in opf.iter() if el.tag.endswith("}item")}
        spine = [manifest[el.get("idref")] for el in opf.iter()
                 if el.tag.endswith("}itemref") and el.get("idref") in manifest]
        names = set(z.namelist())
        if part == "end":
            spine = list(reversed(spine))
        collected: list[str] = []
        total = 0
        for href in spine:
            name = posixpath.normpath(posixpath.join(base, unquote(href.split("#")[0])))
            if name not in names:
                continue
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


def epub_text_digest(path: str) -> str | None:
    """SHA-1 of an EPUB's (X)HTML documents, by name: the same for copies of one
    file whose metadata or cover alone were changed. None if unreadable, or if
    the book is mostly images (comics, scans): their pages are just <img> tags,
    which can be identical in two different volumes."""
    try:
        with zipfile.ZipFile(path) as z:
            names = sorted(n for n in z.namelist() if n.lower().endswith((".html", ".xhtml", ".htm")))
            h = hashlib.sha1()
            text_chars = 0
            for name in names:
                data = z.read(name)
                h.update(name.encode() + b"\0" + data + b"\0")
                text_chars += _visible_chars(data)
    except (OSError, zipfile.BadZipFile, RuntimeError, ValueError) as e:
        log.debug("Cannot hash %s: %s", path, e)
        return None
    if text_chars < MIN_EPUB_TEXT:
        log.debug("Not hashing %s: only %d characters of text", path, text_chars)
        return None
    return h.hexdigest()


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
