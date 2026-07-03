import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from .parser import ParsedDocument, Paragraph
from .detector import ChapterBoundary

# Chapters larger than this get split into chunk files
# 2500 words ≈ 45 seconds of Claude translation time (our performance target)
MAX_CHUNK_WORDS = 2500


@dataclass
class Segment:
    """
    One translatable unit = one output .docx file.
    """
    unit_id: str            # "chapter01", "preamble-001", "appendix-001"
    title: str
    section_type: str       # "chapter" | "preamble" | "appendix" | "narrative_unit"
    para_start: int         # inclusive
    para_end: int           # exclusive
    word_count: int
    confidence: str         # inherited from ChapterBoundary
    output_filename: str    # e.g. "chapter01-The_Man_Who_Desired_Gold.docx"
    signals: list[str] = field(default_factory=list)

    @property
    def paragraphs_range(self) -> range:
        return range(self.para_start, self.para_end)


def _slugify(text: str, max_len: int = 40) -> str:
    """
    Converts a title to a safe filename component.
    """
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s\-]+", "_", text.strip())
    return text[:max_len]


class Segmenter:
    """
    Decision tree:
    ┌─ Are there ≥1 HIGH/CERTAIN confidence boundaries?
    │  YES → chapter path  (named chapter01, chapter02 ...)
    │  NO  → narrative unit path (named unit_001, unit_002 ...)
    └─
    """

    def segment(self, doc: ParsedDocument, boundaries: list[ChapterBoundary]) -> list[Segment]:
        total_paras = len(doc.paragraphs)

        high_confidence = [
            b for b in boundaries if b.confidence in ("HIGH", "CERTAIN")
        ]

        if high_confidence:
            return self._chapter_path(doc, boundaries, total_paras)
        else:
            return self._narrative_unit_path(doc, total_paras)

    def _chapter_path(self, doc, boundaries, total_paras) -> list[Segment]:
        paras = doc.paragraphs
        segments: list[Segment] = []
        boundaries = sorted(boundaries, key=lambda b: b.para_index)

        # Front Matter
        if boundaries and boundaries[0].para_index > 0:
            first_boundary_para = boundaries[0].para_index
            pre_words = sum(p.word_count for p in paras[:first_boundary_para])
            if pre_words > 20:
                segments.append(Segment(
                    unit_id="preamble-000",
                    title="Front Matter",
                    section_type="preamble",
                    para_start=0,
                    para_end=first_boundary_para,
                    word_count=pre_words,
                    confidence="CERTAIN",
                    output_filename="preamble-000-Front_Matter.docx",
                ))

        chapter_num = 0
        preamble_num = 0
        appendix_num = 0

        for i, boundary in enumerate(boundaries):
            if i + 1 < len(boundaries):
                para_end = boundaries[i + 1].para_index
            else:
                para_end = total_paras

            content_start = boundary.content_start
            word_count = sum(p.word_count for p in paras[content_start:para_end])

            if boundary.section_type == "preamble":
                preamble_num += 1
                slug = _slugify(boundary.title)
                unit_id = f"preamble-{preamble_num:03d}"
                segments.append(Segment(
                    unit_id=unit_id,
                    title=boundary.title,
                    section_type="preamble",
                    para_start=boundary.para_index,
                    para_end=para_end,
                    word_count=word_count,
                    confidence=boundary.confidence,
                    output_filename=f"{unit_id}-{slug}.docx",
                    signals=boundary.signals,
                ))

            elif boundary.section_type == "appendix":
                appendix_num += 1
                slug = _slugify(boundary.title)
                unit_id = f"appendix-{appendix_num:03d}"
                segments.append(Segment(
                    unit_id=unit_id,
                    title=boundary.title,
                    section_type="appendix",
                    para_start=boundary.para_index,
                    para_end=para_end,
                    word_count=word_count,
                    confidence=boundary.confidence,
                    output_filename=f"{unit_id}-{slug}.docx",
                    signals=boundary.signals,
                ))

            else:
                # Regular chapter
                chapter_num += 1
                slug = _slugify(boundary.title)

                if word_count <= MAX_CHUNK_WORDS:
                    unit_id = f"chapter{chapter_num:02d}"
                    segments.append(Segment(
                        unit_id=unit_id,
                        title=boundary.title,
                        section_type="chapter",
                        para_start=boundary.para_index,
                        para_end=para_end,
                        word_count=word_count,
                        confidence=boundary.confidence,
                        output_filename=f"{unit_id}-{slug}.docx",
                        signals=boundary.signals,
                    ))
                else:
                    chunks = self._split_chapter(
                        paras, content_start, para_end,
                        chapter_num, slug, boundary
                    )
                    segments.extend(chunks)

        return segments
    
    def _split_chapter(self, paras, content_start, content_end, chapter_num, slug, boundary) -> list[Segment]:
        """
        When a chapter is > 2500 words, we split it.
        """
        chunks: list[Segment] = []
        chunk_num = 0
        current_start = content_start
        current_words = 0

        for i in range(content_start, content_end):
            current_words += paras[i].word_count

            if current_words >= MAX_CHUNK_WORDS:
                chunk_num += 1
                unit_id = f"chapter{chapter_num:02d}_chunk{chunk_num:02d}"
                chunks.append(Segment(
                    unit_id=unit_id,
                    title=f"{boundary.title} (Part {chunk_num})",
                    section_type="chapter",
                    para_start=current_start,
                    para_end=i + 1,
                    word_count=current_words,
                    confidence=boundary.confidence,
                    output_filename=f"{unit_id}-{slug}.docx",
                    signals=boundary.signals,
                ))
                current_start = i + 1
                current_words = 0

        if current_start < content_end:
            chunk_num += 1
            remaining = sum(p.word_count for p in paras[current_start:content_end])
            unit_id = f"chapter{chapter_num:02d}_chunk{chunk_num:02d}"
            chunks.append(Segment(
                unit_id=unit_id,
                title=f"{boundary.title} (Part {chunk_num})",
                section_type="chapter",
                para_start=current_start,
                para_end=content_end,
                word_count=remaining,
                confidence=boundary.confidence,
                output_filename=f"{unit_id}-{slug}.docx",
                signals=boundary.signals,
            ))

        return chunks

    def _narrative_unit_path(self, doc, total_paras) -> list[Segment]:
        """
        Fallback when NO high-confidence chapters were detected.
        """
        paras = doc.paragraphs
        segments: list[Segment] = []
        unit_num = 0
        current_start = 0
        current_words = 0
        first_phrase = ""

        for i in range(total_paras):
            text = paras[i].text.strip()
            if not first_phrase and text:
                first_phrase = " ".join(text.split()[:5])
            current_words += paras[i].word_count

            if current_words >= 1500:
                unit_num += 1
                slug = _slugify(first_phrase)
                unit_id = f"unit_{unit_num:03d}"
                segments.append(Segment(
                    unit_id=unit_id, title=first_phrase,
                    section_type="narrative_unit",
                    para_start=current_start, para_end=i + 1,
                    word_count=current_words, confidence="LOW",
                    output_filename=f"{unit_id}-{slug}.docx",
                ))
                current_start = i + 1
                current_words = 0
                first_phrase = ""

        if current_start < total_paras:
            unit_num += 1
            if not first_phrase:
                t = paras[current_start].text.strip() if current_start < total_paras else "end"
                first_phrase = " ".join(t.split()[:5])
            remaining = sum(p.word_count for p in paras[current_start:])
            slug = _slugify(first_phrase)
            unit_id = f"unit_{unit_num:03d}"
            segments.append(Segment(
                unit_id=unit_id, title=first_phrase,
                section_type="narrative_unit",
                para_start=current_start, para_end=total_paras,
                word_count=remaining, confidence="LOW",
                output_filename=f"{unit_id}-{slug}.docx",
            ))

        return segments