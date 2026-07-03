# Universal Book Chapter Splitter — Architecture & SRS

**Mission.** Accept any DOCX book and split it into one DOCX per chapter while
preserving formatting, images, tables, styles, headers/footers, and page
settings as close to perfectly as the format physically allows.

This document is the design + Software Requirements Specification for the engine
implemented in the accompanying `book_splitter/` package. It was validated
end-to-end on two deliberately contrasting real books:

| Book | Structure class | What it exercises |
|------|-----------------|-------------------|
| *The Richest Man in Babylon* | Manually formatted: heading styles defined but **never applied**; no TOC; no page breaks; body 11pt, chapter titles **bold ~20pt split across 2–3 paragraphs** | Relative font-size clustering + multi-paragraph title merge + visual/text fallback |
| *Quiver, don't Quake* | Styled: a Word **TOC field + a Contents page**, custom `Title`/`Subtitle`/`chapter` styles, 25 page breaks, plus internal "Section/Part N" lines that are **not** real chapters | TOC-authoritative detection that overrides misleading visual cues |

A design honesty note carried over from the analysis phase: **"100% detection on
every book" is not an achievable target** — there is no universal ground truth for
"chapter," valid answers differ by reader intent, and structure is sometimes
simply absent from the bits. The engine therefore targets *near-perfect format
preservation* (which is genuinely attainable, because we trim rather than
rebuild) and *calibrated, abstaining detection* that reaches ~95%+ on
conventional books and refuses to guess when it cannot.

---

## 1. Architecture diagram

A rendered diagram ships alongside this document
(`universal_document_splitter_architecture.svg`). In text form, the production
pipeline is:

```
                    ┌──────────────────────────────────────────────┐
   any .docx ─────► │ STAGE 0  Package Loader (docx_package.py)     │
                    │  read every ZIP part verbatim; parse only      │
                    │  word/document.xml; keep all other parts raw   │
                    └───────────────┬──────────────────────────────┘
                                    ▼
                    ┌──────────────────────────────────────────────┐
                    │ STAGE 1  Feature Extraction (blocks.py)        │
                    │  StyleResolver (basedOn chain, cycle-guarded)  │
                    │  Block features: text, style level, bold, size,│
                    │  caps, alignment, page-break, word count        │
                    └───────────────┬──────────────────────────────┘
                                    ▼
                    ┌──────────────────────────────────────────────┐
                    │ STAGE 2  Baselines + Candidate Merge           │
                    │  body size; relative heading band by clustering│
                    │  merge consecutive heading paragraphs → 1 title│
                    └───────────────┬──────────────────────────────┘
                                    ▼
          ┌───────── LAYERED DETECTION (detector.py) ───────────────┐
          │  L1  TOC Intelligence (toc.py)   ◄── authoritative when  │
          │       detect → extract → match        it matches well    │
          │  L2  Style / outline level                               │
          │  L3  Visual + text fallback (signals.py + scoring.py)    │
          └───────────────┬─────────────────────────────────────────┘
                          ▼
                    ┌──────────────────────────────────────────────┐
                    │ STAGE 4  Confidence Scoring + Level Inference  │
                    │  weighted signal fusion; penalties; bands      │
                    │  (optional) LLM fallback on REVIEW-band blocks │
                    └───────────────┬──────────────────────────────┘
                                    ▼
                    ┌──────────────────────────────────────────────┐
                    │ STAGE 5  Plan Assembly + Abstention Gate       │
                    │  contiguous ranges; front matter; <2 boundaries│
                    │  → single-file passthrough (never shred)       │
                    └───────────────┬──────────────────────────────┘
                                    ▼
                    ┌──────────────────────────────────────────────┐
                    │ STAGE 6  Loss-free Emitter (splitter.py)       │
                    │  clone package per chapter; rewrite ONLY        │
                    │  document.xml body + preserved sectPr           │
                    └───────────────┬──────────────────────────────┘
                                    ▼
              one .docx per chapter + JSON report + verifier
```

---

## 2. Components

| Module | Responsibility | Key interface |
|--------|----------------|---------------|
| `docx_package.py` | **Format-preservation core.** Loads the OOXML ZIP, keeps every part as raw bytes, parses only `document.xml`. Clones the package per chapter, rewriting one part. | `DocxPackage(path)`, `.block_children()`, `.clone_with_blocks(blocks)`, `.write(path, data)` |
| `blocks.py` | Feature extraction. `StyleResolver` resolves the `basedOn` inheritance chain (cycle-guarded) to a heading level; `Block` exposes detection-ready features. | `StyleResolver`, `Block`, `build_blocks()`, `body_baseline_size()` |
| `toc.py` | **TOC Intelligence Module.** Detects the TOC region, extracts entries, matches them back to body candidates via monotonic fuzzy alignment. | `detect_toc_region()`, `extract_entries()`, `match_to_body()` |
| `signals.py` | The 16 detection signals, each a pure predicate returning a strength in [0,1] with documented FP/FN risk. | `SIGNALS` registry |
| `scoring.py` | Weighted confidence model: fuses signals, applies body-like penalties, infers level, defines accept/review bands. | `score_block()`, `WEIGHTS`, `ACCEPT`, `REVIEW` |
| `detector.py` | Orchestrator: baselines, candidate merge, layered detection, TOC-authority decision, plan assembly, abstention. | `detect(package, level_filter)` → `ChapterPlan` |
| `splitter.py` | Emitter: writes one preserved DOCX per chapter + manifest. | `split(package, plan, out_dir)` |
| `llm_fallback.py` | Optional LLM layer (off by default): interface, caching, cost cap, failure handling; deterministic offline stub. | `NullLLM`, `HeuristicLLM`, `build_prompt()` |
| `cli.py` | argparse CLI: glob inputs, `--out`, `--level`, `--llm`, `--report`, `--dry-run`, `--verbose`. | `python -m book_splitter ...` |
| `verify.py` | Preservation verifier / benchmark helper: proves non-document parts are byte-identical to source. | `python -m book_splitter.verify orig.docx dir/` |

---

## 3. Data flow

1. **Ingest.** `DocxPackage` reads the ZIP. Member order and raw bytes of every
   part are retained; only `word/document.xml` is parsed into an lxml tree. This
   is the contract that makes preservation possible: *we never reconstruct
   parts, we only ever trim the body.*
2. **Model.** `build_blocks()` wraps each body child (paragraph, table, content
   control) in a `Block` with resolved features. `StyleResolver` turns style ids
   into semantic heading levels by walking `basedOn`.
3. **Calibrate.** `compute_baselines()` finds the body font size (modal run
   size) and a *relative* heading band (the most frequent size meaningfully
   above body, capped below title-page extremes). No absolute "16pt" rule.
4. **Generate candidates.** Consecutive heading-like paragraphs are merged into
   single boundaries (`_MergedView`) so multi-line titles count once.
5. **Detect (layered).** TOC module runs; if it aligns to a healthy share of the
   body, its boundaries are authoritative. Otherwise every merged candidate is
   scored and thresholded.
6. **Assemble.** Boundaries become contiguous `[start, end)` block ranges; pre-
   first-boundary content becomes *Front Matter*. If fewer than two confident
   boundaries exist, the engine **abstains** (single passthrough file).
7. **Emit.** For each chapter, `clone_with_blocks()` deep-copies the document
   tree, empties the body, re-inserts the chapter's blocks plus the preserved
   final `sectPr`, and re-zips with all other parts untouched.
8. **Report & verify.** A JSON report records diagnostics, per-chapter
   confidence, and fired signals; `verify.py` confirms preservation.

---

## 4. Detection pipeline (layered)

The engine is a **priority cascade of reliability classes**, not a flat union of
heuristics. Higher layers, when confident, *override* lower ones.

- **L1 — TOC Intelligence (highest authority).** A Table of Contents is
  author-declared structure. When `match_to_body()` aligns at least
  `max(5, 50% of entries)`, the matched boundaries become the *sole* split set.
  This is what stops *Quiver*'s internal "Section 1 / Part 2" lines (absent from
  the TOC) from causing false splits.
- **L2 — Style / outline level.** Resolved heading/`Title` styles and explicit
  `outlineLvl`. Reliable when authors actually applied styles. Used directly
  when there is no usable TOC.
- **L3 — Visual + text fallback.** Relative font band, bold, all-caps,
  centering, chapter/part keywords, roman/numeric patterns, short-text gating,
  and structural isolation. This is the only layer that fires for *Babylon*,
  whose styles are unused — and it recovers every chapter.

Two pipeline operations are decisive for real books:

- **Multi-paragraph title merge.** *Babylon* titles such as "The Man Who" /
  "Desired Gold" are separate paragraphs; the merge step groups consecutive
  same-signature heading paragraphs (gap ≤ 2) into one boundary.
- **Duplicate-title suppression.** A near-duplicate recap heading
  ("THE FIVE LAWS OF GOLD" after "The Five Laws of Gold") normalizes to an
  already-seen title and is dropped, preventing a spurious split inside a
  chapter.

---

## 5. Confidence scoring system

Each signal contributes `strength × weight`. The block's raw score is the sum,
minus body-like penalties; confidence = `min(1, raw / 1.6)`. Bands:
**ACCEPT = 0.62** (auto-split), **REVIEW = 0.42** (flag / route to optional LLM),
below 0.42 = reject. Weights rank signals by how often each is *right* across
book classes — semantic/structural signals dominate, visual signals support.

### Per-signal scoring specification

| Signal | Why it matters | Weight | False-positive risk | False-negative risk |
|--------|----------------|:-----:|---------------------|---------------------|
| `TOC_MATCH` | Author-declared structure; the strongest cue | 1.00 | ~0 (already validated vs body) | Books with no TOC (never fires) |
| `CHAPTER_KEYWORD` | Explicit "Chapter N" declaration | 0.95 | Cross-refs ("see Chapter 3") — mitigated by start-anchoring | Named chapters without the word |
| `CUSTOM_CHAPTER_STYLE` | Bespoke `chapter` style = explicit marker | 0.90 | Low | Only present in some templates |
| `HEADING_STYLE` | Semantic heading/`Title` style, appearance-independent | 0.85 | `Title` reused for front matter — mitigated by TOC + position | Manually formatted books (Babylon) |
| `PART_KEYWORD` | "Part/Book/Volume N" — the higher level | 0.80 | "part of…" — mitigated by requiring a number | Unnumbered parts |
| `OUTLINE_LEVEL` | Explicit `outlineLvl` contract | 0.75 | Low | Absent in casual documents |
| `FONT_SIZE` | In the document's *relative* heading band | 0.70 | Pull-quotes/drop-caps/title page — mitigated by band bounds | Styled-but-not-enlarged titles |
| `PAGE_BREAK_BEFORE` | Chapters usually start on a new page | 0.45 | Any deliberate page break | Continuous-flow books |
| `SECTION_BREAK` | Odd/even-page section start (pro layouts) | 0.45 | Column/layout sections — limited to oddPage/evenPage | Single-section documents |
| `ROMAN_NUMERAL` | Classic fiction chapter marker | 0.45 | "I" pronoun, "Henry VIII" — isolation required | Arabic-numbered books |
| `ISOLATION` | Surrounded by blank, followed by body text | 0.40 | Low | Dense layouts without spacing |
| `CENTERED` | Chapter openers are often centered | 0.30 | Centered captions/quotes | Left/right-aligned titles |
| `NUMERIC_CHAPTER` | "3 The Method" numbered style | 0.30 | Numbered lists/figures | Named chapters |
| `ALL_CAPS` | All-caps titling convention | 0.30 | Emphasis/promo text — duplicates dropped later | Mixed-case titles |
| `BOLD` | Bold short lines as manual markers | 0.25 | Heavily overloaded (key terms) | Regular-weight titles |
| `SHORT_TEXT` | Titles are short, unpunctuated | 0.20 | Short body/list lines | Long descriptive titles |

**Penalties (subtractive).** Terminal punctuation on an 8+-word line (−0.5,
"looks like a sentence"); more than 16 words (−0.6, "too long to be a title").
These stop a bold full sentence from masquerading as a chapter.

**Level inference.** `PART_KEYWORD` → *part*; `TOC_MATCH`/`CHAPTER_KEYWORD`/
custom style / style-level-0 / font-band → *chapter*; weaker combinations →
*section*. Level drives the `--level` filter and the output labelling.

**Why fusion beats any single rule.** On *Babylon*, "The Man Who Desired Gold"
scores via `FONT_SIZE`+`BOLD`+`SHORT_TEXT` (no styles, no TOC); on *Quiver*,
every real boundary scores 1.0 via `TOC_MATCH`; the misleading "Section 1"
internal lines never enter the boundary set because the TOC layer is
authoritative. Same model, different signals carry the weight per book.

---

## 6. TOC Intelligence Module

**Goal.** Convert any of four TOC kinds into authoritative boundaries:
generated Word TOC field, hyperlinked TOC, manual "Contents" page, TOC-styled
entries (`TOC1..TOC9`).

**Architecture (three stages).**

1. **Detect region.** Locate the first TOC-styled paragraph *or* a "Contents"/
   "Table of Contents" heading. If TOC styles exist, the region ends at the
   **last** TOC-styled paragraph (this prevents the region from greedily
   swallowing the first real heading — the bug that initially hid *Quiver*'s
   Prologue). For a manual contents page (no TOC styles), consume the run of
   short, unpunctuated entry lines, stopping on a large blank gap or a body
   paragraph.
2. **Extract entries.** Ordered titles with page-number/dot-leader suffixes
   stripped and a coarse level guess (*part* vs *chapter*).
3. **Match to body.** Align each entry to the best heading candidate **after**
   the TOC region.

**Matching algorithm (monotonic fuzzy alignment).**

- Normalize both sides: lowercase, strip punctuation and trailing page numbers.
- Score each (entry, candidate) pair with `SequenceMatcher` ratio, **boosted to
  ≥ 0.92 on prefix/substring containment** (so "Interlude" matches "Interlude:
  Why we should quake", and a "Chapter 1" label merged onto a title still
  matches the bare TOC title).
- Walk entries with a **monotonic cursor**: each accepted match must occur later
  in the body than the previous one. This is what resolves **duplicate titles**
  (two "Introduction"s map to their two occurrences in order) and prevents
  backwards/cross alignment.
- Accept at ratio ≥ 0.72; an entry that finds no confident match is dropped and
  reported, never forced onto an unrelated paragraph.

**Ambiguities handled:** page-number mismatches (stripped before compare);
duplicate titles (monotonic cursor); multi-level TOCs (part vs chapter level
retained on each entry); multi-paragraph body titles (matched against the merged
candidate text).

---

## 7. LLM fallback layer

**Rule first, LLM only on doubt.** The deterministic engine decides everything
it can. The LLM is consulted **only** for blocks in the REVIEW band (0.42–0.62)
— never for confident yes/no — so a typical book triggers a handful of calls or
none.

**Question.** "Is this paragraph a chapter heading?" → strict JSON
`{is_heading, confidence, reason}`.

**Inputs.** Paragraph text; previous/next paragraph (neighbour window); style
metadata (id, resolved level, bold, size-vs-body); layout metadata (alignment,
page-break-before, word count).

**Design.**

1. **Prompting strategy.** A strict system instruction plus a compact
   JSON-only schema for cheap, deterministic parsing (see `build_prompt`).
2. **Thresholds.** Only REVIEW-band blocks are sent; the model score is blended
   with the rule score by `max`, so the LLM can rescue a miss but never drags a
   confident rule decision downward.
3. **Cost optimisation.** Batch all borderline blocks into one call; short
   neighbour windows; a hard `max_calls` cap per document.
4. **Caching.** Keyed by a hash of (text + style + layout); re-runs and repeated
   near-identical lines cost nothing.
5. **Failure handling.** Any error (timeout, bad JSON, no provider) silently
   falls back to the rule score. The engine **never hard-depends** on the LLM.

The shipped default is `NullLLM` (disabled). `HeuristicLLM` is a deterministic
offline stand-in that exercises the same contract for testing; the real provider
call-site is marked in `llm_fallback.py`.

---

## 8. Failure recovery strategy

| Failure mode | Strategy |
|--------------|----------|
| No detectable structure / < 2 confident boundaries | **Abstain**: emit the whole document as one valid file + reason. Never shred a book into noise. |
| Misleading visual cues (internal "Section/Part" lines) | TOC authority overrides L2/L3 when a TOC matches well. |
| Multi-paragraph titles | Merge consecutive heading paragraphs before scoring. |
| Duplicate/recap headings | Normalize and drop already-seen titles. |
| Circular `basedOn` style chains | Cycle-guarded resolver returns safely. |
| Malformed/edge XML | `huge_tree` parser; body-absent raises a clear error; non-document parts are never parsed so they cannot fail. |
| Adjacent boundaries → empty slice | Empty slices are skipped in the emitter. |
| Low-confidence boundaries | Reported with confidence + fired signals so a human can review; optional LLM rescue. |

**Guiding principle:** degrade to a *correct, conservative* output (the unsplit
book) rather than a confident wrong one.

---

## 9. Performance considerations

- **Single parse.** Only `document.xml` is parsed; all other parts stay as
  opaque bytes — the expensive embedded fonts and media are never deserialized.
- **Linear detection.** Feature extraction, baselines, and scoring are O(blocks).
  *Babylon* (2,828 paragraphs) and *Quiver* (1,191) both process in well under a
  second of detection time.
- **Emit cost = clone cost.** Each chapter deep-copies the document tree and
  re-zips the package. Because every part is copied, **per-chapter file size ≈
  source size** (Babylon chapters ≈ 4.1 MB each — they carry the 9 MB of
  embedded fonts). This is the deliberate price of *byte-perfect preservation*.
- **Optimisation knob (future).** An opt-in "prune unreferenced media/fonts"
  pass could shrink outputs, at the cost of strict byte-identity. Off by default
  precisely because the requirement is 100% preservation.

---

## 10. Scalability considerations

- **Stateless per book.** Each document is independent → trivially parallel/
  horizontally scalable (process pool, queue workers, or serverless fan-out).
- **Streaming-friendly emit.** Cloning writes to an in-memory buffer; can be
  swapped for streamed ZIP writes for very large books.
- **Pluggable layers.** New input formats (EPUB/PDF) attach as alternative
  Stage-0/Stage-1 adapters feeding the same Block model; new signals register in
  `SIGNALS`; weights are overridable per document class.
- **LLM budget control.** Caching + per-document call caps keep cost bounded and
  predictable under batch load.


---

## 11. MVP implementation plan (development roadmap)

### Phase 1 — Works on ~80% of books, no LLM

- **Folder structure:** `book_splitter/{docx_package, blocks, signals, scoring,
  detector, splitter, cli}.py`.
- **Modules & responsibilities:** package clone-and-trim (preservation), block
  feature extraction, the visual+style signal set, weighted scoring, contiguous
  plan assembly, CLI.
- **Interfaces:** `DocxPackage`, `detect(pkg)→ChapterPlan`,
  `split(pkg, plan, out)`.
- **Testing:** golden-plan tests on styled and manually-formatted samples;
  preservation assertions (parts byte-identical).
- **Datasets:** a styled book, a manually-formatted book (the two provided),
  plus 8–10 public-domain DOCX conversions.
- **Success metrics:** ≥ 80% of books split with no missing/extra chapters;
  100% of outputs valid and preservation-clean.

### Phase 2 — Works on ~95% of books, optional LLM fallback

- **Adds:** `toc.py` (TOC Intelligence) and `llm_fallback.py`.
- **Responsibilities:** TOC-authoritative detection; REVIEW-band escalation to
  the LLM with caching and cost caps.
- **Interfaces:** `detect_toc_region/extract_entries/match_to_body`;
  `LLM.classify(...)`.
- **Testing:** TOC alignment tests (duplicates, page numbers, multi-level);
  LLM-path tests against the deterministic stub; ablation (rules-only vs
  rules+LLM).
- **Datasets:** add TOC-bearing books, anthologies, fiction with roman numerals,
  bilingual editions.
- **Success metrics:** ≥ 95% correct boundaries on the benchmark; LLM invoked on
  < 2% of paragraphs; zero preservation regressions.

### Phase 3 — Production-grade

- **Adds:** `verify.py` (already shipped), benchmark harness, structured logging,
  metrics, config-driven weights, parallel batch runner, optional EPUB/PDF
  adapters.
- **Responsibilities:** observability (per-signal metrics, confidence
  histograms), human-in-the-loop review export, packaging/deployment.
- **Interfaces:** stable CLI + library API; report schema versioned.
- **Testing:** large regression corpus, fuzzed/malformed inputs, performance
  budgets, CI gating on preservation + detection F1.
- **Datasets:** 200+ book corpus across genres, languages, and converters.
- **Success metrics:** detection F1 ≥ 0.95, abstention precision high (when it
  splits, it is right), p95 latency budget met, 100% preservation.

---

## 12. Software Requirements Specification

### 12.1 Functional requirements

- **FR1 Universal detection.** Detect chapter boundaries across styled, manually
  formatted, TOC-bearing, and multi-level books.
- **FR2 DOCX preservation.** Each output retains styles, numbering, theme,
  embedded fonts, images, tables, headers/footers, and page settings.
- **FR3 TOC analysis.** Detect and exploit Word/manual/hyperlinked/styled TOCs;
  match entries to body content with ambiguity resolution.
- **FR4 Multi-level structures.** Distinguish part vs chapter; support a
  `--level` filter.
- **FR5 Optional AI fallback.** Pluggable LLM consulted only on low-confidence
  blocks, fully optional.
- **FR6 CLI.** argparse interface with glob inputs, output dir, level, llm,
  report, dry-run, verbose.
- **FR7 Benchmark/verify.** A verifier proves preservation; a report records
  detection diagnostics.
- **FR8 Logging & metrics.** Per-book diagnostics (body size, heading band, TOC
  match count, authority flag, accepted boundaries) and per-chapter confidence +
  fired signals.
- **FR9 Extensibility.** New signals, weights, and input adapters add without
  touching the core.

### 12.2 Non-functional requirements

- **NFR1 Correctness/preservation:** non-document parts byte-identical to source;
  outputs pass OOXML validation.
- **NFR2 Robustness:** never crash on malformed input; abstain rather than
  mis-split.
- **NFR3 Performance:** linear in block count; single parse of `document.xml`.
- **NFR4 Determinism:** identical input → identical output (LLM off).
- **NFR5 Portability:** pure-Python + lxml; no network required by default.
- **NFR6 Observability:** structured JSON report; verifiable claims.
- **NFR7 Maintainability:** small, single-responsibility modules; documented
  signals and weights.

### 12.3 Architecture & 12.4 Data flow

See sections 1–4. Layered detection (TOC ▸ style ▸ visual) feeding a weighted
scorer and an abstaining plan assembler, with a clone-and-trim emitter that
guarantees preservation.

### 12.5 Testing strategy

- **Unit:** style-chain resolution, each signal's fire/no-fire, TOC matching
  edge cases.
- **Golden:** expected chapter plans for reference books (the two provided are
  the first two golden cases).
- **Preservation:** `verify.py` asserts byte-identity of all non-document parts
  and a valid trimmed body with `sectPr`.
- **Validation:** OOXML validator on a sample of outputs.
- **Ablation:** rules-only vs rules+LLM; TOC-on vs TOC-off.
- **Property/fuzz:** malformed XML, empty docs, single-heading docs (abstain
  path).

### 12.6 Risks

| Risk | Mitigation |
|------|------------|
| Detection ambiguity (no universal "chapter") | Calibrated confidence + abstention + `--level`; report surfaces uncertainty |
| Style-overloaded `Title` (part = chapter = front matter) | TOC authority + position + duplicate suppression |
| Large outputs from preserved fonts/media | Documented; optional future pruning pass |
| LLM cost/latency | Off by default; REVIEW-band only; caching + caps |
| Non-DOCX inputs | Out of scope v1; adapter pattern reserved for EPUB/PDF |

### 12.7 Future enhancements

EPUB/PDF input adapters; concurrent structural layers (OHCO) output; intent-
parameterized split (read vs RAG-chunk vs print); media/font pruning mode;
human-in-the-loop review UI; learned weights per document class.

### 12.8 Deployment strategy

Ship as a pip-installable package + CLI. Library API for embedding. Batch mode
via process pool or serverless fan-out (stateless per book). Versioned report
schema. CI gates: preservation verifier + detection F1 on the golden corpus must
pass before release.

### 12.9 Success criteria

- 100% of emitted files valid and preservation-clean (**met** on both samples:
  37/37 files, all non-document parts byte-identical, all OOXML-valid).
- ≥ 95% correct boundaries on conventional books (**met** on both samples:
  Babylon all chapters recovered; Quiver matches its TOC exactly).
- Abstains rather than mis-splits when structure is absent.
- LLM optional and bounded; deterministic by default.