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

import pytest

from calibre_dedup.normalize import (
    authors_key, is_unknown, normalize_asin, normalize_isbn, parse_edition_number, same_publisher,
    similar_author_key, similar_authors_keys, title_key,
)


def test_title_key_ignores_case_punctuation_articles_and_edition():
    assert title_key("The Pragmatic Programmer (2nd Edition)") == title_key("pragmatic programmer")
    assert title_key("Dune!") == title_key("dune")
    assert title_key("Clean Code: A Handbook") != title_key("Clean Code")
    assert title_key("Clean Code: A Handbook", ignore_subtitle=True) == title_key("Clean Code")


def test_title_key_ignores_a_bracket_left_open_by_a_cut_title():
    assert title_key("Kate Aylesford (Italia") == title_key("Kate Aylesford (Italian Edition)")
    assert title_key("Kate Aylesford [ediz") == title_key("Kate Aylesford")
    assert title_key("Dune (Book 1) (Ace") == title_key("Dune (Book 1)")  # closed brackets are kept
    assert title_key("(Senza titolo") == title_key("senza titolo")  # never reduced to nothing


def test_title_key_accents():
    assert title_key("Il nome della rosa") == title_key("Il Nome Della Rosa")
    assert title_key("Café") == title_key("Cafe")


def test_authors_key_order_and_format_insensitive():
    assert authors_key(["J. R. R. Tolkien"]) == authors_key(["Tolkien, J.R.R."])
    assert authors_key(["A B", "C D"]) == authors_key(["C D", "A B"])
    assert authors_key(["Unknown"]) == frozenset()


def test_similar_author_key_ignores_initials_and_suffixes():
    assert similar_author_key("Stephen E. King") == similar_author_key("King, Stephen")
    assert similar_author_key("J. R. R. Tolkien") == similar_author_key("Tolkien")
    assert similar_author_key("Martin Luther King Jr.") == similar_author_key("Martin Luther King")
    assert similar_author_key("J") == ("j",)  # nothing left: keep the plain key
    assert similar_authors_keys(["Unknown", "Frank Herbert", "Herbert, F. Frank"]) == [("frank", "herbert")]


def test_parse_edition_number():
    assert parse_edition_number("Second Edition") == 2
    assert parse_edition_number("2nd ed.") == 2
    assert parse_edition_number("Terza edizione") == 3
    assert parse_edition_number("3a edizione") == 3
    assert parse_edition_number("2. Auflage") == 2
    assert parse_edition_number("Edition 4") == 4
    assert parse_edition_number("First") == 1
    assert parse_edition_number("Deluxe") is None
    assert parse_edition_number(None) is None


def test_same_publisher():
    assert same_publisher("O'Reilly Media, Inc.", "O'Reilly")
    assert same_publisher("The MIT Press", "MIT Press")
    assert same_publisher("Mondadori", "Arnoldo Mondadori Editore")
    assert not same_publisher("Penguin", "HarperCollins")


def test_same_publisher_written_differently():
    assert same_publisher("DeAgostini Periodici S.r.l.", "De Agostini periodici")
    assert same_publisher("Arnoldo Mondadori Editore", "A. Mondadori")
    assert same_publisher("Giulio Einaudi editore S.p.A.", "Einaudi")
    assert not same_publisher("A. Mondadori", "Adelphi")
    assert not same_publisher("B. Mondadori", "Arnoldo Mondadori")


def test_normalize_isbn():
    assert normalize_isbn("0-306-40615-2") == "9780306406157"
    assert normalize_isbn("978-0-306-40615-7") == "9780306406157"
    assert normalize_isbn("978-0-306-40615-8") is None
    assert normalize_isbn("garbage") is None
    # right checksum, but no book's ISBN: filler, a magazine's barcode (ISSN, the same on every issue)
    for junk in ("0000000000", "0000000000000", "9999999999999", "9771123076005"):
        assert normalize_isbn(junk) is None


def test_normalize_asin():
    assert normalize_asin(" b01blyjwma ") == "B01BLYJWMA"
    assert normalize_asin("8817042641") == "8817042641"  # a printed book's: its ISBN-10
    # converters' junk, shared by thousands of books
    for junk in ("F20", "0000000000", "XXXXXXXXXX", "8817042642", "URN:UUID:F20", "",
                 "CD4B0B0F-BA5A-46C9-AD42-7EAADBF059B3", None):
        assert normalize_asin(junk) is None


def test_is_unknown():
    assert is_unknown("Unknown")
    assert is_unknown("Sconosciuto")
    assert is_unknown("  ")
    assert not is_unknown("Dune")


def test_title_key_ignores_a_series_or_imprint_in_closing_brackets():
    assert title_key("Rookwood (Everyman)") == title_key("Rookwood")
    assert title_key("Polyeucte (VINTAGE) (Italian Ed") == title_key("Polyeucte")
    assert title_key("Fratelli d'Italia (Gli Adelphi) [Oscar]") == title_key("Fratelli d'Italia")
    assert title_key("Rookwood (Everyman) (2nd Edition)") == title_key("Rookwood")
    # a collection's issue number is not a volume
    assert title_key("Barriers Burned Away (Gutenberg 0842)") == title_key("Barriers Burned Away")
    assert title_key("Il segno (Il Giallo Mondadori 2345)") == title_key("Il segno")
    # doubled brackets
    assert title_key("Rachel Dyer ( (Gutenberg 332))") == title_key("Rachel dyer")
    assert title_key("Glimpses of Three Coasts ( (Gutenberg) (Italian Edition))") == title_key("Glimpses of Three Coasts")
    assert title_key("Saga ( (Libro 2))") != title_key("Saga")  # a volume stays, doubled or not
    # only at the end: brackets inside the title stay
    assert title_key("Il (quasi) perfetto delitto") != title_key("Il perfetto delitto")


def test_title_key_keeps_brackets_that_tell_volumes_apart():
    assert title_key("Dune (Book 1)") != title_key("Dune (Book 2)")
    assert title_key("Il trono di spade (Vol. 3)") != title_key("Il trono di spade")
    assert title_key("Guerra e pace (Parte prima)") != title_key("Guerra e pace (Parte seconda)")
    assert title_key("Memorie (II)") != title_key("Memorie (III)")
    assert title_key("Serie (#4)") != title_key("Serie")
    assert title_key("Racconti (2)") != title_key("Racconti (3)")
    assert title_key("Almanacco (n. 12)") != title_key("Almanacco")
    assert title_key("Saga (Collana Oro) (Libro 2)") != title_key("Saga (Libro 3)")
    assert title_key("(Senza titolo)") == "senza titolo"  # never reduced to nothing


def test_title_key_keeps_brackets_that_change_content_or_language():
    assert title_key("Isabel Leicester (Serie Completa)") != title_key("Isabel Leicester")
    assert title_key("Racconti (Antologia)") != title_key("Racconti")
    assert title_key("Utilitarianism (Em Portuguese Do Brasil)") != title_key("Utilitarianism")
    assert title_key("I promessi sposi (versione ridotta)") != title_key("I promessi sposi")


def test_apostrophe_separates_words():
    from calibre_dedup.normalize import title_key
    assert title_key("Mary's Meadow") == title_key("Mary s Meadow") == title_key("Mary’s meadow")
    assert title_key("A Little Dinner at Timmins's") == title_key("A Little Dinner at Timmins s")
    assert authors_key(["Gabriele D'Annunzio"]) == authors_key(["D'Annunzio, Gabriele"])


def test_various_authors_spellings_are_one_author_unknown_is_none():
    various = ["AA.VV.", "Aa. Vv.", "AAVV", "A.V.", "V.A.", "Autori Vari", "Various Artists", "Various Authors", "Various"]
    assert len({authors_key([a]) for a in various}) == 1
    assert len({tuple(similar_authors_keys([a])) for a in various}) == 1
    assert authors_key(["Autore sconosciuto"]) == authors_key(["Unknown author"]) == frozenset()
    assert authors_key(["AA.VV."]) != authors_key(["Anonimo"])


def test_title_variants_drop_collection_number_and_author():
    from calibre_dedup.normalize import title_variants
    assert "brother jacob" in title_variants("(Gutenberg - 0411- Brother Jacob - George Eliot)", ["George Eliot"])
    assert "revolution and counter revolution" in title_variants(
        "(Gutenberg - 0708 -Revolution and Counter-Revolution - Friedrich Engels;Karl Marx)", ["Friedrich Engels", "Karl Marx"])
    assert "shifting winds" in title_variants("(Gutenberg Classics 2x033 2001 Dicembre - SHIFTING WINDS)", ["AA.VV."])
    assert "fahrenheit 451" in title_variants("Fahrenheit 451 - Ray Bradbury", ["Ray Bradbury"])
    assert title_variants("Jean-Paul", ["X"]) == ["jean paul"]  # a hyphen is not a separator


def test_contains_title_allows_only_noise_around():
    from calibre_dedup.normalize import contains_title, noise_words
    noise = noise_words("Walter Thornbury", "Gutenberg", "Mondadori")
    assert contains_title("1 haunted london", "haunted london", noise)
    assert contains_title("shifting winds inverno 2001", "shifting winds", noise)
    assert contains_title("gutenberg 0411 haunted london walter thornbury", "haunted london", noise)
    assert not contains_title("dune messiah", "dune", noise)
    assert not contains_title("fondazione e impero", "fondazione", noise)
    assert not contains_title("it 2", "it", noise)  # too short to look for


def test_edition_note_is_a_whole_word():
    assert title_key("(Gutenberg Classics 2x033 2001 Dicembre - SHIFTING WINDS)") != ""
    assert title_key("Naomi (Medallion)") == title_key("Naomi")  # a trailing imprint, not an edition
    assert title_key("Dune (2nd ed.)") == title_key("Dune") == title_key("Dune [Terza edizione]")


def test_names_one_letter_apart():
    from calibre_dedup.normalize import names_nearly_equal
    assert names_nearly_equal("Frederickk Marryat", "Marryat, Frederick")
    assert names_nearly_equal("Charles Kingsley", "Charles Kingsly")
    assert not names_nearly_equal("James Herbert", "Frank Herbert")
    assert not names_nearly_equal("John Neal", "John Neil")  # short names: another name
    assert not names_nearly_equal("Isaac Asimov", "Isaac Asimov")  # the same: nothing to add


@pytest.mark.parametrize("a,b,same", [
    ("E.Marshall", "Marshall, Emma", True),
    ("E. Marshall", "Emma Marshall", True),
    ("J.R.R. Tolkien", "John Ronald Reuel Tolkien", True),
    ("Tolkien, J. R. R.", "J.R.R. Tolkien", False),  # already the same key: not this rule's case
    ("A. Marshall", "Emma Marshall", False),  # the initial doesn't fit
    ("Marshall", "Emma Marshall", False),  # no initial: a missing first name is not enough
    ("E. Marshall", "Emma Anne Marshall", False),  # a part with nothing to stand for
    ("E. Smith", "Emma Marshall", False),  # different surname
    ("James Herbert", "Frank Herbert", False),
])
def test_initials_match(a, b, same):
    from calibre_dedup.normalize import initials_match
    assert initials_match(a, b) is same


@pytest.mark.parametrize("text,name", [
    ("F. Max Müller", True), ("Kingston", True), ("Rousseau, Jean-Jacques", True), ("A. B. Ellis", True),
    ("Ursula K. Le Guin", True), ("De Kock, Paul", True),
    ("Chasing the Sun", False), ("Volume 02", False), ("Il Nome Della Rosa", False),
    ("The Antiquary", False), ("Kate Aylesford SCAN", False), ("uploader", False), ("", False),
])
def test_looks_like_name(text, name):
    from calibre_dedup.normalize import looks_like_name
    assert looks_like_name(text) is name


@pytest.mark.parametrize("a,b,same", [
    ("Muller", "F. Max Müller", True),
    ("Rousseau, Jean-Jacques", "Rousseau", True),
    ("Eco", "Umberto Eco", False),  # too short: too many others
    ("Muller", "Müller", False),  # the same key: not this rule's case
    ("Herbert", "James Herbert Wells", True),
    ("Frank Herbert", "James Herbert", False),
])
def test_surname_match(a, b, same):
    from calibre_dedup.normalize import surname_match
    assert surname_match(a, b) is same


@pytest.mark.parametrize("title", ["ITABOOK 0052 - Hemingway", "il_vecchio_e_il_mare", "Moby Dick.epub", "scan0012",
                                   "B00ABC1234", "(Gutenberg - 0411- Brother Jacob - George Eliot)"])
def test_titles_made_from_file_names(title):
    from calibre_dedup.normalize import looks_like_file_name
    assert looks_like_file_name(title)


@pytest.mark.parametrize("title", ["1984", "Fahrenheit 451", "1Q84", "2001: Odissea nello spazio", "Catch-22",
                                   "Le 120 giornate di Sodoma", "Il conte di Montecristo (Vol. 2)", "Urania 1234"])
def test_titles_with_numbers_are_titles(title):
    from calibre_dedup.normalize import looks_like_file_name
    assert not looks_like_file_name(title)
