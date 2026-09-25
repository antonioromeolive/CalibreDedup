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
