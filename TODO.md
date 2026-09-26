# TODO

Open work items. Each one records the problem, the current decision (or what is still
undecided), and pointers into the code, so it can be reviewed later and handed to an
implementing agent.

Status legend: **DECIDED** = agreed, ready to implement · **PROPOSED** = suggested, awaiting
decision · **OPEN** = idea on the table, not discussed further.

---

## 1. Make settings that silently do nothing visible — DONE (2026-09-26)

### Problem
Settings could be on but have no effect, and nothing on screen said so. Real case
(2026-09-26): "Compare covers when metadata can't decide" was ticked, but Image AI was
"None", so the cover check never ran. Also, when Ollama wasn't running, the AI was turned
off after 3 consecutive errors and only the log said so.

### Guiding principle
The app never changes or resets a user's setting. It keeps the value the user chose and
**reports** when that setting can't take effect, and why. Anything turned off at run time
(the AI after repeated errors) applies to the current run only.

### What was implemented
- **1a. AI stops responding:** after `MAX_CONSECUTIVE_AI_ERRORS`, `AIResolver` calls
  `on_down(message, image)`. The GUI (`AnalyzeWorker.ask` / `MainWindow._on_ai_down`) pauses
  and asks **Retry / Continue without the text|image AI / Stop**. The same dialog is used for
  the text AI and the image AI (decided). The CLI keeps the old behaviour (turn it off, go on).
- **1b. "None" styling:** "None" in the Text AI and Image AI combos is amber italic, both in
  the open list and in the closed combo (`style.mark_inactive`, `style.style_none_item`;
  font and palette, no stylesheet). Amber: `#b26a00` light theme, `#ffb74d` dark theme.
- **1c. Inactive, not greyed out:** the Image AI combo stays enabled and keeps its value when
  the Text AI is None, marked inactive with a tooltip. In Settings, "Compare covers" and
  "Re-check year differences" show an inline "inactive: …" note while ticked but unable
  to run.
- **1d, chosen items:**
  - **Pre-flight check** (`session.preflight`, `MainWindow._preflight_ok`): cover check with
    no Image AI (offers "Use <profile> as Image AI"), AI off with AI options on, invalid
    profiles, and local Ollama unreachable or model not installed. "Don't warn me again"
    for everything except unreachable AIs (`Settings.dismissed_warnings`; reset with
    Settings → "Show dismissed warnings again").
  - **Per-row skipped reasons:** `PlanItem.skipped` (planner `SKIP_*`), with notes in the
    reason ("cover check skipped: no Image AI", "year re-check skipped: AI is off",
    "scanned PDF skipped: no Image AI", AI disabled). New filter: **Reduced checks**.
  - **Post-run summary:** `Plan.stats` / `Plan.ai_down`, `planner.run_summary`. Shown in the
    status line, the amber bar above the table, the log and the CLI output.
  - **Stale-plan banner:** `session.analysis_signature` / `changed_settings`; the amber bar
    names the settings changed since the analysis.
- Tests: `tests/test_planner.py` (retry, skipped checks, AI down, summary) and
  `tests/test_session.py` (pre-flight, changed settings). README: "Settings that can't take
  effect".

### Not done (still OPEN)
- **Effective-configuration strip** next to Analyze (chips such as
  `Cover check: OFF (no Image AI)`). Probably unnecessary now that pre-flight exists.
- Cloud providers (Azure, OpenAI, Anthropic) are not pinged by the pre-flight: it would
  need keys and could cost calls.

---

## 2. Titles with extra text (numbers, series, author, file names) never match — DONE (2026-09-26)

### Problem
Matching needed an exact normalized title, so books whose title had extra text around the
real title were never paired with their target copy and were moved as "not in target".
Real cases: "1 Abissi D'acciaio" / "Abissi D'Acciaio"; "(Urania - 0411- Supernormale -
J. Hunter Holly)" / "Supernormale"; "(Urania Millemondi 2x033 2001 Dicembre - ASTRONAVI
MALEDETTE)" / "ASTRONAVI MALEDETTE Inverno 2001".

### Decisions
- Implement B (title inside title, both directions), C (clean file-name titles), the
  apostrophe fix and the "various authors" mapping.
- Similar titles need **strong proof** to be a duplicate: same ISBN / ASIN / series number,
  identical EPUB text, or same cover. Unproven → **Leave, "check manually"**, match shown.
- New setting **"Match similar titles by the same author"**, on by default.
- "Various authors" (autori vari, various artists, AA.VV, AAVV, A.V., V.A., various
  authors, various) is one author; it is **not** the same as unknown ("unknown",
  "sconosciuto", "autore sconosciuto"), which counts as no author.

### What was implemented
- `normalize._tokens`: an apostrophe separates words ("l'Inferno" = "l Inferno").
- `normalize.author_key`: all "various authors" spellings → `VARIOUS_AUTHORS_KEY`;
  "autore sconosciuto", "unknown author"… added to `UNKNOWN_VALUES`.
- `normalize.title_variants` (C): splits dash-separated titles, drops author, bare-number
  and "collection + number" parts; the first variant is the **core** title.
- `normalize.contains_title` + `noise_words` (B): a title is inside another only with
  noise around it (numbers, single letters, authors, series, publisher, seasons, months,
  issue words such as NS/nr/speciale). A "title" made only of noise is never used.
- `planner._similar_title` / `_decide_similar`: with no exact candidate, books by the same
  author whose core titles both contain one shared title. Noise also includes the
  libraries' **collections**: words of series with ≥ 20 books (`COLLECTION_MIN_BOOKS`), e.g.
  Urania, Millemondi. A real metadata difference (not only the year) or a different cover
  rules a book out (moved as usual).
- `matcher.Comparison.proof` marks ISBN / ASIN / series-number duplicates.
- Found and fixed along the way (pre-existing bug): `strip_edition` removed any bracket
  group containing "ed" inside a word, e.g. "(… ASTRONAVI MALEDETTE)" became an empty
  title ("title/authors unusable after normalization"). Now whole words only; 5 titles in
  the user's libraries were affected.
- Tests in `tests/test_normalize.py` and `tests/test_planner.py`; README "Similar titles".

### Result on the real libraries (metadata only, no AI; 2026-09-26)
With the option on, 46 of 393 source books change: 9 become duplicates with proof
(identical EPUB text or same ISBN), about 33 are left to "check manually" (with the Image AI
on, the cover check will decide many of them), the rest are other small shifts.

### Known limits / possible follow-ups (OPEN)
- "Series N - Title" titles whose series isn't set in Calibre ("Robot 02 - Il sole nudo",
  "Ciclo dell'Impero 3 - Paria dei cieli") are not matched: "robot", "ciclo dell impero"
  aren't noise. Setting the series in Calibre fixes it (its words become noise).
- Two parts of different volumes ("Demon" / "Demon 1", "I grandi maestri … 3" / "… 3-2")
  are proposed for a manual check.

---

## 3. "Always compare covers": a matching cover overrides the metadata — DONE (2026-09-26)

### Problem
The cover check ran only when the metadata verdict was UNKNOWN. "L'inferno a rovescio"
(source id 2036, date 2011 = e-book creation; target id 5312, 1986) was moved as "different
year" although it is the same book with the same cover.

### Decisions
- New setting **"Always compare covers"** (off by default: to try out; needs an Image AI).
- The same cover means the same book, whatever the metadata: it overrides **year,
  publisher and edition**. Such a duplicate is **Trash only**: nothing is copied into the
  other book. A different cover leaves the metadata verdict as it is.
- **Safeguard:** Calibre's `cover.jpg` may be a downloaded picture, so the covers **inside
  the book files** must match too. No cover inside a file → nothing is overruled.
- Rows decided by a cover are marked: filter **"Decided by cover"**.

### What was implemented
- `extract.TextExtractor.embedded_cover` / `_epub_cover`: the cover inside the file. EPUB
  read directly (EPUB 3 cover-image, EPUB 2 meta cover, an image named "cover"), else
  Calibre's `ebook-meta --get-cover` (also MOBI, AZW3, AZW, FB2). On the user's library the
  direct EPUB read finds 199 of 200 covers.
- `planner.AIResolver.same_embedded_cover` (identical bytes → same without AI; otherwise
  the Image AI; cached per pair of files and model); `planner._cover_decides`.
- Step 4 of `_decide_one` compares DISTINCT candidates too when the option is on; also in
  `_decide_similar` (similar titles). `PlanItem.by_cover`, `Settings.always_cover`,
  pre-flight (needs an Image AI), Settings checkbox with "inactive" note, summary counts.
- Tests in `tests/test_planner.py`, `tests/test_session.py`; README "Always compare covers".

### Still OPEN
- See how it behaves on the real libraries with the Image AI on, then decide whether it
  should be on by default.
- A cover match when the metadata could not decide (UNKNOWN) still uses only `cover.jpg`
  and still merges formats (Merge & Trash), as before.

---

## 4. "Copy title" / "Copy author" in the book list's right-click menu — DONE (2026-09-26)

Right-clicking a row offers **Copy title** and **Copy author**, next to the Open actions
(available even while analyzing or executing).
- Decided: source book only; copies exactly what the Title and Authors columns show (the
  AI's value only when the book had no title/authors in Calibre); **all authors** as shown
  ("A & B"); **only the right-clicked row**, even with several rows selected.
- Code: `MainWindow._context_menu` in [main_window.py](calibre_dedup/gui/main_window.py);
  README "Right-click → Copy title / Copy author".

---

## 5. Same title, author written differently — DONE (2026-09-26)

### Problem
"Signori Del Tempo" (source id 458, author "Wilson Tucke") was moved although the target
has the same book (id 4733, "Wilson Tucker", identical EPUB text): with different author
keys the books were never compared, so neither the EPUB text nor the covers were checked.

### Decisions
- New setting **"Same title, author written differently"**, on by default.
- Only books with the **same title** are checked (a title index), so there are few pairs;
  comparing every source book with every target book (or asking the AI about every pair)
  was ruled out as too costly.
- First a free check (one letter apart in a name part of ≥ 5 letters), then the **Text AI,
  always when one is set**, asked **in English** whether the two names are the same person.
- If they are, the books are compared with the normal rules (no extra proof needed: the
  title is identical).

### What was implemented
- `normalize.names_nearly_equal`; `ai.AUTHOR_PROMPT` / `ai.compare_authors`;
  `planner.AIResolver.same_person` (cached per pair of names and model);
  `planner._author_variant_candidates`; title index in `build_plan`;
  `Settings.author_variants`, Settings checkbox, summary count, CLI.
- Tests in `tests/test_planner.py`, `tests/test_normalize.py`; README "Authors written
  differently".

### Result (2026-09-26)
Metadata only, no AI: 9 source books found by the free check (Tucke/Tucker,
Rackam/Rackham, Hamiton/Hamilton, Golstein/Goldstein, McGregor/MacGregor, Fletcher/Flecher,
Michaerl/Michael, Edmund/Edmond, Harryson Harry/Harry Harrison). "Signori Del Tempo" is
now a duplicate (identical EPUB text); the others go on to the AI and cover checks.
qwen3.5:4b answers the English prompt correctly for transliterations and name order, but
doesn't know pen names (Paul French = Isaac Asimov).

### Not done (OPEN)
- Titles **and** authors both different (only a cover would link them): candidates by a
  perceptual hash of the covers (an index, no pairwise comparison), with proof required.
  Discussed, not requested.

---

## 6. More items

_To be added: the user has further changes to suggest._
