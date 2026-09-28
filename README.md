# CalibreDedup: tidy up Calibre libraries, offline, with AI when needed

Version **0.1.0** · Released **2026-09-23** · Author **Antonio Romeo** (with Claude Code et al.)
([antonioromeo@ilve.it](mailto:antonioromeo@ilve.it)) · [MIT License](LICENSE)

This package holds **two programs** for [Calibre](https://calibre-ebook.com) e-book
libraries. Both work on your own computer, read your libraries without changing them until
you press *Execute*, and can use an AI (local with Ollama, or in the cloud) when a book's
metadata isn't enough.

| | **Duplicate Remover** (`calibre-dedup`) | **Metadata Review** (`calibre-review`) |
|---|---|---|
| **Job** | Brings books from one library into another **without creating duplicates**, or finds the duplicates inside one library. | Checks **every book** of one library and proposes corrections to its **title, authors, publisher, year and series**. |
| **Libraries** | a source, a target and a trash library | the library to review and a trash library |
| **Result** | each book is **moved**, **sent to the trash library** (a duplicate) or **left** where it is | each book is **updated**, **kept** as it is or **sent to the trash library** |
| **AI** | only when the metadata can't decide (few books) | on every book (it reads the first pages and the cover) |
| **Start** | `launch_calibre_dedup.cmd` | `launch_calibre_review.cmd` |

Both programs work the same way, in three steps:

1. **Analyze** (button *1. Analyze (dry run)*, or *1. Scan with AI* in the review):
   read-only, safe while Calibre is open. Books appear in a list as soon as they are
   decided, each with the proposed action and the reason.
2. **Review** the list: filter it, tick or untick books, change any proposal.
3. **Execute** (button *2. Execute checked*): only the ticked books, with Calibre closed.

**Your books are never lost.** A "deleted" book is copied, whole (files, cover, metadata,
custom columns), into a **trash library**, a normal Calibre library you choose, and only then
removed from its library, into Calibre's own recycle bin by default. Every write goes
through Calibre's own library code, and a copy is verified before the original is removed.
Nothing is converted or repaired: the files stay as they are.

**Contents**

- [Installation](#installation)
- [Duplicate Remover](#duplicate-remover)
- [Metadata Review](#metadata-review)
- [AI](#ai)
- [Data files and logs](#data-files-and-logs)
- [Tests](#tests)
- [Appendix: AI prompts](#appendix-ai-prompts)


## Installation

Requires Windows, Python 3.10+ and Calibre (tested with Calibre 9.x).

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m pip install -e .
```

**Starting.** On Windows, `launch_calibre_dedup.cmd` and `launch_calibre_review.cmd` start
the programs with the local `.venv` when it exists, or an available Python installation
otherwise. The window opens without a console and the launcher ends at once; with `--cli`
the program runs in the console instead (see each program's *Command line*). The same from
Python: `python -m calibre_dedup` and `python -m calibre_dedup.review_app` (or the
`calibre-dedup` and `calibre-review` commands). If a window never appears, start it from a
console to see the error.

**Libraries.** Calibre needs library paths shorter than 89 characters. A target or trash
library is created when you choose an empty folder.

**Both programs at once.** Analyses and scans can run side by side. Executions take turns:
while one program executes, the other's *Execute* waits until it is done.


## Duplicate Remover

### Three ways to use it

- **Source → target** (the main use): books arriving in a *source* library (downloads, an
  old collection) are moved into your main *target* library. Books the target already has go
  to the **trash library** instead; books whose identity can't be established stay in the
  source for you to check.
- **Within one library**: choose the same library as source and target. Its duplicates go to
  the trash library; one copy of each book stays (see
  [Duplicates within one library](#duplicates-within-one-library)).
- **Cleanup source only** (the box under the libraries): nothing is written to the target.
  Only the source books the target already has go to the trash library; everything else stays
  in the source. A duplicate whose target copy lacks one of its formats stays too, since
  trashing it would lose that format from both libraries: right-click → *Merge & Trash* to
  add the format to the target copy first, or leave it.

### Step 1: Analyze

**1. Analyze (dry run)** reads the libraries' `metadata.db` files read-only and decides, for each source
book:

| Action | When |
|---|---|
| **Move to target** | The target has no book with the same title and authors, or only different editions of it. |
| **Merge & Trash** | A duplicate that has formats the target copy lacks: they are added to the target copy (except PDF), then the book goes to the trash library. |
| **Trash only** | A duplicate with nothing to add: the book goes to the trash library. |
| **Leave in source** | The program can't be sure: title or authors unknown, or edition and publisher can't be compared, even with AI. The reason says why, and the likely match is shown. |

How a duplicate is recognised is explained in [How duplicates are found](#how-duplicates-are-found).

- Rows can be ticked, unticked and overridden while the analysis runs (the list is sorted at
  the end); *Execute* waits for it to finish.
- **Stop** ends the analysis after the current book. The books analyzed so far can be
  reviewed and executed; analyze again for the rest (fast: AI answers are cached).
- If a library's drive disconnects or reports errors, the analysis stops the same way, with
  a message. A single missing or locked file only leaves that book in the source ("file
  error").

### Step 2: Review the list

Only **ticked** books are acted on. *Move* and *Trash* rows start ticked; *Leave* rows can't
be ticked unless you change their action.

- **Filter** with the search box (all words must match, across title, authors, reason and
  match), by action, or with:
  - *AI used*, *Adds formats*, *Only checked*, *Only failed*;
  - *Reduced checks*: books decided with fewer checks than the settings ask for (see
    [When a setting can't take effect](#when-a-setting-cant-take-effect));
  - *Decided by cover*: books a cover comparison decided (see *Always compare covers*);
  - *Unreadable files*: books with files Calibre can't open (see
    [Files Calibre can't open](#files-calibre-cant-open));
  - *Hide books with no duplicate* (within one library, on by default): hides the books left
    in place because no other book shares their title and authors.
- **Tick in bulk:** *Check visible*, *Uncheck visible* and *Invert visible* act on the rows
  the filters show. **Space** toggles the selected rows.
- **Right-click** a row (or several) to change the action:
  - *Force move to target*;
  - *Force Merge & Trash*, when the book has a match lacking some of its formats;
  - *Force Trash only*, always available. With no match at all the book will then exist only
    in the trash library; the Execute confirmation counts such books;
  - *Keep in source* (*Keep in library* within one library);
  - *Move the unreadable formats to the trash library* / *Keep the unreadable formats*;
  - *Revert to analysis decision*.

  A book judged a *different edition* keeps that book as its match, shown as "(different
  edition)" in *Match in target*: forcing it to the trash uses it. Changed rows are shown in
  italics.
- **Right-click → Open this book / Open the match / Open both** opens the files with the
  apps Windows associates with them (EPUB preferred), to compare before deciding. Works
  during an analysis or execution too.
- **Right-click → Copy title / Copy author** copies what the list shows, e.g. to search in
  Calibre.
- A duplicate of a book this same plan moves into the target is **blocked** (⚠) while that
  move is unticked or overridden.
- Your unticked books and changes are **remembered** per source/target pair and applied
  again at the next analysis.
- *Export CSV* saves the plan, including what is ticked.

An amber bar above the list tells you what to know about the plan: checks that were skipped,
an AI that stopped responding, settings changed since the analysis.

### Step 3: Execute

**2. Execute checked** requires Calibre to be closed. The writes go through Calibre's own library code
(`calibre-debug`), the same as Calibre's *Copy to library*: covers, custom columns and all
other metadata are kept. A source book is removed only after its copy has been verified, by
default into the source library's Calibre recycle bin (Settings → Analysis → *Delete
permanently from source* to skip it).

With *Write AI-found title/authors/publisher/ISBN to moved books* (on by default), the
values the AI read are written to the moved copy, **only into empty fields**.

Books moved or trashed successfully leave the list (the log keeps a line for each); failed,
unticked and *Leave* rows stay. At the end, book and author folders that Windows left behind
empty are deleted; folders with files in them are never touched.

### How duplicates are found

#### Which books are compared

Only books with the **same title and authors**, after normalization:

- case, accents, punctuation and a leading "The/A/An" are ignored; an apostrophe separates
  words ("Mary's" = "Mary s", as titles from file names often have it);
- edition statements such as "(2nd Edition)" are removed from titles;
- "Tolkien, J.R.R." and "J. R. R. Tolkien" are the same author;
- every spelling of *various authors* (AA.VV., AAVV, A.V., V.A., Autori vari, Various
  Artists, Various Authors, Various) is one author, so anthologies match; an unknown author
  ("Unknown", "Sconosciuto", "Autore sconosciuto") counts as no author;
- a series, collection or imprint in brackets at the end of a title is ignored ("Rookwood
  (Everyman)" = "Rookwood"), but brackets that tell books apart are kept: a
  volume ("(Vol. 3)", "(Libro 2)", "(II)", any number), a different content ("(Serie
  completa)", "(Antologia)", "(versione ridotta)") or a language ("(Em Portuguese Do
  Brasil)");
- subtitles count, unless *Ignore subtitles* is on.

With *Similar author matching* (on by default), authors are compared more loosely, like the
"similar" algorithm of Calibre's *Find Duplicates* plugin: initials and the words von, van,
jr, sr, i, ii, iii, second, third, md and phd are ignored ("Stephen E. King" = "King,
Stephen"), and **one shared author is enough** ("Dune" by Frank Herbert is compared with
"Dune" by Frank Herbert & Brian Herbert). Without it, all authors must match.

Three options find more pairs (below): *Same title, author written differently*, *Match
similar titles by the same author* and *Same author + same series + same number*.

#### How two books are compared

In this order; the first rule that decides wins:

1. **The same ISBN** means a duplicate (ISBNs come from the metadata or from the book's
   pages). So does **the same Amazon ASIN** (the `mobi-asin` or `amazon…` identifiers),
   which Amazon gives to one edition. Different ISBNs alone decide nothing: an e-book and a
   print book of the same edition have different ISBNs.
2. **Edition and publisher.** The edition is the edition number when both books have one,
   otherwise the publication year. Publishers are compared loosely: "O'Reilly Media, Inc." =
   "O'Reilly", "Arnoldo Mondadori Editore" = "A. Mondadori", "DeAgostini Periodici S.r.l." =
   "De Agostini periodici".
   - Both the same: duplicate. Either one different: a different book.
   - Either one unknown: if both books have an EPUB with **identical text**, they are
     duplicates, without AI. Otherwise the AI reads both books for the missing fields, and
     they are compared again.
   - **Only the years differ**: Calibre's date is often the original publication, not this
     edition's. With *Re-check year differences* (on by default) the AI reads both books and
     the years printed in them decide; if it can't find a year in both, the metadata
     decision stands.

   Years and publishers are compared **like with like**: the AI's readings of both books,
   or else the metadata of both. The reason says so, e.g. "same year (2005, read by AI)".
3. **Still undecided: the covers.** With *Compare covers* on and an Image AI, the same cover
   means a duplicate. Different, unclear or missing covers decide nothing: the book stays in
   the source.

A duplicate is **Merge & Trash** when it has formats the kept copy lacks (except PDF), which
are added to the kept copy first; otherwise it is **Trash only**.

**Identical EPUB text.** The text files inside each EPUB (the chapters, not the metadata or
the images) are compared by fingerprint. Identical text means the same file with only its
metadata or cover changed. It is checked only when edition or publisher can't be compared,
never to overrule a difference, and needs an EPUB on both sides.

#### Options that find more duplicates

All in Settings → Analysis.

**Always compare covers** (off by default; needs an Image AI). Covers are compared also when
the metadata says the books differ, e.g. only the years differ because one date is the
e-book's creation (2011) and the other the edition's (1986). Different editions normally
have different covers, so with the same title and authors **the same cover makes a
duplicate**, whatever the year or publisher. For *similar titles* (below) the title is weaker
proof, and Calibre's `cover.jpg` may be a picture downloaded by *Download metadata*, so there
the covers **stored inside the files** must match too (EPUB directly; MOBI, AZW3 and FB2 with
Calibre's `ebook-meta`). Such a duplicate is **Trash only**: its formats are not added to the
other copy, whose files may be another edition. The reason says what the metadata differed
on; the *Decided by cover* filter lists these books to check before executing. It costs more
AI calls (cached).

**Same title, author written differently** (on by default). A book with no match is checked
against books with **the same title** whose author may be the same person written
differently: one letter apart in a name part of 5 letters or more ("Frederickk Marryat" / "Frederick
Marryat", no AI), initials, or else the Text AI is asked (a transliteration such as
"Dostoevskij" / "Fyodor Dostoyevsky", or a pen name; a small model may not know pen names).
The books are then compared as usual, and the reason says why, e.g. "same person: 'Frederickk
Marryat' / 'Frederick Marryat' (one letter apart)".

**Match similar titles by the same author** (on by default). With no book of the same
title, books by the same author are compared when both titles are the same title with only
"noise" around it: numbers, single letters, the authors, the series or publisher, the
libraries' collections (series with 20 or more books, such as Gutenberg), seasons, months and
issue words (NS, nr, speciale…). Titles made from file names are first split at dashes,
dropping the author, bare numbers and a collection name followed by a number. So these match:

- "1 Haunted London" and "Haunted London";
- "(Gutenberg - 0411- Brother Jacob - George Eliot)" and "Brother Jacob";
- "(Gutenberg Classics 2x033 2001 Dicembre - SHIFTING WINDS)" and "SHIFTING WINDS
  Inverno 2001".

But "Dune Messiah" and "Dune" don't: "Messiah" is part of the title. Alike titles are weaker
than the same title, so **only proof makes a duplicate**: the same ISBN, ASIN or series
number, identical EPUB text, or the same cover. A real difference in edition or publisher
(not only the year), or a different cover, rules the book out: it is moved. Anything else is
left in the source, "similar title to …, not proven the same book: check manually", with the
match shown so you can open both.

**Same author + same series + same number = same book** (off by default). Two books sharing
an author, a series and a number are the same book even if their titles differ ("Chasing the Sun"
and "Chasing", both Gutenberg #243). Number 1 is ignored: it is Calibre's default. Turn it on only
for libraries whose series numbers are reliable (a collection numbered by issue): where a
genre is used as the series, or numbers are wrong, it would trash different books. While it
is on, **source books without a series and a real number are left untouched**, not even an
obvious duplicate is handled, so a library is cleaned in two passes: first with the option on,
then off. These rows are counted apart ("N without series number") and hidden by *Hide books
with no duplicate*.

#### Duplicates within one library

Books are not compared with every other book (billions of pairs in a large library):

1. **Best copy first.** Books are sorted by format, then by how much metadata they have
   (formats, cover, description, publisher, year, ISBNs). A book with an EPUB comes first,
   then MOBI, then AZW/AZW3, then the rest. The first copy of each group is the one kept.
2. **Look up, then add.** Each book's title and authors give a key; a dictionary from key to
   the books already analyzed gives its candidates in one lookup, and the book is added only
   after its decision. So a book never meets itself, and each pair is compared once.
3. **Trashed books leave the pool.** A third copy is compared with the kept one, not with a
   trashed one. Books that stay (different editions, undecided) are added.

| Book (best first) | Compared with | Result |
|---|---|---|
| A | nobody (first of its group) | kept |
| B | A | same ISBN: Merge & Trash into A |
| C | A only | same edition and publisher: Trash only |

AI is used only for the pairs metadata can't decide, and every answer is cached, so AI calls
grow with the undecided pairs, not with the size of the library.

### Files Calibre can't open

A format Calibre doesn't read (DOC, JPG…), a file that isn't what its format says (a "PDF"
that is really a LIT book or a picture), or a file that fails to open, is **unreadable**.
Unreadable files are never read, converted or repaired, and never count in a comparison.

- A book with **only** unreadable files is proposed for the trash library ("no file Calibre
  can open").
- A book with **some**: it is decided on its other files. When its unreadable formats go to
  the trash, the whole record is first copied to the trash library as it is, then those
  formats are removed from the book. Right-click to keep them instead.

With *Move files Calibre can't open to the trash library without asking* (off by default)
these rows start ticked; otherwise they are listed (filter *Unreadable files*) for you to
decide.

### Settings (Settings → Analysis)

| Setting | Default | What it does |
|---|---|---|
| PDF pages to read / Characters to read | 6 / 12,000 | How much of a book the AI reads, from the start (and the end). |
| Ignore subtitles when comparing titles | off | "Dune: Messiah" = "Dune". |
| Similar author matching | on | Initials ignored; one shared author is enough. |
| Compare covers when metadata can't decide | on | Needs an Image AI. |
| Always compare covers | off | The same cover makes a duplicate even when the metadata differs. |
| Re-check year differences by reading both books | on | The years printed in the books decide. |
| Same title, author written differently | on | "Frederickk Marryat" / "Frederick Marryat"; AI for other spellings. |
| Match similar titles by the same author | on | Needs proof: ISBN, same text or same cover. |
| Same author + same series + same number = same book | off | For reliably numbered collections only. |
| Move files Calibre can't open to the trash library without asking | off | Ticks those rows. |
| Write AI-found title/authors/publisher/ISBN to moved books | on | Only empty fields. |
| Delete permanently from source | off | Else Calibre's recycle bin. |
| Calibre program folder | found automatically | Where `calibre-debug` and the converters are. |

On the main window: the three libraries, *Cleanup source only*, and the **Text AI** and
**Image AI** (see [AI](#ai)).

### Command line

```powershell
python -m calibre_dedup --cli --source D:\Books\Inbox --target D:\Books\Main --trash D:\Books\Dupes --report plan.csv
python -m calibre_dedup --cli ... --execute        # perform the plan (default: dry run)
python -m calibre_dedup --cli ... --text-profile "Azure gpt-4o" --image-profile ""
python -m calibre_dedup --cli ... --no-ai          # metadata only
python -m calibre_dedup --cli ... --cleanup-only   # nothing copied to the target
python -m calibre_dedup --cli ... --clear-cache    # ask the AI again
```

Options left out are taken from the GUI's saved settings. The plan is printed; `--report`
also saves it as CSV.


## Metadata Review

### What it does

It reads **every** book of one library with the AI (the first pages and, with an Image AI,
the cover) and compares what it read with Calibre's metadata. Where they differ, it proposes
an update of **title, authors, publisher, year and series (with its number)**, as printed in
the book itself.

Choose the **library to review** and the **trash library**, then **1. Scan with AI**.

### Step 1: Scan

- **What is read:** the first pages (PDF: *PDF pages to read*, default 6; other formats:
  *Characters to read*, default 12,000) and, with an **Image AI**, the cover: Calibre's
  `cover.jpg`, else the cover inside the file. With an Image AI each book goes to it once
  (text, cover and, for scanned PDFs, page images); without one, the Text AI reads the text
  only and scanned PDFs are skipped.
- **Stop** at any point: what was scanned can be executed, and the next scan continues (see
  *Continue another day* below).

### Step 2: Review the list

- Books where the AI read something different show two lines: the current value, then
  `→ proposed value` in green. Case, accents, punctuation, author order and publisher
  suffixes ("Editore", "S.p.A.") are not differences, and a field the AI did not find is
  never proposed: **nothing is erased**. The cover of the selected book is shown on the
  right.
- **Filters:** *Only with differences* (on by default), *Not read* (books the AI couldn't
  read: no file, no text, errors), *Only checked*, *Only failed*, *Unreadable files*.
- **Choosing:** books with differences are proposed for **Update** and ticked.
  - The **Change:** boxes turn a field on or off for all books (e.g. never change the
    publisher).
  - Right-click turns one field off for the selected books ("Don't change publisher", shown
    struck through), or sets the action: *Update metadata*, *Keep as it is*, *Move to the
    trash library*; and, for unreadable formats, *Move the unreadable formats to the trash
    library* / *Keep the unreadable formats*.
  - *Check visible* / *Uncheck visible* act on the rows the filters show.
- **Books with no file Calibre can open** (a record without files, or only unreadable ones)
  are proposed for the trash, ticked. A book with some unreadable files is read from the
  others, and those files are proposed for the trash library (the record is copied there
  first). See [Files Calibre can't open](#files-calibre-cant-open).

### Step 3: Execute

With Calibre closed, **2. Execute checked** writes the ticked updates inside Calibre (a new year keeps the
date's month and day), and the ticked trash books are copied to the trash library and removed
from the reviewed library (recycle bin, or permanently with *Delete permanently from the
reviewed library when trashing*). Updated and trashed books leave the list.

### Continue another day, on any computer

Every book whose metadata is updated also gets the tag **`AIReviewed`**, and the next scan
skips books with that tag (*Skip books tagged AIReviewed*, on by default). So you can stop a
scan anywhere, execute what you have, and continue later, even from another computer: the
mark is in the library itself, not in the cache. Only updated books are tagged: books with
no differences, or that you keep, are read again by the next scan (quickly on the same
computer, from the cache). To review a book again, remove the tag in Calibre.

### Settings

The review has its own settings (`review_settings.json`) and its own AI cache
(`review_cache.json`), so it can run at the same time as the Duplicate Remover. The first
time, both start as a copy of the Duplicate Remover's: the same AI profiles, trash library,
Calibre folder and reading limits (Settings → *Reading*). After that a change in one program
doesn't reach the other; API keys stay shared (they are stored per profile name). AI answers
are cached per book and cover, so a second scan is quick, even after an update renamed the
book's files.

### Command line

```powershell
python -m calibre_dedup.review_app --cli --library D:\Books\Main                  # dry run: prints the differences
python -m calibre_dedup.review_app --cli ... --fields title,authors --execute    # write only these fields
python -m calibre_dedup.review_app --cli ... --include-reviewed                  # also books tagged AIReviewed
```

Also `--trash`, `--text-profile`, `--image-profile` (`""` for none) and `--clear-cache`.
Options left out are taken from the review's saved settings.


## AI

Both programs use the AI the same way, and share the profiles and API keys.

### Profiles and the Text AI / Image AI

A **profile** (Settings → *AI providers*) describes one model: provider, server or endpoint,
model name, key and options. Tick *Supports images* when the model reads images as well as
text. The main window of each program then chooses:

- **Text AI**: reads book text to find missing metadata. *None* turns all AI off.
- **Image AI**: a model that reads text and images. It compares covers, reads covers in the
  review, and reads scanned PDFs. Only profiles with *Supports images* are listed; it can be
  the same profile as the Text AI. *None* skips covers and scanned PDFs.

Each run logs what it uses, e.g. `AI: text = Ollama (gemma3:12b) · images = Ollama
(gemma3:12b) · cover check on`.

| Provider | What to fill in |
|---|---|
| **Ollama** (local) | Server URL (default `http://localhost:11434`) and model; *Refresh models* lists the installed ones. For text, e.g. `qwen2.5:14b`, `llama3.1:8b`, `mistral-nemo`; for images a model that reads them, such as `qwen2.5vl`, `llama3.2-vision`, `gemma3` or `qwen3.5`. Context size default 16,384. |
| **Azure OpenAI** | Endpoint (`https://<resource>.openai.azure.com`), deployment name, API key and API version (e.g. `2024-10-21`). Version `v1` uses the new `/openai/v1` API, where the deployment field holds the model name. For reasoning models (o-series, gpt-5) untick *Send temperature*. |
| **OpenAI** | Model name and API key (the endpoint is fixed). |
| **Anthropic** | Model name and API key (the endpoint is fixed). |

API keys are stored in the Windows Credential Manager (through `keyring`), never in the
settings files. The `CDR_API_KEY` environment variable can provide one too.

**Advanced parameters.** A profile can add parameters of your choice to every request, for
options the form doesn't have. The model must accept them.

| Provider | Example | Effect |
|---|---|---|
| Ollama | `think` = `false` | No "thinking" before answering. With thinking models (qwen3.x) this is much faster (seconds instead of minutes) and avoids empty replies from a model that thinks until its context is full. |
| OpenAI, Azure | `reasoning_effort` = `none` or `low` | Less reasoning. Reasoning models only: others refuse the parameter. |
| Anthropic | — | Nothing needed: thinking is off unless requested. |

- A value is JSON when it parses as JSON (`false`, `1024`, `"text"`), otherwise plain text
  (`none`, `low`). A dot puts a parameter inside an object: `options.num_predict` = `1024`.
- Not accepted: fields the form already has (model, temperature, context size) and fields
  the program sets itself (`messages`, `stream`, `format`, `response_format`, and for
  Anthropic `system` and `max_tokens`). Settings can't be saved with an invalid parameter.
- The log shows the parameters sent with each call (`extra=think=false`).

**Test connection** sends one real metadata request, with all the profile's settings and
parameters, on a made-up copyright page built into the program (so the model can't answer
from memory; its traps: a translator, an original title and a later edition). It reports a
parameter refused by the server, an empty or cut-off reply (and why), a slow answer, a model
that still "thinks", and each value read, right (✓) or wrong (✗). With *Supports images* it
also checks that the model sees a test image. Ollama silently ignores parameter names it
doesn't know: a misspelled `thinking` = `false` has no effect, and the test shows the model
still thinking.

When the request is **refused**, the test finds the cause by asking again, whatever the
provider's error format: once without advanced parameters (if that fails too, check the
model, key and URL), then with each parameter alone, marking each accepted (✓) or refused
(✗, with the server's message). Whatever fails, the report shows the actual error as the
server or the network gave it.

### What the AI reads

Only extracted text and images are sent, never whole files. One format per book is read, in
this order: EPUB, KEPUB, AZW3, MOBI, AZW, PDF, FB2, DOCX, RTF, HTMLZ, TXT, DJVU.

| Format | How | What is read |
|---|---|---|
| EPUB, KEPUB | read directly, chapters in reading order | the first (or last) 12,000 characters |
| PDF | Calibre's `pdftotext` | the first (or last) 6 pages |
| TXT | read directly | the first (or last) 12,000 characters |
| others (MOBI, AZW3, FB2, DOCX…) | converted to text with Calibre's `ebook-convert`, into a temporary folder (the book is not changed) | the first (or last) 12,000 characters |

The first pages (title page, copyright page) are read first; the Duplicate Remover reads the
last pages (colophon) only if fields are still missing. A PDF with almost no text is taken as
scanned: up to 4 pages are rendered with `pdftoppm` and sent as images to the Image AI. There
is no OCR. Files with DRM or damaged files can't be read: such books stay where they are
when the AI is needed to identify them.

The exact instructions sent are in the [Appendix](#appendix-ai-prompts).

### Cache

Every AI answer is kept (`ai_cache.json` for the Duplicate Remover, `review_cache.json` for
the review), so analyzing again, or after *Stop*, is fast.

- A book is read again when its file changes (size or date). Switching the Duplicate
  Remover's *Text AI* model does **not** re-read books.
- A cover comparison, or an author question, is asked again when the model changes.
- Errors (timeouts, connection problems, invalid replies) are not cached: those books are
  retried next time. An empty reply is cached as "nothing found" (some models answer nothing
  when the pages hold no metadata).
- Changing a setting never needs a cache reset: the rules are applied again to the cached
  answers.
- To start over: Settings → **Clear AI cache…** (small button at the bottom right; not
  while a run is in progress), or `--clear-cache`. Each program clears only its own cache.

### When a setting can't take effect

The programs never change a setting by themselves. When one is on but can't work, they tell
you:

- **In the window:** a *None* AI, or an Image AI while the Text AI is *None*, is shown in
  amber italics. In Settings, a ticked option that can't run says why, e.g. *inactive: no
  Image AI selected*.
- **Before a run:** *Analyze* lists what won't take effect (e.g. the cover check with no
  Image AI) and whether a local Ollama server is unreachable or lacks the model, and offers
  to use a profile that reads images as the Image AI. A warning can be hidden for good,
  except an unreachable AI; *Settings → Show dismissed warnings again* brings them back.
- **When an AI stops responding** (3 errors in a row), the run pauses and asks: *Retry*,
  *Skip this book*, *Continue without* that AI for the rest of this run, or *Stop*. The
  next run tries it again. A failing Image AI never stops the Text AI. Errors about one book
  (a content filter refusing it, a text too long for the model) don't count.
- **After an analysis:** each row's reason says what was skipped (e.g. *cover check
  skipped: no Image AI*), the *Reduced checks* filter lists those books, and the amber bar
  sums it up. The status line counts what the AI did (pages read, covers compared, year
  re-checks).
- **Settings changed since the analysis:** the amber bar names them, so you know to analyze
  again (fast: AI answers are cached).


## Data files and logs

Everything lives in `~\.CalibreDedup` (e.g. `C:\Users\<you>\.CalibreDedup`), never in the
program folder. Data from older versions in `%APPDATA%\CalibreDuplicateRemover` is moved
there automatically.

| File | Program | Contents |
|---|---|---|
| `settings.json` | Duplicate Remover | libraries, options, AI profiles (no keys) |
| `review_settings.json` | Review | its own settings |
| `ai_cache.json` / `review_cache.json` | each | the AI's answers |
| `selections.json` | Duplicate Remover | remembered ticks and changes, per source/target pair |
| `calibre_dedup.log` / `calibre_review.log` | each | what happened, including every AI request and reply |
| `calibre_dedup_perf.log` / `calibre_review_perf.log` | each | AI performance (below) |

The **performance logs** hold one JSON object per line, to compare AI providers: for each
request its size (characters, images, bytes), time, tokens and Ollama's own timings; for each
analysis or scan, its length, the books sent to the AI and the totals. They hold no book
metadata: a book is only its number in the run.

The logs rotate at 5 MB, keeping 3 old files.


## Tests

```powershell
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m pytest
```


## Appendix: AI prompts

These are all the instructions the programs send to the AI, in English whatever the books'
language (they are in [ai.py](calibre_dedup/ai.py)). Each is a *system prompt* (what the AI
must do and how to answer) plus a short *user message* carrying the book's text, images or
names. Every request also asks the server for JSON (`format: "json"` for Ollama,
`response_format: json_object` for Azure and OpenAI; Anthropic has only the prompt's own
instruction). The answers are cached, so each question is asked once per file and model.

### 1. Reading a book's metadata (Duplicate Remover)

Used when the metadata can't decide: a book without title/authors, two copies whose edition
or publisher is missing, and the year re-check. The AI reads the **start** of the book (title
page, copyright page) and, if still needed, the **end** (colophon). Scanned PDFs are sent as
page images to the Image AI. `SYSTEM_PROMPT`:

```text
You are a librarian extracting bibliographic metadata from an excerpt of an e-book (front
matter such as the title page and copyright page, or back matter such as a colophon). The
text may be in any language.

Respond with a single JSON object with exactly these keys:
{"title": string|null, "authors": [string], "publisher": string|null, "edition": string|null,
 "edition_number": integer|null, "year": integer|null, "isbn": [string]}

Rules:
- Use only information present in the text. Never guess. Use null or [] when not found.
- "title": the book's title (include the subtitle if clearly shown), not a chapter or series name.
- "authors": the authors' names as printed. Exclude translators, editors of forewords, illustrators.
- "publisher": the publishing house of THIS edition (not the printer or distributor).
- "edition": the edition statement as printed, e.g. "Second edition", "3a edizione".
- "edition_number": the edition number (1 for an explicitly stated first edition).
  Printing/reprint lines such as "10 9 8 7 6 5 4 3 2 1" or "ristampa" are NOT editions.
- "year": the publication year of THIS edition (not the original first publication, if both
  are shown).
- "isbn": ISBNs printed for this book (digits and X only).
```

User message: `Excerpt:` and the text; for a scanned book, `The excerpt is given as page
images, plus this extracted text:` and the text (or `…page images.` when there is none).

### 2. Comparing two covers (Duplicate Remover)

Used by *Compare covers* (when the metadata can't decide) and *Always compare covers* (also
when it says the books differ), sent to the **Image AI**. The same prompt compares the covers
stored inside the book files, for similar titles. "unsure" counts as not the same.
`COVER_PROMPT`:

```text
You compare two book cover images to tell whether they are the cover of the same edition of
the same book.

Respond with a single JSON object: {"verdict": "same"|"different"|"unsure", "reason": string}

Rules:
- "same": the same cover artwork, title and author, and layout. Differences in resolution,
  cropping, compression, colour balance, borders or small overlays (e.g. a store badge) do
  not matter.
- "different": different artwork, title, author, publisher logo or edition statement (e.g. a
  "2nd edition" banner on one only).
- "unsure": either image is not a real cover (blank, generic placeholder, text-only
  generated cover) or you cannot tell.
```

User message: `Cover 1 and cover 2 are attached.`, with the two covers.

### 3. Same person, name written differently (Duplicate Remover)

Used by *Same title, author written differently*: two books with the same title whose
authors don't match and aren't one letter apart or initials (checked first, without AI). Sent
to the **Text AI**. `AUTHOR_PROMPT`:

```text
You decide whether two author names, taken from e-book metadata, name the same person.

Respond with a single JSON object: {"verdict": "same"|"different"|"unsure", "reason": string}

Rules:
- "same": the same person written differently: a typo or a missing/extra letter ("Frederickk
  Marryat" / "Frederick Marryat"), another name order ("Asimov Isaac"), initials, accents, a
  transliteration ("Dostoevskij" / "Dostoyevsky"), or a well-known pen name of that person.
- "different": different people, even if they share a surname or a first name ("James
  Herbert" / "Frank Herbert").
- "unsure": you cannot tell.
A list of names separated by "&" names several authors: "same" if at least one person is on
both lists.
When the title is given, both names are the author of two copies of a book with that title:
a name matching the other with initials or a typo is then "same". Judge the names only: do
not guess who else a short name might be.
```

User message:

```text
Name 1: "<authors of one book>"
Name 2: "<authors of the other>"
Both books are titled: "<title>"
```

### 4. Identifying a book (Metadata Review)

Used for every book the review scans (unless cached): the AI reads the first pages and the
cover and proposes title, authors, publisher, year and series, which are compared with
Calibre's metadata. `REVIEW_PROMPT`:

```text
You are a librarian identifying an e-book from its first pages and, when attached, its cover.
The text may be in any language.

Respond with a single JSON object with exactly these keys:
{"title": string|null, "authors": [string], "publisher": string|null, "year": integer|null,
 "series": string|null, "series_index": number|null}

Rules:
- Use only information shown in the pages or on the cover. Never guess. Use null or [] when
  not found.
- "title": the book's title as printed, with normal capitalisation (not ALL CAPS). Without
  the series name or number, unless they are part of the title itself.
- "authors": the authors' names as printed, in "First Last" order. Exclude translators,
  editors of forewords, illustrators and cover artists.
- "publisher": the publishing house of THIS edition (not the printer or distributor).
- "year": the publication year of THIS edition (not the original first publication, if both
  are shown).
- "series": the series or numbered collection the book belongs to, e.g. a saga
  ("Foundation") or a publisher's numbered collection ("Gutenberg"). null if none is shown.
- "series_index": the book's number in that series (e.g. 3, or 1234 for "Gutenberg n. 1234").
  null if not shown.
```

User message, from these lines:

```text
The first attached image is the book's cover.        (when it has a cover)
The other N attached images are its first pages.     (scanned books)
Text of the first pages:

<text>                                               (or "No text could be extracted.")
```

### Connection tests (Settings, both programs)

- **Test connection** sends prompt 1 with the made-up Italian front page (*Il guardiano del
  faro* by Elena Marchetti; the translator Paolo Bianchi is not an author; Edizioni
  Lanterna, third edition 2021, the first was 2019; ISBN 978-88-7000-123-4) and checks each
  value of the answer.
- **Finding a refused advanced parameter** sends short requests with the parameters one at
  a time: system `Reply with a JSON object.`, user `Return {"ok": true}.`
- **Checking that a model reads images** sends a plain red square: system `Reply with a JSON
  object.`, user `What is the colour of the attached image? Return {"colour": "<one word>"}.`
  The model passes if it answers red.
