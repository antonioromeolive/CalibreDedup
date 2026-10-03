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

"""Whether two books have the same text, whatever their files, formats and metadata:
the same book converted twice, a TXT and an EPUB of one edition. Tested on real
undecided pairs (TODO.md, "Same text"): the same text shares 95% or more of its
5-word runs both ways, another translation under 20%.

A book's text is kept as a fingerprint: a sample (1 in SAMPLE) of the hashes of its
5-word runs, chosen by the hash itself, so two copies keep the same runs. Before
hashing, what differs between copies of one text is made uniform: apostrophes (c'era,
c’era), accented or mis-encoded letters (più, piů; è, č: a file in the wrong code page),
old accents (perche'), Windows-1252 apostrophes in a file read as Latin-1, words
hyphenated at line ends. A text whose language can't be told (language.detect_language:
symbols from a PDF with a broken font, a language not known here) has no fingerprint:
it is never compared."""

from __future__ import annotations

import base64
import hashlib
import re
import unicodedata

from .language import detect_language

SAME_TEXT = 0.95  # share of runs found in the other text, both ways
RUN = 5  # words per run
SAMPLE = 16  # one run in SAMPLE is kept
MIN_WORDS = 500  # less text: too little to tell
LANGUAGE_SAMPLE = 20000  # characters from the middle of the text, for its language

_HYPHENATED = re.compile(r"(\w)-[ \t]*\n\s*(\w)")
# Apostrophes, also Windows-1252's read as Latin-1 (\x91, \x92: control characters)
_APOSTROPHE = "'’‘`´ʼ′\x91\x92"
_OLD_ACCENT = re.compile(rf"([a-z]?)[aeiou][{_APOSTROPHE}](?=[\s.,;:!?»\"”)\x94]|$)")
_APOSTROPHES = re.compile(f"[{_APOSTROPHE}]")
_WORD = re.compile(r"[a-z0-9]+")


def words(text: str) -> list[str]:
    """The text's words, made uniform (see the module's notes)."""
    text = _HYPHENATED.sub(r"\1\2", text).lower()
    text = _OLD_ACCENT.sub(r"\1", text)
    text = _APOSTROPHES.sub("", text)
    # accented or mis-encoded letters dropped (più/piů -> pi, è/č -> ''), other non-ASCII (« — …) -> space
    text = "".join(c if c < "\x80" else ("" if unicodedata.category(c)[0] in "LM" else " ") for c in text)
    return _WORD.findall(text)


def fingerprint(text: str) -> str | None:
    """The text's fingerprint (base64 of the kept runs' 32-bit hashes), or None: too little
    text, or no language found in it."""
    middle = max(0, (len(text) - LANGUAGE_SAMPLE) // 2)
    if detect_language(text[middle:middle + LANGUAGE_SAMPLE]) is None:
        return None
    w = words(text)
    if len(w) < MIN_WORDS:
        return None
    kept = set()
    for i in range(len(w) - RUN + 1):
        h = int.from_bytes(hashlib.blake2b(" ".join(w[i:i + RUN]).encode(), digest_size=4).digest(), "big")
        if h % SAMPLE == 0:
            kept.add(h)
    return base64.b64encode(b"".join(h.to_bytes(4, "big") for h in sorted(kept))).decode() if kept else None


def _runs(fp: str) -> set[int]:
    data = base64.b64decode(fp)
    return {int.from_bytes(data[i:i + 4], "big") for i in range(0, len(data), 4)}


def share(a: str, b: str) -> float:
    """How much of each text is in the other: the lower of the two shares (so a story
    inside a collection, or an abridged edition, isn't the same text)."""
    ra, rb = _runs(a), _runs(b)
    if not ra or not rb:
        return 0.0
    common = len(ra & rb)
    return min(common / len(ra), common / len(rb))


def percent(value: float) -> str:
    return f"{int(value * 100)}%"
