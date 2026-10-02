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

"""The language of a book's text, from its common words, and its text read from the files."""

import zipfile
from collections import Counter

import pytest

from calibre_dedup.extract import TextExtractor
from calibre_dedup.language import _WORDS, detect_language

SAMPLES = {
    "ita": "Il vecchio pescatore guardava il mare che non si calmava, e pensava alla barca che aveva lasciato "
           "nella baia. Non sapeva perché, ma sentiva che quella notte sarebbe stata diversa da tutte le altre. ",
    "eng": "The old fisherman looked at the sea, which would not calm down, and he thought of the boat that he "
           "had left in the bay. He did not know why, but he felt that this night would be different. ",
    "fra": "Le vieux pêcheur regardait la mer qui ne se calmait pas, et il pensait au bateau qu'il avait laissé "
           "dans la baie. Il ne savait pas pourquoi, mais il sentait que cette nuit serait différente. ",
    "deu": "Der alte Fischer sah auf das Meer, das sich nicht beruhigen wollte, und er dachte an das Boot, das er "
           "in der Bucht gelassen hatte. Er wusste nicht warum, aber diese Nacht war anders. ",
    "spa": "El viejo pescador miraba el mar que no se calmaba, y pensaba en la barca que había dejado en la bahía. "
           "No sabía por qué, pero sentía que esa noche sería muy distinta de todas las otras. ",
    "por": "O velho pescador olhava o mar que não se acalmava, e pensava no barco que havia deixado na baía. Ele "
           "não sabia por quê, mas sentia que aquela noite seria muito diferente de todas as outras. ",
    "nld": "De oude visser keek naar de zee, die niet wilde kalmeren, en hij dacht aan zijn boot die hij in de "
           "baai had gelaten. Hij wist niet waarom, maar hij voelde dat deze nacht anders zou zijn. ",
}


def test_no_word_counts_for_two_languages():
    counts = Counter(w for words in _WORDS.values() for w in set(words.split()))
    assert [w for w, n in counts.items() if n > 1] == []


@pytest.mark.parametrize("code", sorted(SAMPLES))
def test_each_language_is_told_from_its_common_words(code):
    assert detect_language(SAMPLES[code] * 4) == code


def test_too_little_or_mixed_text_is_no_language():
    assert detect_language("The end.") is None
    assert detect_language(SAMPLES["ita"] * 4 + SAMPLES["eng"] * 4) is None
    assert detect_language("") is None


def epub(path, pages: list[str]):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("META-INF/container.xml", '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>')
        items = "".join(f'<item id="p{i}" href="p{i}.xhtml" media-type="application/xhtml+xml"/>'
                        for i in range(len(pages)))
        refs = "".join(f'<itemref idref="p{i}"/>' for i in range(len(pages)))
        z.writestr("OEBPS/content.opf", '<package xmlns="http://www.idpf.org/2007/opf"><manifest>'
                   f"{items}</manifest><spine>{refs}</spine></package>")
        for i, text in enumerate(pages):
            z.writestr(f"OEBPS/p{i}.xhtml", f"<html><body><p>{text}</p></body></html>")
    return str(path)


def test_the_language_is_read_in_the_middle_of_the_book(tmp_path):
    # An Italian book whose first pages are the English licence of Project Gutenberg
    book = epub(tmp_path / "book.epub", [SAMPLES["eng"] * 120] + [SAMPLES["ita"] * 150] * 3)
    extractor = TextExtractor(tmp_path)
    try:
        language, chars = extractor.text_profile({"EPUB": book})
    finally:
        extractor.close()
    assert language == "ita" and chars > 80_000  # spaces not counted


def test_a_text_file_and_files_without_text(tmp_path):
    (tmp_path / "b.txt").write_text(SAMPLES["eng"] * 50, encoding="utf-8")
    (tmp_path / "empty.epub").write_bytes(b"PK\x03\x04 not really an EPUB")
    extractor = TextExtractor(tmp_path)
    try:
        text = SAMPLES["eng"] * 50
        assert extractor.text_profile({"TXT": str(tmp_path / "b.txt")}) == ("eng", len(text) - text.count(" "))
        assert extractor.text_profile({"EPUB": str(tmp_path / "missing.epub")}) == (None, None)
        assert extractor.text_profile({"EPUB": str(tmp_path / "empty.epub")}) == (None, None)
        assert extractor.failed == {}  # failing here doesn't make a file unreadable: only the AI's reading does
    finally:
        extractor.close()
