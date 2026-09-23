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

# Preferred formats for extraction, best first.
FORMAT_PRIORITY = ["EPUB", "KEPUB", "AZW3", "MOBI", "AZW", "PDF", "FB2", "DOCX", "RTF", "HTMLZ", "TXT", "DJVU"]
MIN_TEXT = 200  # below this a PDF is considered scanned (image only)


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
        if path not in self._converted:
            out = Path(self._tmp.name) / f"c{len(self._converted)}.txt"
            self._run("ebook-convert", [path, str(out)], timeout=600)
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
