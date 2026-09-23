"""Normalization of titles, authors, publishers, editions and ISBNs so that
metadata from different sources can be compared."""

from __future__ import annotations

import re
import unicodedata

# Placeholder values Calibre uses when a field is empty, in several UI languages.
UNKNOWN_VALUES = {
    "", "unknown", "sconosciuto", "inconnu", "unbekannt", "desconocido",
    "desconhecido", "onbekend", "nieznany", "okand", "ukjent", "ukendt",
}

_ORDINAL_WORDS = {
    # English
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
    "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12,
    # Italian
    "prima": 1, "primo": 1, "seconda": 2, "secondo": 2, "terza": 3, "terzo": 3,
    "quarta": 4, "quarto": 4, "quinta": 5, "quinto": 5, "sesta": 6, "sesto": 6,
    "settima": 7, "settimo": 7, "ottava": 8, "ottavo": 8, "nona": 9, "nono": 9,
    "decima": 10, "decimo": 10,
    # French / Spanish / German (common forms)
    "premiere": 1, "deuxieme": 2, "troisieme": 3, "primera": 1, "segunda": 2,
    "tercera": 3, "erste": 1, "zweite": 2, "dritte": 3, "vierte": 4, "funfte": 5,
}
_EDITION_WORDS = r"(?:edition|ed|edizione|edizioni|edicion|edition|auflage|aufl|edicao|editie|uitgave)"
_ORDINAL_RE = r"(?:\d{1,2}\s*(?:st|nd|rd|th|a|ª|°|º|e|er|re|eme|ème|\.)?|" + "|".join(_ORDINAL_WORDS) + r")"
# "2nd edition", "seconda edizione", "2a ed.", "2. Auflage"
_EDITION_PREFIX_RE = re.compile(rf"\b({_ORDINAL_RE})\s+{_EDITION_WORDS}\b\.?", re.I)
# "edition 2", "edizione 3"
_EDITION_SUFFIX_RE = re.compile(rf"\b{_EDITION_WORDS}\s+(\d{{1,2}})\b", re.I)

_PUBLISHER_STOPWORDS = {
    "the", "inc", "incorporated", "ltd", "limited", "llc", "co", "company", "corp",
    "corporation", "gmbh", "ag", "spa", "srl", "sa", "plc", "pty", "publishing",
    "publishers", "publisher", "pub", "publ", "publications", "group", "house",
    "books", "book", "press", "media", "editore", "editori", "edizioni", "editrice",
    "casa", "ed", "verlag", "editions", "editorial", "and", "e", "et", "und", "y",
}


def strip_accents(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c))


def _tokens(text: str) -> list[str]:
    text = strip_accents(text).casefold()
    text = text.replace("&", " and ").replace("'", "").replace("’", "")
    text = re.sub(r"[^\w\s]", " ", text)
    return text.split()


def is_unknown(value: str | None) -> bool:
    return value is None or value.strip().casefold() in UNKNOWN_VALUES


def parse_edition_number(text: str | None) -> int | None:
    """Extract an edition number from free text like '2nd edition'."""
    if not text:
        return None
    plain = strip_accents(text)
    m = _EDITION_PREFIX_RE.search(plain) or _EDITION_SUFFIX_RE.search(plain)
    if not m:
        # A bare ordinal like "Second" or "2nd" (e.g. from an AI 'edition' field)
        m = re.fullmatch(rf"\s*({_ORDINAL_RE})\s*", plain, re.I)
        if not m:
            return None
    token = m.group(1).strip().casefold()
    if token in _ORDINAL_WORDS:
        return _ORDINAL_WORDS[token]
    digits = re.match(r"\d+", token)
    if digits:
        n = int(digits.group())
        return n if 0 < n < 100 else None
    return None


def strip_edition(title: str) -> str:
    """Remove edition statements like '(2nd Edition)' from a title."""
    plain = strip_accents(title)
    plain = re.sub(r"[\(\[]\s*[^\)\]]*" + _EDITION_WORDS + r"[^\)\]]*[\)\]]", " ", plain, flags=re.I)
    plain = _EDITION_PREFIX_RE.sub(" ", plain)
    plain = _EDITION_SUFFIX_RE.sub(" ", plain)
    return plain


def title_key(title: str, ignore_subtitle: bool = False) -> str:
    title = strip_edition(title)
    if ignore_subtitle:
        title = re.split(r"\s*[:–—]\s+|\s+-\s+", title, maxsplit=1)[0]
    tokens = _tokens(title)
    if len(tokens) > 1 and tokens[0] in {"the", "a", "an"}:
        tokens = tokens[1:]
    return " ".join(tokens)


def author_key(name: str) -> tuple[str, ...]:
    """Order-insensitive author key: 'Tolkien, J.R.R.' == 'J. R. R. Tolkien'."""
    name = name.replace(".", " ")
    return tuple(sorted(_tokens(name)))


def authors_key(authors: list[str]) -> frozenset[tuple[str, ...]]:
    return frozenset(k for k in (author_key(a) for a in authors if not is_unknown(a)) if k)


def publisher_tokens(publisher: str) -> frozenset[str]:
    return frozenset(t for t in _tokens(publisher) if t not in _PUBLISHER_STOPWORDS)


def same_publisher(a: str, b: str) -> bool:
    ta, tb = publisher_tokens(a), publisher_tokens(b)
    if not ta or not tb:
        # Nothing left after removing stopwords: compare the raw token lists.
        return _tokens(a) == _tokens(b)
    return ta <= tb or tb <= ta


def normalize_isbn(raw: str | None) -> str | None:
    """Return an ISBN-13 string, or None if `raw` is not a valid ISBN."""
    if not raw:
        return None
    s = re.sub(r"[^0-9Xx]", "", raw).upper()
    if len(s) == 10:
        if not s[:9].isdigit() or not (s[9].isdigit() or s[9] == "X"):
            return None
        total = sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(s))
        if total % 11:
            return None
        s = "978" + s[:9]
        check = (10 - sum((1 if i % 2 == 0 else 3) * int(c) for i, c in enumerate(s)) % 10) % 10
        return s + str(check)
    if len(s) == 13 and s.isdigit():
        check = (10 - sum((1 if i % 2 == 0 else 3) * int(c) for i, c in enumerate(s[:12])) % 10) % 10
        return s if check == int(s[12]) else None
    return None
