# CalibreDedup - an offline Duplicate Remover for Calibre Libraries

Version **0.1.0** · Released **2026-09-23** · Author **Antonio Romeo**
([antonioromeo@ilve.it](mailto:antonioromeo@ilve.it)) · [MIT License](LICENSE)

Moves books from a **source** Calibre library into a **target** library. Books
that already exist in the target go to a **trash** library instead. Books whose
identity can't be determined stay in the source. With the same library as source
and target, it finds the duplicates inside that library instead.
Uses AI to identify books when the metadata isn't enough.


## How it works

1. **Analyze (dry run).** Reads the three libraries' `metadata.db` files
   read-only. This is safe while Calibre is open. It then decides, for each
   source book:

   | Action | When |
   |---|---|
   | **Move to target** | No target book has the same title and authors, or every such book is a different edition or from a different publisher. |
   | **Merge & Trash** | A duplicate: a target book has the same title and authors, **and** the same edition and publisher (or the same ISBN or ASIN, the same EPUB text, or the same cover). The source book has formats the target copy lacks: they are added to the target copy (except PDF), then the source book goes to the trash library. |
   | **Trash only** | A duplicate with nothing to merge: the target copy already has all its formats. The source book goes to the trash library. |
   | **Leave in source** | Title or authors can't be determined, or edition or publisher can't be compared, even with AI, and neither the EPUB text nor the covers prove it's the same book. |

  When source and target are the same library, confirmed duplicate records
  are handled in place: the record with richer metadata is kept, missing
  formats from the other record are added to it, and the weaker record goes
  to the trash library. Records that are different or cannot be compared stay
  in the library. See [Finding duplicates within one library](#finding-duplicates-within-one-library).

  Books appear in the list as soon as they are decided. While the analysis runs
  you can already tick, untick and override them (the list is not sorted until
  the end); *Execute* waits for the analysis to end. During an execution the
  list is read-only. At the end the list is sorted by the column you chose. **Stop** ends the analysis
  after the current book. The books analyzed so far are listed and can be
  reviewed and executed; analyze again for the rest.

  A disk problem stops the analysis the same way: if the drive of a library
  disconnects or reports errors, the analysis ends with a message and the
  books analyzed so far stay listed. A single missing or locked file only
  leaves that book in the source ("file error").

2. **Review and choose.** Only books whose checkbox is ticked are acted on.
   Move and Trash rows start ticked. Leave rows can't be ticked unless you
   override their action.
   * **Filter** with the search box, where all words must match across title,
     authors, reason and match. You can also filter by action or turn on *AI
     used*, *Adds formats*, *Only checked*, *Only failed*, *Reduced checks* or *Decided by cover*
     (books decided with fewer checks than the settings ask for, see
     [Settings that can't take effect](#settings-that-cant-take-effect)). When source and
     target are the same library, *Hide books with no duplicate* (on by
     default) hides the books left in place because no other book shares their
     title and authors, or every such book is a different edition. Undecided
     books, and books whose title or authors couldn't be read, stay visible.
   * **Tick or untick in bulk:** *Check/Uncheck/Invert visible* acts on the
     rows the filter currently shows. **Space** toggles the selected rows.
   * **Right-click** to change a book's action:
     * *Force move to target*.
     * *Force Merge & Trash*: when the book has a match with formats it lacks:
       they are added to the match, then the book goes to the trash library.
     * *Force Trash only*: **always available**. The book goes to the trash
       library and nothing is merged. With no match at all, the book will then
       be only in the trash library; the Execute confirmation says how many
       such books there are.
     * *Keep in source* (*Keep in library* when source and target are the
       same library, where there is no move), or *Revert to analysis decision*.

     A book judged a *different edition* keeps that book as its match, shown
     as "(different edition)" in *Match in target*: forcing it to trash uses
     it, and *Open both* lets you compare first. Your choices, *Trash only*
     included, are remembered for the next analysis. Overridden rows are shown
     in italics.
   * **Right-click → Open this book / Open the match / Open both** opens the
     book files with the apps your system associates with them (EPUB is
     preferred when a book has several formats). This also works while an
     analysis or execution is running.
   * **Right-click → Copy title / Copy author** copies the right-clicked
     book's title or authors, as the list shows them (all authors, "A & B"),
     e.g. to search for it in Calibre.
   * A duplicate of a book that this same plan moves into the target is
     **blocked** (⚠) while that move is unticked or overridden.
   * Your unticked books and overrides are remembered per source/target pair
     and re-applied on the next analysis.
   * *Export CSV* saves the plan, including what is ticked.

3. **Execute.** Requires Calibre to be closed. The writes go through Calibre's
   own library code (`calibre-debug`), the same code as Calibre's *Copy to
   library*. Covers, custom columns and all other metadata are kept. A source
   book is removed only after its copy has been verified. By default, removed
   books go to the source library's own Calibre recycle bin. Books moved or
   trashed successfully are then removed from the list (the log keeps a line
   for each); failed, unticked and *Leave* rows stay. At the end, the
   folders of the removed books (and their author folders) are deleted if
   Windows left them behind empty; folders with files in them are never touched.

### Duplicate rules

**Which books are compared.** Only books with the same title and authors,
after normalization. Normalization ignores case, accents, punctuation and a
leading "The/A/An". An apostrophe separates words, so "L'inferno" matches "L
Inferno", as titles taken from file names often have it. It also strips
edition statements such as "(2nd Edition)" from titles, and treats "Tolkien,
J.R.R." and "J. R. R. Tolkien" as the same author. Every spelling of *various
authors* (AA.VV., AAVV, A.V., V.A., Autori vari, Various Artists, Various
Authors, Various) is one author, so anthologies match. An unknown author
("Unknown", "Sconosciuto", "Autore sconosciuto") is no author at all. A series, collection or imprint in brackets at the end of a title is
ignored too: "Il grande freddo (eLit)" is compared with "Il grande freddo".
Brackets that tell books apart are kept: a volume ("(Vol. 3)", "(Libro 2)",
"(II)", any number), a different content ("(Serie completa)", "(Antologia)",
"(versione ridotta)") or a language ("(Em Portuguese Do Brasil)"). Subtitles count unless *Ignore subtitles* is on.

With *Similar author matching* (Settings → Analysis, on by default), authors
are compared more loosely, like the "similar" algorithm of Calibre's Find
Duplicates plugin:

* initials and the words von, van, jr, sr, i, ii, iii, second, third, md and
  phd are ignored, so "Stephen E. King" matches "King, Stephen";
* **one shared author is enough**: "Dune" by Frank Herbert is compared with
  "Dune" by Frank Herbert & Brian Herbert.

Without it, all authors must match.

**Authors written differently** (option *Same title, author written
differently*, Settings → Analysis, on by default). When a book finds no book
with the same title and authors, books with **the same title** are checked for
an author that is the same person written differently:

* one letter apart in a part of the name of 5 letters or more: "Wilson Tucke" /
  "Wilson Tucker", "Edmond Hamiton" / "Edmond Hamilton" (no AI needed);
* otherwise the *Text AI* is asked, in English, whether the two names are the
  same person: another spelling or transliteration ("Dostoevskij" /
  "Fyodor Dostoyevsky"), or a pen name. The answers are cached. A small model
  may not know pen names.

Those books are then compared as usual (ISBN, edition and publisher, EPUB
text, covers), and the reason says why they were compared, e.g. "same person:
'Wilson Tucke' / 'Wilson Tucker' (one letter apart)".

**How they are compared**, in this order; the first rule that decides wins:

1. **A shared ISBN** means a duplicate. ISBNs come from metadata or from the
   book's pages. So does **a shared Amazon ASIN** (the `mobi-asin` or
   `amazon…` identifiers in Calibre), which Amazon gives to one edition.
2. **Otherwise edition and publisher are compared.** Edition is the edition
   number if both books have one, otherwise the publication year. Publishers
   are compared loosely: "O'Reilly Media, Inc." matches "O'Reilly".
   * Both the same: duplicate.
   * Either one different: a different book.
   * Either one unknown: if both books have an EPUB with **identical text**,
     it is a duplicate, without AI (see below). Otherwise the AI reads both
     books for the missing fields, and they are compared again.
   * **Only the years differ** (the publisher matches or is unknown): Calibre's
     publication date is often the original publication, not this edition's.
     With *Re-check year differences* (Settings → Analysis, on by default) the
     AI reads both books and the years printed in them decide. If it can't
     find a year in both, the metadata decision stands.

   Year and publisher are compared **like with like**: when the AI has read
   them in both books, those readings are compared; otherwise the metadata of
   both. The reason then says so, e.g. "same year (2005, read by AI)".
3. **Still undecided: the covers are compared** by the *Image AI* (if one is
   chosen and *Compare covers* is on). The same cover means a duplicate.
   Different, unclear or missing covers decide nothing, and the book stays in
   the source. Covers overrule a different edition or publisher only with
   *Always compare covers* (below).

**Always compare covers** (Settings → Analysis, off by default; needs an Image
AI). Covers are also compared when the metadata says the books differ, e.g.
when only the years differ because Calibre's date is the e-book's creation
date (2011) and the other book's is the edition's (1986). Different editions
normally have different covers, so the same cover makes a duplicate. Calibre's
cover (`cover.jpg` in the book folder) can be a picture downloaded by *Download
metadata*, shared by two editions, so **the covers stored inside the book files
must match too** (EPUB directly; MOBI, AZW3 and FB2 with Calibre's
`ebook-meta`). If a file has no cover inside, nothing is overruled. Such a
duplicate is proposed as **Trash only**: its formats are not added to the other
book, whose files may be another edition. The reason says what the metadata
differed on, and the **Decided by cover** filter lists every book the covers
decided, to check before executing. It costs more AI calls; answers are cached.

**Identical EPUB text.** The text files inside each EPUB (the chapters, not
the metadata or the images) are compared by fingerprint. If they are identical,
the two books are the same file with only its metadata or cover changed, e.g.
one copy with the publisher and ISBN filled in and another with a different
cover. This is checked only when edition or publisher can't be compared: it
never overrules a different edition or publisher. It needs an EPUB on both
sides; other formats are left to the AI.

**Same series and number** (option *Same author + same series + same number =
same book*, Settings → Analysis, off by default). When two books share an
author and have the same series name and the same number, they are the same
book, even if their titles differ ("Anno 2391" and "An 2391", both Urania
#243). They are compared even when the titles don't match. Number 1 is ignored:
it is Calibre's default when no number was set. Books without a series are not
affected. Turn it on only for libraries whose series numbers are reliable, such
as a collection numbered by issue; in libraries where a genre is used as the
series, or numbers are wrong, it would send different books to the trash.

With the option on, **source books without a series and a real number are left
untouched** ("no series number (series option on): left untouched"): nothing is
decided or executed for them, not even an obvious duplicate. So a library is
cleaned in two passes: first with the option on (books with a series number,
whatever their titles), then with it off (all the rest, as usual). These rows
are counted apart in the status bar ("N without series number") and hidden by
*Hide books with no duplicate*, which is shown whenever the option is on.

**Similar titles** (option *Match similar titles by the same author*, Settings
→ Analysis, on by default). When no book has the same title, books by the same
author are compared if both titles are made of the same title with only
"noise" around it: numbers, single letters, the authors, the series or
publisher of either book, the libraries' collections (series with 20 or more
books, such as Urania), seasons, months and issue words (NS, nr, speciale…).
Titles made from file names are first split at dashes, dropping the author,
bare numbers and a collection name followed by a number. So these match:

* "1 Abissi d'acciaio" and "Abissi d'acciaio";
* "(Urania - 0411- Supernormale - J. Hunter Holly)" and "Supernormale";
* "(Urania Millemondi 2x033 2001 Dicembre - ASTRONAVI MALEDETTE)" and
  "ASTRONAVI MALEDETTE Inverno 2001".

But "Dune Messiah" and "Dune" don't: "Messiah" is part of the title.

Titles that are only alike are weaker than the same title, so **only proof
makes a duplicate**: the same ISBN, ASIN or series number, identical EPUB text,
or the same cover (with *Compare covers* and an Image AI). A real difference in
edition or publisher (not only the year), or a different cover, rules the book
out: it is moved as usual. Anything else is left in the source, "similar title
to …, not proven the same book: check manually", with the match shown so you
can open both and force *Trash only* if they are the same.

**Different ISBNs alone decide nothing.** An e-book and a print book of the
same edition have different ISBNs.

A duplicate is **Merge & Trash** when it has formats the kept copy lacks
(except PDF): they are added to the kept copy first. Otherwise it is **Trash
only**.

### Finding duplicates within one library

With the same library as source and target, books are not compared with every
other book (that would be billions of pairs in a large library), nor with
themselves:

1. **Richest record first.** Books are sorted by how much metadata they have
   (formats, cover, description, publisher, year, ISBNs). So the first book of
   each group analyzed is the best record, and it is the one kept.
2. **Look up, then add.** Each book's title and authors give a key (with
   similar matching, one key per author). A dictionary from key to the books
   already analyzed gives its candidates in a single lookup. Only after its
   decision is the book added to that dictionary. So a book never meets
   itself, and each pair is compared only once.
3. **Trashed books leave the pool.** A duplicate sent to trash is not added,
   so a third copy is compared with the kept record, not with the trashed one.
   Books that stay (different editions, undecided) are added, so later copies
   are compared with all of them.

Example with three copies of "Dune", richest first:

| Book | Compared with | Result |
|---|---|---|
| A | nobody (first of its group) | kept |
| B | A | same ISBN: Merge & Trash into A |
| C | A only | same edition and publisher: Trash only |

AI is used only for pairs inside a group that metadata can't decide, and every
answer is cached. So AI calls grow with the undecided pairs, not with the size
of the library.

### When AI is used

AI is used only when the metadata isn't enough:

* The source book has no title or authors ("Unknown").
* A target book with the same title and authors exists, but edition or
  publisher is missing, and the two EPUBs don't have identical text. Both
  books are then read.
* Still undecided after that: the *Image AI* compares the two covers.

**How the text is extracted.** The text is taken from the book file on your
computer; only the extracted text goes to the *Text AI*, which returns title,
authors, publisher, edition, year and ISBN. One format per book is read, in
this order of preference: EPUB, KEPUB, AZW3, MOBI, AZW, PDF, FB2, DOCX, RTF,
HTMLZ, TXT, DJVU.

| Format | How | What is read |
|---|---|---|
| EPUB, KEPUB | read directly, chapters in reading order | the first (or last) 12,000 characters |
| PDF | Calibre's `pdftotext` | the first (or last) 6 pages |
| TXT | read directly | the first (or last) 12,000 characters |
| others (MOBI, AZW3, FB2, DOCX, ...) | converted to text with Calibre's `ebook-convert` | the first (or last) 12,000 characters |

The first pages (title page and copyright page) are read first; the last pages
only if fields are still missing. Pages and characters are set in Settings →
Analysis. A PDF with almost no text is taken as scanned: up to 4 pages are
rendered with `pdftoppm` and sent as images to the *Image AI*, if one is
chosen. There is no OCR. Files with DRM or that are damaged can't be read.

**Cache.** Every AI answer is kept in `~\.CalibreDedup\ai_cache.json`, so
analyzing again, or after *Stop*, is fast.

* A book is read again when its file changes (size or date). Switching the
  *Text AI* model does not re-read books.
* A cover comparison is asked again when either cover changes or the *Image
  AI* model changes.
* Errors (timeouts, connection problems, invalid replies) are not cached:
  those books are retried next time. An empty reply is not an error: some
  models answer nothing when the pages hold no metadata, so it is cached as
  "nothing found".
* To start over: Settings → Analysis → **Clear AI cache…** (small button at the bottom
  right; not while an analysis runs), or `--clear-cache` on the command line. Each program
  clears only its own cache (`ai_cache.json`, or `review_cache.json` for the review).

## Setup

Requires Python 3.10+ and Calibre installed. It was tested with Calibre 9.x.

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m pip install -e .
.venv\Scripts\python -m calibre_dedup          # GUI
```

On Windows, `launch_calibre_dedup.cmd` (and `launch_calibre_review.cmd`) starts the
program using the local `.venv` when it exists, or an available Python installation
otherwise. The window opens without a console and the launcher ends at once. With
`--cli` it runs in the console and waits. If the window never appears, start it from a
console (`.venv\Scripts\python -m calibre_dedup`) to see the error.

### AI providers (Settings → AI providers)

A profile describes one model: provider, server or endpoint, model name, key
and options. Tick *Supports images* when the model reads images as well as
text. *Test connection* then also sends a small test image and tells you
whether the model really saw it.

Which profile does what is chosen on the main window:

* **Text AI** reads book text to find missing metadata. *None* turns all AI
  off.
* **Image AI** is a model that reads text and images: it compares covers and
  reads scanned PDFs. Only profiles with *Supports images* are listed, and it
  needs a Text AI. It can be the same profile as the Text AI. *None* skips the
  cover check and scanned PDFs.

Each analysis logs what it uses, e.g. `AI: text = Ollama (gemma3:12b) · images =
Ollama (gemma3:12b) · cover check on`.

* **Ollama:** server URL (default `http://localhost:11434`) and model. *Refresh
  models* lists the models you have installed. Good choices are
  `qwen2.5:14b`, `llama3.1:8b` or `mistral-nemo`. For images, use a model that
  reads them, such as `qwen2.5vl`, `llama3.2-vision` or `gemma3`, and tick
  *Supports images*.
* **Azure OpenAI:** endpoint (`https://<resource>.openai.azure.com`),
  deployment name, API key and API version (e.g. `2024-10-21`). The API
  version `v1` uses the new `/openai/v1` API, where the deployment field holds
  the model name. For reasoning models (o-series, gpt-5), untick *Send
  temperature*.

**Advanced parameters.** Each profile can add parameters of your choice to
every request, for options the form doesn't have. The model must accept them;
the app doesn't know every model's options.

| Provider | Example | Effect |
|---|---|---|
| Ollama | `think` = `false` | No "thinking" before answering. With thinking models (qwen3.x) this is much faster (seconds instead of minutes) and avoids empty replies from a model that thinks until its context is full. |
| OpenAI, Azure | `reasoning_effort` = `none` or `low` | Less reasoning. Reasoning models only: others refuse the parameter. |
| Anthropic | — | Nothing needed: thinking is off unless requested. |

* A value is JSON when it parses as JSON (`false`, `1024`, `"text"`), otherwise
  plain text (`none`, `low`).
* A dot puts a parameter inside an object: `options.num_predict` = `1024`
  (Ollama).
* Not accepted: fields the form already has (model, temperature, context size)
  and fields the app sets itself (`messages`, `stream`, `format`,
  `response_format`, and for Anthropic `system` and `max_tokens`). The
  settings can't be saved with an invalid or refused parameter.
* The log shows the parameters sent with each call (`extra=think=false`).

**Test connection** sends one real metadata request with all the profile's
settings, advanced parameters included, on a made-up copyright page (with a
translator, an original title and a later edition as traps). It reports:
a parameter refused by the server, an empty or cut-off reply (and why), a slow
answer, whether the model still "thought", and each value read, right (✓) or
wrong (✗). Ollama silently ignores parameter names it doesn't know: a
misspelled `thinking` = `false` has no effect, and the test shows the model
still thinking. With *Supports images*, it also checks that the model sees a
test image. The report is coloured: green for values read right, red for
problems, orange for notes worth checking (a slow answer; with advanced
parameters, the reminder that a model may ignore or refuse a parameter it
doesn't support).

**When the request is refused** (the server answers with an error), the test
finds the cause by asking the server again, so it works with any provider
whatever its error format: once without advanced parameters (if that fails
too, the parameters aren't the cause: check the model, key and URL), then with
each parameter alone. Each is marked accepted (✓) or refused (✗, with the
server's message); if all are accepted alone, it's the combination. A timeout
or connection failure is not a refusal and starts no search.

Whatever fails, the report shows the actual error as the server or the
network gave it: the server's error message, the page a wrong URL returns, the
model's reply when it isn't valid JSON, and the model's answer when it doesn't
see the test image.

The test page is built into the program (`TEST_EXCERPT` in `ai.py`), not taken
from your books, so the model can't answer from memory. Its correct values:
*Il guardiano del faro*, by Elena Marchetti (the translator Paolo Bianchi is
not an author), Edizioni Lanterna, third edition (2021; the first was 2019),
ISBN 978-88-7000-123-4.

API keys are stored in the Windows Credential Manager through `keyring`. You
can also set the `CDR_API_KEY` environment variable.

### Settings that can't take effect

The program never changes a setting by itself. When one is on but can't work,
it tells you:

* **In the window:** a *None* choice, or an Image AI while the Text AI is
  *None*, is shown in amber italics. In *Settings*, an option that is ticked
  but can't run says why, e.g. *inactive: no Image AI selected*.
* **Before an analysis:** *Analyze* lists what won't take effect (e.g. the
  cover check with no Image AI) and whether a local Ollama server is
  unreachable or lacks the model. It offers to use a profile that reads images
  as the Image AI. You can hide a warning for good, except an unreachable AI;
  *Settings → Show dismissed warnings again* brings them back.
* **When an AI stops responding** (3 errors in a row), the analysis pauses and
  asks: *Retry*, *Continue without* that AI for the rest of this run, or
  *Stop*. The next analysis tries it again. A failing Image AI never stops the
  Text AI.
* **After an analysis:** each row's reason says what was skipped (e.g.
  *cover check skipped: no Image AI*), the *Reduced checks* filter lists those
  books, and an amber bar above the table sums it up, together with an AI that
  stopped responding. The status line counts what the AI did (pages read,
  covers compared, year re-checks).
* **Settings changed since the analysis:** the amber bar says which ones, so
  you know to analyze again (fast: AI answers are cached).

## Command line

```powershell
python -m calibre_dedup --cli --source D:\Books\Inbox --target D:\Books\Main --trash D:\Books\Dupes --report plan.csv
python -m calibre_dedup --cli ... --execute        # perform the moves
python -m calibre_dedup --cli ... --text-profile "Azure gpt-4o" --image-profile ""
python -m calibre_dedup --cli ... --no-ai          # metadata only
```

Options you leave out are taken from the GUI's saved settings.

## Calibre Metadata Review (calibre-review)

A second program in the same package: it reads **every** book of one library with the AI
and proposes corrections to **title, authors, publisher, year and series (with its
number)**. Start it with `launch_calibre_review.cmd`, `python -m calibre_dedup.review_app`
or `calibre-review`.

* **Libraries:** the library to review, and the trash library for the books removed
  (created if it is an empty folder).
* **What is read:** the first pages (PDF: "PDF pages to read", default 6; other formats:
  "Characters to read") and, with an **Image AI**, the cover: Calibre's `cover.jpg`, else
  the cover inside the file. With an Image AI every book goes to it (text + cover + scanned
  pages); without one, the text AI reads the text only and scanned PDFs are skipped.
* **The list** shows two lines per book where the AI read something different: the
  current value, then `→ proposed value` in green. Case, accents, punctuation, author
  order and publisher suffixes ("Editore", "S.p.A.") are not differences, and a field the AI
  did not find is never proposed (nothing is erased). "Only with differences" is on by
  default; the cover of the selected book is shown on the right.
* **Choosing:** books with differences are proposed for **Update** and checked. The
  **Change:** boxes turn a field on or off for all books; right-click turns one field off
  for the selected books ("Don't change publisher", shown struck through), or sets the
  action: Update / Keep as it is / Move to the trash library.
* **Execute** (Calibre closed) writes the checked updates inside Calibre (a new year keeps
  the date's month and day) and moves the checked trash books to the trash library, then
  removes them from the reviewed library (recycle bin, or permanently if set).
* **Books with no files** (a record only, nothing to read) are proposed for **Trash**, checked:
  Execute moves them to the trash library. Books whose files are listed but can't be read
  stay "not read" (check them by hand).
* **Continue another day, on any computer:** every book whose metadata is updated also gets
  the tag **`AIReviewed`**, and the next scan skips books with that tag ("Skip books tagged
  AIReviewed", on by default; `--include-reviewed` on the command line). So you can stop a
  scan at any point, execute what you have, and continue later. The mark is in the library
  itself, not in the cache. Only updated books are tagged: books with no differences, or
  that you keep, are read again by the next scan (quickly on the same computer, from the
  cache). To review a book again, remove the tag in Calibre (tags can be removed in bulk).
  Updated and trashed books are taken off the list after Execute.
* **Own settings and cache:** the review keeps its settings in `review_settings.json` and its
  AI answers in `review_cache.json`, so it can run at the same time as the duplicate
  remover. The first time, both start as a copy of the duplicate remover's
  (`settings.json`, `ai_cache.json`): same AI profiles, trash library, Calibre folder and
  reading limits (Settings → "Reading" tab). After that, a change in one program doesn't
  reach the other. API keys stay shared (they are stored per profile name). AI answers
  are cached per book and cover, so a second scan is quick, also after an update renamed
  the book's files.
* **Both programs at once:** scans and analyses can run side by side. Executions take
  turns: while one program executes, the other's Execute is refused until it is done.

```powershell
python -m calibre_dedup.review_app --cli --library D:\Books\Main            # dry run: prints differences
python -m calibre_dedup.review_app --cli ... --fields title,authors --execute  # write only these fields
```

## Notes

* Calibre needs library paths shorter than 89 characters. Target and trash
  libraries are created if they are empty folders.
* Books with DRM, or damaged files that can't be read, stay in the source when
  AI is needed to identify them.
* All data lives in `~\.CalibreDedup` (e.g. `C:\Users\<you>\.CalibreDedup`), never in the
  program folder: `settings.json` (libraries, options, AI profiles), `ai_cache.json`,
  `selections.json` (remembered ticks/overrides) and `calibre_dedup.log`; for the review,
  `review_settings.json`, `review_cache.json` and `calibre_review.log`. API keys are kept
  in the Windows Credential Manager, not in files. Data from older versions in
  `%APPDATA%\CalibreDuplicateRemover` is moved there automatically.

## Tests

```powershell
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m pytest
```
