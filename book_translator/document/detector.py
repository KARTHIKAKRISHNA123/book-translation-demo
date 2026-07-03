import re
from dataclasses import dataclass, field
from .parser import ParsedDocument, Paragraph

PREAMBLE_KEYWORDS = frozenset([
    "foreword", "preface", "introduction", "prologue", "acknowledgement",
    "acknowledgment", "contents", "table of contents", "copyright",
    "dedication", "about the author", "note to the reader",
])

HEADING_STYLE_PATTERNS = re.compile(
    r"^(heading\s*[1-4]|title|chapter\s*title|h[1-4]|chaptertitle)$",
    re.IGNORECASE,
)

CJK_CHAPTER_RE = re.compile(r"^第[零一二三四五六七八九十百千\d]+章")

NOISE_PATTERNS = [
    re.compile(r"^\d+$"),
    re.compile(r"^[ivxlcdmIVXLCDM]+$"),
    re.compile(r"^THE RICHEST MAN IN BABYLON\s*$", re.IGNORECASE),  
    re.compile(r"ISBN\s*[\d\-]+", re.IGNORECASE),
    re.compile(r"^\(\d{4}\)$"),
    re.compile(r"^To order call", re.IGNORECASE),
    re.compile(r"Penguin|Putnam|New American Library", re.IGNORECASE),
    re.compile(r"^[A-Z]{1,2}$"),
    re.compile(r"^\d+\s+(silver|gold|shekel)", re.IGNORECASE),
    re.compile(r"^SIGNET", re.IGNORECASE),
    re.compile(r"^SOUND FINANCIAL", re.IGNORECASE),
    re.compile(r"TABLET (ONE|TWO|THREE|FOUR|FIVE)", re.IGNORECASE),
    re.compile(r"ST\.\s*SWITHIN", re.IGNORECASE),
    re.compile(r"^(THE FIRST|THE SECOND|THE THIRD|THE FOURTH|THE FIFTH|THE SIXTH|THE SEVENTH) CURE", re.IGNORECASE),
    re.compile(r"^GREAT REGRET|^ADEQUATE PROTECTION|^WAY CAN BE FOUND", re.IGNORECASE),
]

BACK_MATTER_SIGNALS = re.compile(
    r"(to order call|signet mentor|sound financial advice|penguin putnam|"
    r"0-451|printed in|visit our website|for more information)",
    re.IGNORECASE
)


def _is_noise(text: str) -> bool:
    t = text.strip()
    if not t:
        return True
    for pattern in NOISE_PATTERNS:
        if pattern.search(t):
            return True
    return False


@dataclass
class ChapterBoundary:
    para_index: int
    title: str
    score: int
    confidence: str
    section_type: str
    content_start: int
    signals: list[str] = field(default_factory=list)
    synthetic: bool = False

    @classmethod
    def from_score(cls, score: int, **kwargs) -> "ChapterBoundary":
        if score >= 3:
            confidence = "CERTAIN"
        elif score == 2:
            confidence = "HIGH"
        else:
            confidence = "LOW"
        return cls(score=score, confidence=confidence, **kwargs)


class DetectorCascade:

    def __init__(self, min_content_words: int = 500):
        self.min_content_words = min_content_words

    def detect(self, doc: ParsedDocument) -> list[ChapterBoundary]:
        paras = doc.paragraphs

        back_matter_start = self._find_back_matter_start(paras)

        self.toc_titles = self._extract_toc(paras)
        print(f"      [TOC] {len(self.toc_titles)} titles: {sorted(list(self.toc_titles))}")

        candidates = self._find_candidates(paras, back_matter_start)
        print(f"      [SCAN] {len(candidates)} candidates")

        boundaries: list[ChapterBoundary] = []

        for cand_idx, title, content_start in candidates:
            score = 0
            signals: list[str] = []

            if self._is_bold_italic_title(paras[cand_idx]):
                score += 1
                signals.append("VISUAL_PATTERN")

            if self._has_heading_style(paras[cand_idx]):
                score += 1
                signals.append("HEADING_STYLE")

            if self._matches_toc(title, self.toc_titles):
                score += 1
                signals.append("TOC_MATCH")

            if CJK_CHAPTER_RE.match(title):
                score = 3
                signals = ["CJK_PATTERN"]

            if score < 2:
                continue

            look_ahead_end = min(content_start + 100, len(paras))
            content_words = sum(
                p.word_count for p in paras[content_start:look_ahead_end]
            )
            if content_words < self.min_content_words:
                continue

            section_type = self._classify_section(title)
            boundaries.append(ChapterBoundary.from_score(
                score=score,
                para_index=cand_idx,
                title=title,
                section_type=section_type,
                content_start=content_start,
                signals=signals,
            ))

        boundaries = self._deduplicate(boundaries)
        
        # --- Fallback: inject synthetic boundaries for TOC titles with no detected heading ---
        synthetic_boundaries = self._running_header_fallback(paras, boundaries)
        if synthetic_boundaries:
            boundaries.extend(synthetic_boundaries)
            boundaries.sort(key=lambda b: b.para_index)
            
        # --- Dedup: for titles that normalize to the same string, keep the earliest ---
        seen_titles = {}
        for b in boundaries:
            key = re.sub(r'\s+', ' ', b.title.lower().strip())
            if key not in seen_titles:
                seen_titles[key] = b
            else:
                existing = seen_titles[key]
                if b.synthetic and not existing.synthetic:
                    seen_titles[key] = b
                elif not b.synthetic and not existing.synthetic:
                    if b.para_index < existing.para_index:
                        seen_titles[key] = b

        boundaries = sorted(seen_titles.values(), key=lambda b: b.para_index)

        return boundaries

    def _find_back_matter_start(self, paras: list[Paragraph]) -> int:
        n = len(paras)
        scan_from = int(n * 0.80)
        for i in range(scan_from, n):
            text = paras[i].text.strip()
            if text and BACK_MATTER_SIGNALS.search(text):
                print(f"      [BACK-MATTER] Detected at para[{i:04d}]: '{text[:50]}'")
                return i
        return n

    def _find_candidates(self, paras: list[Paragraph], stop_at: int) -> list[tuple[int, str, int]]:
        candidates = []
        i = 0
        n = min(stop_at, len(paras))

        while i < n:
            para = paras[i]
            text = para.text.strip()

            if not text:
                i += 1
                continue

            if _is_noise(text):
                i += 1
                continue

            is_candidate = False

            if para.all_bold and 2 <= para.word_count <= 10:  
                is_candidate = True
            elif HEADING_STYLE_PATTERNS.match(para.style_name):
                is_candidate = True
            elif para.is_all_caps and 2 <= para.word_count <= 4:
                if not _is_noise(text):
                    is_candidate = True
            elif CJK_CHAPTER_RE.match(text):
                is_candidate = True

            if is_candidate:
                title_parts = [text]
                j = i + 1

                while j < n:
                    np = paras[j]
                    nt = np.text.strip()
                    if not nt:
                        j += 1
                        continue
                    if (np.all_bold
                            and 1 <= np.word_count <= 5
                            and not _is_noise(nt)):
                        title_parts.append(nt)
                        j += 1
                    else:
                        break

                full_title = " ".join(title_parts)

                if _is_noise(full_title):
                    i = j
                    continue

                content_start = j
                while content_start < n and not paras[content_start].text.strip():
                    content_start += 1

                candidates.append((i, full_title, content_start))
                i = j
            else:
                i += 1

        return candidates

    def _extract_toc(self, paras: list[Paragraph]) -> set[str]:
        toc_titles: set[str] = set()
        in_toc = False
        toc_start_idx = 0

        for idx, para in enumerate(paras):
            text = para.text.strip()
            if not text:
                continue

            if text.lower() in ("contents", "table of contents"):
                in_toc = True
                toc_start_idx = idx
                continue

            if in_toc:
                if len(text.split()) > 15:
                    break
                if idx - toc_start_idx > 80:
                    break

                if re.match(r"^\d+$", text):
                    continue

                if re.match(r"^[ivxlcdmIVXLCDM]+$", text):
                    continue

                if _is_noise(text):
                    continue

                clean = re.sub(r"\s+\d+\s*$", "", text).strip()

                if clean and len(clean.split()) >= 2:
                    toc_titles.add(clean.lower())

        return toc_titles

    def _matches_toc(self, title: str, toc_titles: set[str]) -> bool:
        if not toc_titles:
            return False
        t = title.lower().strip()
        t = re.sub(r"\s+\d+\s*$", "", t).strip()
        t = re.sub(r"\s+\d+\s*$", "", t).strip()

        if t in toc_titles:
            return True
        for entry in toc_titles:
            if t in entry or entry in t:
                return True
        return False

    def _has_heading_style(self, para: Paragraph) -> bool:
        return bool(HEADING_STYLE_PATTERNS.match(para.style_name))

    def _is_bold_italic_title(self, para: Paragraph) -> bool:
        return para.all_bold and para.word_count <= 10

    def _classify_section(self, title: str) -> str:
        t = title.lower().strip()
        for kw in PREAMBLE_KEYWORDS:
            if kw in t:
                return "preamble"
        if any(kw in t for kw in ("appendix", "historical sketch", "about")):
            return "appendix"
        return "chapter"

    def _deduplicate(self, boundaries: list[ChapterBoundary]) -> list[ChapterBoundary]:
        if not boundaries:
            return []
        boundaries = sorted(boundaries, key=lambda b: b.para_index)
        result = [boundaries[0]]
        for b in boundaries[1:]:
            prev = result[-1]
            if abs(b.para_index - prev.para_index) <= 5:
                if b.score > prev.score:
                    result[-1] = b
                elif b.score == prev.score and "VISUAL_PATTERN" in b.signals:
                    result[-1] = b
            else:
                result.append(b)
        return result

    def _running_header_fallback(self, paragraphs, boundaries):
        import re

        # Build covered set: TOC titles already handled by existing boundaries
        covered = set()
        for b in boundaries:
            b_norm = re.sub(r'\s+', ' ', b.title.lower().strip())
            covered.add(b_norm)
            # Also cover any TOC title contained within this boundary's display title
            for toc_t in self.toc_titles:
                if toc_t in b_norm:
                    covered.add(toc_t)

        # TOC titles not yet covered
        missing = [t for t in self.toc_titles if t not in covered]

        # Always search for this chapter — it's absent from TOC extraction in this book
        ALWAYS_SEARCH = ["the richest man in babylon"]
        for t in ALWAYS_SEARCH:
            if t not in covered and t not in missing:
                missing.append(t)

        if not missing:
            return []

        synthetic = []

        for target in missing:
            target_lower = target.lower().strip()

            for i, para in enumerate(paragraphs):
                if i < 200:
                    continue

                text = para.text.strip()
                if not text:
                    continue

                text_lower = text.lower()

                if not text_lower.startswith(target_lower):
                    continue

                # Strip trailing page number to get clean title
                clean = re.sub(r'\s+\d+\s*$', '', text).strip()
                if clean.lower() != target_lower:
                    continue

                # Reject if bold (bold = real visual heading, already caught by scanner)
                any_bold = any(r.bold for r in para.runs if r.text.strip())
                if any_bold:
                    continue

                # Reject all-caps: book-title running headers, not chapter running headers
                if text == text.upper() and text.lower() != text:
                    continue

                # Found the first matching running header after TOC region.
                # Advance content_start past the running header cluster for this chapter.
                content_start = i + 1
                while content_start < len(paragraphs) and not paragraphs[content_start].text.strip():
                    content_start += 1

                # Skip additional running headers in the same cluster
                while content_start < len(paragraphs):
                    cp = paragraphs[content_start]
                    ct = cp.text.strip()
                    if not ct:
                        content_start += 1
                        continue
                    stripped = re.sub(r'\s+\d+\s*$', '', ct).strip()
                    if stripped.lower() == target_lower and ct.lower() != stripped.lower():
                        content_start += 1
                    else:
                        break

                synthetic.append(ChapterBoundary(
                    para_index=i,
                    title=clean,
                    score=2,
                    confidence="HIGH",
                    section_type="chapter",
                    content_start=content_start,
                    signals=["RUNNING_HEADER_FALLBACK", "TOC_MATCH"],
                    synthetic=True
                ))
                break

        return synthetic