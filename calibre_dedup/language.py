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

"""The language a book's text is written in, from its most common words: no AI, no
network. Calibre's Languages field is often wrong (a book gets the library's default
language when it is added), so copies are told apart by the language of their text."""

from __future__ import annotations

import re
from collections import Counter

# Frequent words of each language that the others here don't use (or rarely), by
# Calibre's language codes. A word in two lists would count for both: none is.
_WORDS = {
    "ita": "gli della delle degli dello nel nella nelle negli alla alle agli dalla dalle che non per sono anche "
           "più questo questa quello quella perché molto essere aveva lui lei loro ancora poi dopo così tutto "
           "tutti mentre",
    "eng": "the and of to that he she with his her which this be at by from they were have would there been "
           "what when their said could into them then him not but you",
    "fra": "les et est une dans pour pas sur aux elle cette avec sont nous vous était avait ont été leur plus "
           "tout comme ces fait au ce qu",
    "deu": "der und ist nicht ein eine zu den dem mit sich auf für im von auch wird bei oder nach noch aus sie "
           "war hatte ich wir einen einem dass",
    "spa": "el los las y pero más muy también cuando sus sin fue había hasta eso ella ellos estaba habían",
    "por": "os em um uma não ao ele ela foi são seu sua mas muito isso já pelo pela seus suas onde havia também",
    "nld": "het een van ik niet hij zijn op aan maar ook nog wat zo dan dat met voor werd haar naar bij uit wel "
           "geen toen zij heeft",
}
_LANGUAGE_OF = {word: lang for lang, words in _WORDS.items() for word in words.split()}
NAMES = {"ita": "Italian", "eng": "English", "fra": "French", "deu": "German", "spa": "Spanish",
         "por": "Portuguese", "nld": "Dutch", "lat": "Latin"}

# What an AI may answer for a language (ISO 639-1 codes, older ISO 639-2 codes, names) ->
# Calibre's language codes (ISO 639-3, as its Languages field stores them).
_CODES = {
    "it": "ita", "en": "eng", "fr": "fra", "de": "deu", "es": "spa", "pt": "por", "nl": "nld", "la": "lat",
    "ru": "rus", "pl": "pol", "sv": "swe", "da": "dan", "no": "nor", "nb": "nob", "fi": "fin", "el": "ell",
    "tr": "tur", "ro": "ron", "hu": "hun", "cs": "ces", "sk": "slk", "hr": "hrv", "sr": "srp", "sl": "slv",
    "ca": "cat", "eu": "eus", "gl": "glg", "ja": "jpn", "zh": "zho", "ko": "kor", "ar": "ara", "he": "heb",
    "hi": "hin", "uk": "ukr", "bg": "bul", "et": "est", "lv": "lav", "lt": "lit", "ga": "gle", "cy": "cym",
    "is": "isl", "eo": "epo",
    "fre": "fra", "ger": "deu", "dut": "nld", "gre": "ell", "chi": "zho", "cze": "ces", "rum": "ron",
    "slo": "slk", "ice": "isl", "wel": "cym",
    "italian": "ita", "italiano": "ita", "english": "eng", "inglese": "eng", "french": "fra", "francese": "fra",
    "german": "deu", "tedesco": "deu", "spanish": "spa", "spagnolo": "spa", "portuguese": "por",
    "portoghese": "por", "dutch": "nld", "olandese": "nld", "latin": "lat", "latino": "lat",
}


def language_code(value) -> str | None:
    """Calibre's code for a language as an AI may write it ("it", "ita", "Italian"); None if unknown."""
    v = str(value or "").strip().casefold()
    if v in _CODES:
        return _CODES[v]
    return v if v in set(_CODES.values()) else None

MIN_HITS = 20  # fewer frequent words: too little text to tell
MARGIN = 3  # the language found must have this many times the hits of the next one


def detect_language(text: str) -> str | None:
    """The language code ("ita", "eng"…) of `text`, or None when it can't be told
    with confidence: too little text, a language not known here, or two mixed."""
    hits = Counter(_LANGUAGE_OF[w] for w in re.findall(r"[^\W\d_]+", text.casefold()) if w in _LANGUAGE_OF)
    if not hits:
        return None
    (best, count), *rest = hits.most_common(2)
    runner_up = rest[0][1] if rest else 0
    return best if count >= MIN_HITS and count >= MARGIN * runner_up else None


def language_name(code: str) -> str:
    return NAMES.get(code, code)
