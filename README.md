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
     used*, *Adds formats*, *Only checked* or *Only failed*. When source and
     target are the same library, *Hide books with no duplicate* (on by
     default) hides the books left in place because no other book shares their
     title and authors, or every such book is a different edition. Undecided
     books, and books whose title or authors couldn't be read, stay visible.
   * **Tick or untick in bulk:** *Check/Uncheck/Invert visible* acts on the
     rows the filter currently shows. **Space** toggles the selected rows.
   * **Right-click** to *Force move to target*, *Force Merge & Trash* or
     *Force Trash only* (only when a matching target book exists; which one
     depends on whether the book has formats to merge), *Keep in source*, or
     *Revert to analysis decision*. When source and target are the same
     library there is no move, and *Keep in source* reads *Keep in library*.
     Overridden rows are shown in italics.
   * **Right-click → Open this book / Open the match / Open both** opens the
     book files with the apps your system associates with them (EPUB is
     preferred when a book has several formats). This also works while an
     analysis or execution is running.
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
leading "The/A/An". It also strips edition statements such as "(2nd Edition)"
from titles, and treats "Tolkien, J.R.R." and "J. R. R. Tolkien" as the same
author. A series, collection or imprint in brackets at the end of a title is
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
   the source. Covers never overrule a different edition or publisher.

**Identical EPUB text.** The text files inside each EPUB (the chapters, not
the metadata or the images) are compared by fingerprint. If they are identical,
the two books are the same file with only its metadata or cover changed, e.g.
one copy with the publisher and ISBN filled in and another with a different
cover. This is checked only when edition or publisher can't be compared: it
never overrules a different edition or publisher. It needs an EPUB on both
sides; other formats are left to the AI.

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
* To start over, close the program and delete `ai_cache.json`.

## Setup

Requires Python 3.10+ and Calibre installed. It was tested with Calibre 9.x.

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m pip install -e .
.venv\Scripts\python -m calibre_dedup          # GUI
```

On Windows, `launch_calibre_dedup.cmd` starts the program using the local
`.venv` when it exists, or an available Python installation otherwise.

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
Ollama (gemma3:12b) · cover check on`. If the Image AI fails 3 times in a row,
only images are switched off for the rest of the run; the Text AI keeps going.

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

## Command line

```powershell
python -m calibre_dedup --cli --source D:\Books\Inbox --target D:\Books\Main --trash D:\Books\Dupes --report plan.csv
python -m calibre_dedup --cli ... --execute        # perform the moves
python -m calibre_dedup --cli ... --text-profile "Azure gpt-4o" --image-profile ""
python -m calibre_dedup --cli ... --no-ai          # metadata only
```

Options you leave out are taken from the GUI's saved settings.

## Notes

* Calibre needs library paths shorter than 89 characters. Target and trash
  libraries are created if they are empty folders.
* Books with DRM, or damaged files that can't be read, stay in the source when
  AI is needed to identify them.
* All data lives in `~\.CalibreDedup` (e.g. `C:\Users\<you>\.CalibreDedup`), never in the
  program folder: `settings.json` (libraries, options, AI profiles), `ai_cache.json`,
  `selections.json` (remembered ticks/overrides) and `calibre_dedup.log`. API keys are kept
  in the Windows Credential Manager, not in files. Data from older versions in
  `%APPDATA%\CalibreDuplicateRemover` is moved there automatically.

## Tests

```powershell
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m pytest
```
