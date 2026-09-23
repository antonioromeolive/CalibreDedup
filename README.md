# CalibreDedup - an offline Duplicate Remover for Calibre Libraries

Version **0.1.0** · Released **2026-09-23** · Author **Antonio Romeo**
([antonioromeo@ilve.it](mailto:antonioromeo@ilve.it)) · [MIT License](LICENSE)

Moves books from a **source** Calibre library into a **target** library. Books
that already exist in the target go to a **trash** library instead. Books whose
identity can't be determined stay in the source.
Ue AI to determine duplicate when no metadata are available.


## How it works

1. **Analyze (dry run).** Reads the three libraries' `metadata.db` files
   read-only. This is safe while Calibre is open. It then decides, for each
   source book:

   | Action | When |
   |---|---|
   | **Move to target** | No target book has the same title and authors, or every such book is a different edition or from a different publisher. |
   | **Trash (duplicate)** | A target book has the same title and authors, **and** the same edition and publisher. The source book's formats that the target copy lacks, except PDF, are added to the target copy. |
   | **Leave in source** | Title or authors can't be determined, or edition or publisher can't be compared, even with AI. |

  When source and target are the same library, confirmed duplicate records
  are handled in place: the record with richer metadata is kept, missing
  formats from the other record are added to it, and the weaker record goes
  to the trash library. Records that are different or cannot be compared stay
  in the library.

2. **Review and choose.** Only books whose checkbox is ticked are acted on.
   Move and Trash rows start ticked. Leave rows can't be ticked unless you
   override their action.
   * **Filter** with the search box, where all words must match across title,
     authors, reason and match. You can also filter by action or turn on *AI
     used*, *Adds formats*, *Only checked* or *Only failed*.
   * **Tick or untick in bulk:** *Check/Uncheck/Invert visible* acts on the
     rows the filter currently shows. **Space** toggles the selected rows.
   * **Right-click** to *Force move to target*, *Force trash* (only when a
     matching target book exists), *Keep in source*, or *Revert to analysis
     decision*. Overridden rows are shown in italics.
   * A duplicate of a book that this same plan moves into the target is
     **blocked** (⚠) while that move is unticked or overridden.
   * Your unticked books and overrides are remembered per source/target pair
     and re-applied on the next analysis.
   * *Export CSV* saves the plan, including what is ticked.

3. **Execute.** Requires Calibre to be closed. The writes go through Calibre's
   own library code (`calibre-debug`), the same code as Calibre's *Copy to
   library*. Covers, custom columns and all other metadata are kept. A source
   book is removed only after its copy has been verified. By default, removed
   books go to the source library's own Calibre recycle bin.

### Duplicate rules

After normalization, two books with the same title and authors are compared:

* **A shared ISBN** means a duplicate. ISBNs come from metadata or from the
  book's pages.
* **Otherwise edition and publisher are compared.** Edition is the edition
  number if both books have one, otherwise the publication year. Publishers
  are compared loosely: "O'Reilly Media, Inc." matches "O'Reilly".
  * Both the same: duplicate.
  * Either one different: a different book.
  * Either one unknown: stays in the source.
* **Different ISBNs alone decide nothing.** An e-book and a print book of the
  same edition have different ISBNs.

Normalization ignores case, accents, punctuation and a leading "The/A/An". It
also strips edition statements such as "(2nd Edition)" from titles, and treats
"Tolkien, J.R.R." and "J. R. R. Tolkien" as the same author. Subtitles count
unless *Ignore subtitles* is on.

### When AI is used

AI is used only when the metadata isn't enough:

* The source book has no title or authors ("Unknown").
* A target book with the same title and authors exists, but edition or
  publisher is missing. Both books are then read.

The program reads the **first pages** (title page and copyright page). If
fields are still missing, it also reads the **last pages**. Text comes from
EPUB and TXT files directly, from PDFs through Calibre's `pdftotext`, and from
other formats through `ebook-convert`. Scanned PDFs can be sent as page images
to a vision model; set a *Vision profile* in Settings. Results are cached in
`~\.CalibreDedup\ai_cache.json`, so re-running an analysis
is fast.

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

You can define several profiles and switch between them from the main window.

* **Ollama:** server URL (default `http://localhost:11434`) and model. *Refresh
  models* lists the models you have installed. Good choices are
  `qwen2.5:14b`, `llama3.1:8b` or `mistral-nemo`. For scanned PDFs, use a
  vision model such as `qwen2.5vl` or `llama3.2-vision` and tick *Model accepts
  images*.
* **Azure OpenAI:** endpoint (`https://<resource>.openai.azure.com`),
  deployment name, API key and API version (e.g. `2024-10-21`). The API
  version `v1` uses the new `/openai/v1` API, where the deployment field holds
  the model name. For reasoning models (o-series, gpt-5), untick *Send
  temperature*.

API keys are stored in the Windows Credential Manager through `keyring`. You
can also set the `CDR_API_KEY` environment variable.

## Command line

```powershell
python -m calibre_dedup --cli --source D:\Books\Inbox --target D:\Books\Main --trash D:\Books\Dupes --report plan.csv
python -m calibre_dedup --cli ... --execute        # perform the moves
python -m calibre_dedup --cli ... --no-ai --profile "Azure gpt-4o"
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
