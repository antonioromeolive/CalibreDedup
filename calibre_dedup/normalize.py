"""Normalization of titles, authors, publishers, editions and ISBNs so that
metadata from different sources can be compared."""

from __future__ import annotations

import re
import unicodedata

# Placeholder values Calibre uses when a field is empty, in several UI languages.
UNKNOWN_VALUES = {
    "", "unknown", "sconosciuto", "inconnu", "unbekannt", "desconocido",
    "desconhecido", "onbekend", "nieznany", "okand", "ukjent", "ukendt",
    "unknown author", "autore sconosciuto", "auteur inconnu", "autor desconocido", "unbekannter autor",
}

# "Various authors" in its many spellings (AA.VV., Autori vari, V.A., Various
# Artists...): one author, so anthologies match whatever the spelling. Not the
# same as an unknown author, which is no author at all. As author_key tokens.
_VARIOUS_AUTHORS = {
    ("aa", "vv"), ("aavv",), ("a", "v"), ("va",), ("autori", "vari"), ("autori", "diversi"),
    ("artists", "various"), ("authors", "various"), ("various",), ("vari",),
}
VARIOUS_AUTHORS_KEY = ("various authors",)

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
    # An apostrophe separates words ("l'Inferno" = "l Inferno"): titles taken
    # from file names often lost it or replaced it with a space.
    text = strip_accents(text).casefold()
    text = text.replace("&", " and ")
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
    # Whole words only: "(... Maledette)" or "(Medusa)" is not an edition note.
    plain = re.sub(r"[\(\[]\s*[^\)\]]*\b" + _EDITION_WORDS + r"\b[^\)\]]*[\)\]]", " ", plain, flags=re.I)
    plain = _EDITION_PREFIX_RE.sub(" ", plain)
    plain = _EDITION_SUFFIX_RE.sub(" ", plain)
    return plain


# A bracket opened at the end of a title and never closed: a title cut short,
# e.g. "Piccole donne crescono (Italia" for "... (Italian Edition)".
_UNCLOSED_TAIL_RE = re.compile(r"\s*[\(\[][^\(\)\[\]]*$")


# A bracket group closing the title names a series, collection or imprint, e.g.
# "Il grande freddo (eLit)", "La targa (VINTAGE)", "Strada senza fine (Urania
# 0842)": not part of the title (a collection's issue number isn't a volume). It
# is kept when it tells books apart: a volume ("(2)", "(#4)", "(II)", "vol.",
# "parte"...), a different content ("Serie completa", "antologia", "ridotta")
# or a language ("Em Portuguese Do Brasil", "versione inglese").
_TRAILING_GROUP_RE = re.compile(r"\s*[\(\[]([^\(\)\[\]]*)[\)\]]\s*$")
_NUMBER_ONLY_RE = re.compile(r"(?:n\.?|no\.?|nr\.?)?\s*#?\s*\d+", re.I)
_VOLUME_RE = re.compile(
    r"#|\b(?:vol|volume|volumi|parte|part|libro|book|tomo|tome|band|episodio|episode"
    r"|complet[ao]|complete|raccolta|antologia|anthology|omnibus|trilogia|trilogy|cofanetto|box|boxset"
    r"|integrale|unabridged|ridott[ao]|abridged"
    r"|italian[ao]?|ingles[ei]|english|frances[ei]|french|spagnol[ao]|spanish|espanol|tedesc[ao]|german|deutsch"
    r"|portoghese|portuguese|portugues)\b", re.I)
_ROMAN_RE = re.compile(r"[ivxlcdm]+", re.I)


# Doubled brackets, e.g. "Il nemico di nebbia ( (Urania 332))": read as one group.
_DOUBLED_GROUP_RE = re.compile(r"[\(\[]\s*([\(\[][^\(\)\[\]]*[\)\]])\s*[\)\]]\s*$")


def _strip_trailing_groups(title: str) -> str:
    title = _DOUBLED_GROUP_RE.sub(r"\1", title)
    while True:
        m = _TRAILING_GROUP_RE.search(title)
        if not m:
            return title
        inside = m.group(1).strip()
        if _VOLUME_RE.search(inside) or _NUMBER_ONLY_RE.fullmatch(inside) or _ROMAN_RE.fullmatch(inside):
            return title
        rest = title[:m.start()]
        if not _tokens(rest):  # never reduce a title to nothing
            return title
        title = rest


def series_key(name: str) -> str:
    """Series names compared loosely: "Urania" == "urania", "I casi di X" == "I Casi Di X"."""
    return " ".join(_tokens(name))


def title_key(title: str, ignore_subtitle: bool = False) -> str:
    title = strip_edition(title)
    cut = _UNCLOSED_TAIL_RE.sub("", title)
    if cut.strip():  # never reduce a title to nothing
        title = cut
    title = _strip_trailing_groups(title)
    if ignore_subtitle:
        title = re.split(r"\s*[:–—]\s+|\s+-\s+", title, maxsplit=1)[0]
    tokens = _tokens(title)
    if len(tokens) > 1 and tokens[0] in {"the", "a", "an"}:
        tokens = tokens[1:]
    return " ".join(tokens)


# Titles made from file names: "(Urania - 0411- Supernormale - J. Hunter Holly)".
# Split at dashes with a space on at least one side (not "Jean-Paul").
_SEGMENT_SPLIT_RE = re.compile(r"\s+[-–—]\s*|\s*[-–—]\s+")
MIN_CONTAINED_CHARS = 4  # a shorter title ("It") is found inside too many others


def title_variants(title: str, authors: list[str]) -> list[str]:
    """Title keys to look for similar titles. First the core: for a title made of
    dash-separated parts, the parts that aren't the author, a bare number or a
    collection name followed by a number ("supernormale" for "(Urania - 0411-
    Supernormale - J. Hunter Holly)"); else the title's key. Then the title's
    key and each part kept."""
    variants = [title_key(title)]
    text = title.strip()
    if text[:1] in "([":
        text = text[1:]
        if text.endswith((")", "]")) and text.count("(") + text.count("[") < text.count(")") + text.count("]"):
            text = text[:-1]
    parts = [p for p in _SEGMENT_SPLIT_RE.split(text) if p.strip()]
    if len(parts) < 2:
        return [v for v in variants if v]
    names = {author_key(a) for a in authors} | {similar_author_key(a) for a in authors}

    def is_author(part: str) -> bool:
        people = [p for p in re.split(r"[;&]|\band\b|\be\b", part) if p.strip()]
        return bool(people) and all(author_key(p) in names or similar_author_key(p) in names for p in people)

    def is_number(part: str) -> bool:
        return bool(_tokens(part)) and all(any(c.isdigit() for c in t) for t in _tokens(part))

    kept = []
    for i, part in enumerate(parts):
        if is_author(part) or is_number(part):
            continue
        if i == 0 and len(parts) > 2 and is_number(parts[1]):  # "Urania - 0411 - ...": a collection
            continue
        kept.append(part)
    core = title_key(" ".join(kept)) if kept else ""
    variants = [core] + variants + [title_key(p) for p in kept]
    return [v for v in dict.fromkeys(variants) if v]


# Words around a title that don't make it another book: "ASTRONAVI MALEDETTE
# Inverno 2001" is "Astronavi maledette".
_DATE_WORDS = {
    "inverno", "estate", "primavera", "autunno", "winter", "summer", "spring", "autumn", "fall",
    "gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre",
    "ottobre", "novembre", "dicembre", "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december", "inv", "est", "prim", "aut",
    # issue numbering: "Millemondi NS 22", "Speciale Estate 1999"
    "ns", "nr", "no", "numero", "serie", "nuova", "speciale", "collezione",
}


def noise_words(*texts: str | None) -> set[str]:
    """Words that may surround a title without changing the book: those of the
    given names (authors, series, publisher), plus seasons and months."""
    words = set(_DATE_WORDS)
    for t in texts:
        if t:
            words.update(_tokens(t.replace(".", " ")))
    return words


def _is_noise(word: str, noise: set[str]) -> bool:
    return len(word) == 1 or any(c.isdigit() for c in word) or word in noise


def contains_title(big: str, small: str, noise: set[str]) -> bool:
    """Whether title key `small` is `big`, or appears in it as whole words with
    only noise around: numbers, single letters and `noise` words. "1 abissi d
    acciaio" contains "abissi d acciaio"; "dune messiah" does not contain "dune".
    A `small` made of noise only ("urania", "estate 1998") is not a title."""
    if len(small.replace(" ", "")) < MIN_CONTAINED_CHARS or all(_is_noise(w, noise) for w in small.split()):
        return False
    padded = f" {big} "
    at = padded.find(f" {small} ")
    if at < 0:
        return False
    rest = (padded[:at] + " " + padded[at + len(small) + 2:]).split()
    return all(_is_noise(w, noise) for w in rest)


def author_key(name: str) -> tuple[str, ...]:
    """Order-insensitive author key: 'Tolkien, J.R.R.' == 'J. R. R. Tolkien'.
    Every spelling of "various authors" gives VARIOUS_AUTHORS_KEY."""
    name = name.replace(".", " ")
    key = tuple(sorted(_tokens(name)))
    return VARIOUS_AUTHORS_KEY if key in _VARIOUS_AUTHORS else key


def authors_key(authors: list[str]) -> frozenset[tuple[str, ...]]:
    return frozenset(k for k in (author_key(a) for a in authors if not is_unknown(a)) if k)


MIN_TYPO_LENGTH = 5  # in shorter name parts a letter makes another name (Ann/Anna, Shaw/Shay)


def _one_edit_apart(a: str, b: str) -> bool:
    """Whether one letter added, removed or changed turns `a` into `b`."""
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) > len(b):
        a, b = b, a
    i = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), len(a))
    return a[i + (len(a) == len(b)):] == b[i + 1:]


def names_nearly_equal(a: str, b: str) -> bool:
    """Whether two author names differ by one letter in one of their parts, e.g.
    "Wilson Tucke" / "Tucker, Wilson". Parts of fewer than MIN_TYPO_LENGTH letters
    must be the same (initials are ignored as in similar matching)."""
    ta, tb = list(similar_author_key(a)), list(similar_author_key(b))
    if len(ta) != len(tb) or ta == tb:
        return False
    only_a = [t for t in ta if t not in tb]
    only_b = [t for t in tb if t not in ta]
    return (len(only_a) == len(only_b) == 1 and min(len(only_a[0]), len(only_b[0])) >= MIN_TYPO_LENGTH
            and _one_edit_apart(only_a[0], only_b[0]))


def initials_match(a: str, b: str) -> bool:
    """Whether one name is the other with first names shortened to initials:
    "M. Scott" / "Scott, Melissa", "J.R.R. Tolkien" / "John Ronald Reuel Tolkien".
    Every part of each name must stand for a part of the other (the same word, or
    an initial and a word starting with it), at least one initial is involved, and
    at least one full word (the surname) is the same. "Michael Scott" would match
    "M. Scott" too: only used for books with the same title."""
    ta, tb = list(author_key(a)), list(author_key(b))
    if ta == tb or not any(len(t) == 1 for t in ta + tb):
        return False
    if not {t for t in ta if len(t) > 1} & {t for t in tb if len(t) > 1}:
        return False

    def covered(xs: list[str], ys: list[str]) -> bool:
        free = list(ys)
        for x in sorted(xs, key=len, reverse=True):  # full words first, then initials
            match = next((y for y in free if y == x), None)
            if match is None:
                match = next((y for y in free if (len(x) == 1 or len(y) == 1) and x[0] == y[0]), None)
            if match is None:
                return False
            free.remove(match)
        return True

    return len(ta) == len(tb) and covered(ta, tb) and covered(tb, ta)


# Name parts ignored by "similar" author matching (from the Find Duplicates plugin).
_IGNORE_AUTHOR_WORDS = {"von", "van", "jr", "sr", "i", "ii", "iii", "second", "third", "md", "phd"}


def similar_author_key(name: str) -> tuple[str, ...]:
    """Looser author key, as the Find Duplicates plugin's "similar" algorithm:
    initials and suffixes are dropped, so 'Stephen E. King' == 'King, Stephen'."""
    tokens = author_key(name)
    kept = tuple(t for t in tokens if len(t) > 1 and t not in _IGNORE_AUTHOR_WORDS)
    return kept or tokens


def similar_authors_keys(authors: list[str]) -> list[tuple[str, ...]]:
    """One key per author: books sharing any author are compared."""
    keys = (similar_author_key(a) for a in authors if not is_unknown(a))
    return list(dict.fromkeys(k for k in keys if k))


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
