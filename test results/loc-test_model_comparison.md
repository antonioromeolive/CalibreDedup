# Metadata Review on loc-test: qwen3.5:4b vs gpt-5.4-nano

Two runs of the Metadata Review over the whole **loc-test** library (522 books), both with *No AI cache*, on 2026-09-29. Each model was both the Text AI and the Image AI:

| Run | Model | Where | Started | Run file |
|---|---|---|---|---|
| A | `qwen3.5:4b` | Ollama, local | 21:44 | `review_loc-test_20260929_221619.csv` |
| B | `gpt-5.4-nano` | Azure | 22:26 | `review_loc-test_20260929_225855.csv` |

## In short

- **Speed:** the same. Both took about 32 minutes for 522 books, and both had a median of 2.8 s per AI call.
- **Errors:** qwen had none. gpt had **21 calls refused by Azure's content filter**, on 17 books. The review's retries recovered all of them, so every book was read.
- **Finding the planted errors:** a tie. Both found every detectable title and author error (30 of 30). gpt restored the exact original value slightly more often: 25 against 23.
- **Precision:** gpt is clearly better. It was never misled by a swapped cover, where qwen was twice, renaming a book after the book on its cover. It made fewer unrequested title and author changes on correct books: 122 of 468 books against 176.
- **gpt's weak spot:** series. It proposes a series 84 times, 25 of them the Project Gutenberg eBook number ("Project Gutenberg eBook [9594]"). Its two copies of the same book also agree less often.

gpt-5.4-nano gives better quality at the same speed. The price is the content filter and bad series proposals, both of which the program can work around (see the last section). qwen3.5:4b is free and local and never refuses, but it needs more checking by hand.

## 1. Performance

| | qwen3.5:4b | gpt-5.4-nano |
|---|---:|---:|
| Wall-clock duration | 1,921 s (32.0 min) | 1,962 s (32.7 min) |
| Time spent in AI calls | 1,473 s (77%) | 1,577 s (80%) |
| AI calls | 522 | 543 (522 + 21 retries) |
| Per call: min / median / mean | 1.40 / 2.81 / 2.82 s | 1.65 / 2.82 / 2.90 s |
| Per call: p95 / max | 3.29 / 8.15 s | 3.58 / 4.06 s |
| Input tokens | 1,837,099 | 1,748,695 |
| Output tokens | 28,132 | 26,745 |
| Reasoning tokens | 0 | 0 |

- **The speeds are the same in practice.** gpt's run is 40 s longer; the 21 refused calls account for 58 s of it.
- **qwen's slowest call (8.15 s) was a model load:** 5 s loading the model, 1.8 s processing the prompt and 1.2 s generating. Loading added 21.6 s over the whole run.
- **Time outside the AI calls** was about 6.5 to 7.5 minutes per run: reading the books, extracting text and rendering covers. It doesn't depend on the model.
- **Cost:** qwen is free (it runs locally). gpt's cost is the tokens above at the Azure price for gpt-5.4-nano, about 1.75 M input and 27 k output tokens for 522 books.

## 2. Errors returned by the AI

**qwen3.5:4b:** no failed calls. All 522 calls ended normally.

**gpt-5.4-nano:** 21 calls failed with HTTP 400, all refused by Azure's content filter, on 17 books.
- The review asks again with less input each time: the first 3,000 characters plus the cover, then text only, then the cover only.
- Every book was read in the end. The run reports 0 books not read.
- **Which books:** 19th-century works, in both their MOBI and EPUB copies. Examples:
  - *History of the Negro Race in America*;
  - *Dred: A Tale of the Great Dismal Swamp*;
  - *Attila*;
  - *Incidents of the War*;
  - *The House of the Wolfings*;
  - *The Discovery of the Source of the Nile*;
  - *Peter Plymley's Letters*;
  - the Brann essays.
- **Why they were refused:** most likely the period language about race, war and violence in the opening pages. False positives, in effect.
- **Which filter category fired isn't known:** the log line records only "content filter", not the categories Azure returned.

| Retries needed | Books |
|---|---|
| 1 (read with the first 3,000 characters + cover) | 14 |
| 2 (read with text only) | 2 (both copies of *Attila*) |
| 3 (read from the cover only) | 1 (#724 *History of the Negro Race in America*, MOBI copy) |

**Effect on quality:** almost none in this test. Only one refused book carried a planted error (#636, an author written surname first), and it's ignored by design anyway. In a real library, though, a book read from 3,000 characters or from the cover alone gets a weaker reading.

## 3. Quality: the planted errors

`loc-test_wrong_metadata.csv` plants 45 errors in titles, authors and series, plus 20 changed covers (section 4). 13 of the 45 are **not differences by design**, so the review should stay quiet about them:
- titles in ALL CAPS (4);
- authors written surname first (6);
- series that aren't numbered (2);
- a correct series number used as a control (1).

Both models did stay quiet on those. That leaves **32 detectable errors**.

In the table, *fixed* means the exact original value was proposed. *Found* means fixed, or flagged with another value, such as the right title without its subtitle.

| Field | Kind of error | Count | qwen fixed | qwen found | gpt fixed | gpt found |
|---|---|---:|---:|---:|---:|---:|
| title | typo | 5 | 3 | 5 | 3 | 5 |
| title | truncated | 4 | 3 | 4 | **4** | 4 |
| title | "Unknown" | 4 | 3 | 4 | 3 | 4 |
| title | another book's title | 3 | 2 | 3 | 2 | 3 |
| authors | another book's authors | 6 | **5** | 6 | 5\* | 6 |
| authors | typo | 4 | 4 | 4 | 4 | 4 |
| authors | "Unknown" | 4 | 3 | 4 | **4** | 4 |
| series | wrong number | 2 | 0 | 0 | 0 | 1\*\* |
| **Total** | | **32** | **23** | **30** | **25** | **31** |

\* gpt and qwen each got a different book wrong here: see the differences below.
\*\* Found by accident: gpt proposed the Gutenberg eBook number as the series.

**Detection:** titles and authors 30 of 30 for both models. Neither model can fix a wrong series number: the book's text rarely states it.

**Where the two runs differ:**

| Book | Planted error | qwen3.5:4b | gpt-5.4-nano |
|---|---|---|---|
| #581 | authors "Unknown" (was *Duchess of Margaret Cavendish Newcastle*) | *Margaret Cavendish*: right person, different form | **fixed** |
| #877 | title truncated to *The sacred theory of the* | *The Sacred Theory of the Earth*: drops "Volume 1 (of 2)" | **fixed** |
| #1050 | authors of another book (was *Torbern Bergman*) | *William Withering*, the translator | **fixed** |
| #818 | authors of another book (was *Aaron Burr*) | **fixed** | *Matthew L. Davis*, the editor of Burr's memoirs |
| #635 | series *The Works [4]* (was 6) | missed | *Project Gutenberg eBook [9594]*: wrong |

**Missed by both:** #899, series *The Works [3]* (was 6).

**A pattern in the "found but not fixed" cases:** most are titles returned without their subtitle or volume part. For example, *Ten Years Among the Mail Bags* for *Ten Years Among the Mail Bags / Or, Notes from the Diary of a Special Agent…*. The error is detected and the proposal is still better than the planted error, but volume information is lost. qwen does this more often than gpt.

## 4. Robustness: changed covers

Ten books had their cover swapped for another book's cover, and ten had it deleted. Where the metadata itself was correct, any title or author proposal is a false alarm.

| | qwen3.5:4b | gpt-5.4-nano |
|---|---:|---:|
| Correct books with a changed cover | 10 | 10 |
| Left alone | 6 | **8** |
| False alarm: title shortened | 2 | 2 |
| **False alarm: misled by the cover** | **2** | **0** |

qwen took the cover over the text twice:
- **#572** *The Mysterious Key and What It Opened* (Louisa May Alcott) became *A Sicilian Romance* (Ann Ward Radcliffe), the book on the swapped cover.
- **#819** *The Coming Night* (Edward Hoare) became *Mercadet: A Comedy in Three Acts* (Honoré de Balzac), again the book on the cover.

These are the worst kind of error, because accepting them renames a book as a different book. gpt never did this. On the 10 books with both a planted error and a changed cover, the two models scored the same.

## 5. Unrequested changes on the untouched books

468 books have no planted error and an unchanged cover. Every title or author change proposed for them is either a false alarm or a real error that was already in loc-test.

| Proposals on the 468 untouched books | qwen3.5:4b | gpt-5.4-nano |
|---|---:|---:|
| Books with a title or author change | 176 (38%) | **122 (26%)** |
| title shortened (subtitle or volume dropped) | 147 | 99 |
| title made longer | 6 | 6 |
| title different | 10 | 3 |
| authors changed | 30 | 18 |
| series proposed | 17 | 84 (25 of them "Project Gutenberg eBook [n]") |
| publisher proposed | 427 | 415 |
| year proposed | 296 | 309 |

The two models agree on 109 of these books. qwen alone accounts for another 67, and gpt alone for 13.

- **Titles:** most proposals shorten the title, e.g. *Carols of Cockayne / The Third Edition, 1874* becomes *Carols of Cockayne*. Proposals like this are mostly noise. When they drop "Vol. 4 (of 9)", accepting them does harm. qwen does this about 50% more often.
- **Authors:** most changes switch to the name as printed on the title page, e.g. *Sir Richard Francis Burton* to *Richard F. Burton*, or *Mrs. Barbauld* to *Anna Lætitia Barbauld*. That's harmless, and sometimes an improvement: *Duchess* to *Mrs. Hungerford* is right.
  - **Real mistakes by qwen:** it dropped Engels from *Marx & Engels*, and put the translator William Withering in place of Torbern Bergman. That's the same mistake as on planted book #1050.
  - **Real mistake by gpt:** it replaced the Duke of Wellington with *George Henry Francis* (#721).
- **Series:** qwen's 17 are plausible, e.g. *Novels of Paul de Kock [11]* or *Dime Library [49]*. gpt proposes five times as many.
  - 25 are the Gutenberg eBook number, which is wrong every time.
  - Others are publisher imprints written in capitals, e.g. *TAUCHNITZ EDITION [2719]* or *BOHN'S CLASSICAL LIBRARY*.

**Consistency:** 197 books appear twice in loc-test (MOBI and EPUB) with no planted error. The table counts the twin pairs that got the same proposal on both copies:

| Same proposal on both copies | qwen3.5:4b | gpt-5.4-nano |
|---|---:|---:|
| title | 192 / 197 | 189 / 197 |
| authors | 195 / 197 | 192 / 197 |
| series | 194 / 197 | 171 / 197 |
| publisher | 181 / 197 | 162 / 197 |
| year | 192 / 197 | 187 / 197 |

qwen is steadier, especially on series and publisher, where gpt's answers change with the format it reads.

## 6. Conclusions and suggestions

1. **Model choice:** prefer **gpt-5.4-nano** for the title and author review.
   - Same speed.
   - Slightly more exact fixes.
   - About 30% fewer unrequested title and author changes.
   - Never fooled by a wrong cover.

   Keep qwen3.5:4b as the offline, no-cost fallback. Its cover mistakes mean its title and author proposals need checking one by one.
2. **Changes to the program**, whichever model is used:
   - Drop proposed series matching "Project Gutenberg eBook [n]" (or tell the AI in the prompt that it isn't a series). That removes 25 of gpt's 84 series proposals, and #635.
   - Consider not proposing a title that only drops the subtitle or volume part of the current one. That's 99 to 147 proposals per run of little value, some of them harmful.
   - Record the content-filter categories in the review log, to confirm what Azure objects to.
3. **For the next comparison:**
   - Add a few planted series errors whose number *is* printed in the book, so series can be scored.
   - Grade publisher and year: this test only scores titles, authors and series, and both models propose a publisher for about 90% of books.

## Files

All in `C:\calibre`:

- **`loc-test_model_comparison.md`:** this report.
- **`loc-test_compare_models.py`:** produces the numbers above. It reuses the scoring of `loc-test_check_review.py` and the log reading of `loc-test_run_metrics.py`, and runs on the two newest review runs, or on two run files given as arguments:

  ```
  C:\Github\CalibreDedup\.venv\Scripts\python.exe C:\calibre\loc-test_compare_models.py
  ```
- **`loc-test_compare_models.csv`:** every planted error, changed cover and proposal on an untouched book, with both models' results side by side.
- **`loc-test_run_metrics.csv`:** the performance of both runs, from `loc-test_run_metrics.py`.
