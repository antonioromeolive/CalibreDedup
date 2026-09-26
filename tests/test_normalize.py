import pytest

from calibre_dedup.normalize import (
    authors_key, is_unknown, normalize_isbn, parse_edition_number, same_publisher,
    similar_author_key, similar_authors_keys, title_key,
)


def test_title_key_ignores_case_punctuation_articles_and_edition():
    assert title_key("The Pragmatic Programmer (2nd Edition)") == title_key("pragmatic programmer")
    assert title_key("Dune!") == title_key("dune")
    assert title_key("Clean Code: A Handbook") != title_key("Clean Code")
    assert title_key("Clean Code: A Handbook", ignore_subtitle=True) == title_key("Clean Code")


def test_title_key_ignores_a_bracket_left_open_by_a_cut_title():
    assert title_key("Piccole donne crescono (Italia") == title_key("Piccole donne crescono (Italian Edition)")
    assert title_key("Piccole donne crescono [ediz") == title_key("Piccole donne crescono")
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


def test_normalize_isbn():
    assert normalize_isbn("0-306-40615-2") == "9780306406157"
    assert normalize_isbn("978-0-306-40615-7") == "9780306406157"
    assert normalize_isbn("978-0-306-40615-8") is None
    assert normalize_isbn("garbage") is None


def test_is_unknown():
    assert is_unknown("Unknown")
    assert is_unknown("Sconosciuto")
    assert is_unknown("  ")
    assert not is_unknown("Dune")


def test_title_key_ignores_a_series_or_imprint_in_closing_brackets():
    assert title_key("Il grande freddo (eLit)") == title_key("Il Grande Freddo")
    assert title_key("La targa (VINTAGE) (Italian Ed") == title_key("La Targa")
    assert title_key("Fratelli d'Italia (Gli Adelphi) [Oscar]") == title_key("Fratelli d'Italia")
    assert title_key("Il grande freddo (eLit) (2nd Edition)") == title_key("Il grande freddo")
    # a collection's issue number is not a volume
    assert title_key("Strada Senza Fine (Urania 0842)") == title_key("Strada Senza Fine")
    assert title_key("Il segno (Il Giallo Mondadori 2345)") == title_key("Il segno")
    # doubled brackets
    assert title_key("Il nemico di nebbia ( (Urania 332))") == title_key("Il Nemico Di Nebbia")
    assert title_key("Caccia alla fenice ( (Urania) (Italian Edition))") == title_key("Caccia alla fenice")
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
    assert title_key("Non posso amarti (Serie Completa)") != title_key("Non posso amarti")
    assert title_key("Racconti (Antologia)") != title_key("Racconti")
    assert title_key("Teoria Estetica (Em Portuguese Do Brasil)") != title_key("Teoria estetica")
    assert title_key("I promessi sposi (versione ridotta)") != title_key("I promessi sposi")


def test_apostrophe_separates_words():
    from calibre_dedup.normalize import title_key
    assert title_key("L'inferno a rovescio") == title_key("L Inferno A Rovescio") == title_key("L’Inferno a rovescio")
    assert title_key("Pianeti dell'Impossibile") == title_key("Pianeti dell Impossibile")
    assert authors_key(["Gabriele D'Annunzio"]) == authors_key(["D'Annunzio, Gabriele"])


def test_various_authors_spellings_are_one_author_unknown_is_none():
    various = ["AA.VV.", "Aa. Vv.", "AAVV", "A.V.", "V.A.", "Autori Vari", "Various Artists", "Various Authors", "Various"]
    assert len({authors_key([a]) for a in various}) == 1
    assert len({tuple(similar_authors_keys([a])) for a in various}) == 1
    assert authors_key(["Autore sconosciuto"]) == authors_key(["Unknown author"]) == frozenset()
    assert authors_key(["AA.VV."]) != authors_key(["Anonimo"])


def test_title_variants_drop_collection_number_and_author():
    from calibre_dedup.normalize import title_variants
    assert "supernormale" in title_variants("(Urania - 0411- Supernormale - J. Hunter Holly)", ["J. Hunter Holly"])
    assert "dalle fogne di chicago" in title_variants(
        "(Urania - 0708 -Dalle Fogne Di Chicago - Theodore L. Thomas;Kate Wilhelm)", ["Theodore L. Thomas", "Kate Wilhelm"])
    assert "astronavi maledette" in title_variants("(Urania Millemondi 2x033 2001 Dicembre - ASTRONAVI MALEDETTE)", ["AA.VV."])
    assert "fahrenheit 451" in title_variants("Fahrenheit 451 - Ray Bradbury", ["Ray Bradbury"])
    assert title_variants("Jean-Paul", ["X"]) == ["jean paul"]  # a hyphen is not a separator


def test_contains_title_allows_only_noise_around():
    from calibre_dedup.normalize import contains_title, noise_words
    noise = noise_words("Isaac Asimov", "Urania", "Mondadori")
    assert contains_title("1 abissi d acciaio", "abissi d acciaio", noise)
    assert contains_title("astronavi maledette inverno 2001", "astronavi maledette", noise)
    assert contains_title("urania 0411 abissi d acciaio isaac asimov", "abissi d acciaio", noise)
    assert not contains_title("dune messiah", "dune", noise)
    assert not contains_title("fondazione e impero", "fondazione", noise)
    assert not contains_title("it 2", "it", noise)  # too short to look for


def test_edition_note_is_a_whole_word():
    assert title_key("(Urania Millemondi 2x033 2001 Dicembre - ASTRONAVI MALEDETTE)") != ""
    assert title_key("Gli occhi (Medusa)") == title_key("Gli occhi")  # a trailing imprint, not an edition
    assert title_key("Dune (2nd ed.)") == title_key("Dune") == title_key("Dune [Terza edizione]")


def test_names_one_letter_apart():
    from calibre_dedup.normalize import names_nearly_equal
    assert names_nearly_equal("Wilson Tucke", "Tucker, Wilson")
    assert names_nearly_equal("Harry Harrison", "Harry Harrisson")
    assert not names_nearly_equal("James Herbert", "Frank Herbert")
    assert not names_nearly_equal("Bob Shaw", "Bob Shay")  # short names: another name
    assert not names_nearly_equal("Isaac Asimov", "Isaac Asimov")  # the same: nothing to add


@pytest.mark.parametrize("a,b,same", [
    ("M.Scott", "Scott, Melissa", True),
    ("M. Scott", "Melissa Scott", True),
    ("J.R.R. Tolkien", "John Ronald Reuel Tolkien", True),
    ("Tolkien, J. R. R.", "J.R.R. Tolkien", False),  # already the same key: not this rule's case
    ("A. Scott", "Melissa Scott", False),  # the initial doesn't fit
    ("Scott", "Melissa Scott", False),  # no initial: a missing first name is not enough
    ("M. Scott", "Melissa Anne Scott", False),  # a part with nothing to stand for
    ("M. Smith", "Melissa Scott", False),  # different surname
    ("James Herbert", "Frank Herbert", False),
])
def test_initials_match(a, b, same):
    from calibre_dedup.normalize import initials_match
    assert initials_match(a, b) is same
