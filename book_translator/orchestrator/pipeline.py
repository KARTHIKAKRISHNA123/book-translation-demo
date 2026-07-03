# book_translator/orchestrator/pipeline.py

import json
from pathlib import Path

from ..document.parser import DocxParser
from ..document.detector import DetectorCascade
from ..document.segmenter import Segmenter
from ..document.source_writer import SourceWriter


class Phase1Pipeline:
    """
    Orchestrates the full Phase 1 extraction.

    segmentation_map.json is the handoff document to Phase 2.
    It stores every segment's paragraph range, word count, and status=PENDING.
    Phase 2 reads this, translates each segment, sets status=TRANSLATED.

    detection_report.json is for YOU to review.
    Open it after running — check that chapter boundaries look correct.
    If a boundary has confidence=LOW or MEDIUM, verify it manually.
    """

    def __init__(self, output_root: Path = Path("output")):
        self.output_root = Path(output_root)
        self.parser = DocxParser()
        self.detector = DetectorCascade()
        self.segmenter = Segmenter()
        self.writer = SourceWriter()

    def run(self, docx_path: Path, target_lang: str = "Tamil") -> dict:
        docx_path = Path(docx_path)
        print(f"\n{'='*60}")
        print(f"PHASE 1: EXTRACTION PIPELINE")
        print(f"Input : {docx_path.name}")
        print(f"Target: {target_lang}")
        print(f"{'='*60}\n")

        # ── Step 1: Parse ──────────────────────────────────────────────
        print("[1/4] Parsing DOCX...")
        doc = self.parser.parse(docx_path)
        total_paras = len(doc.paragraphs)
        total_words = sum(p.word_count for p in doc.paragraphs)
        print(f"      Paragraphs : {total_paras}")
        print(f"      Total words: {total_words:,}")

        # ── Step 2: Detect ─────────────────────────────────────────────
        print("\n[2/4] Running chapter detection cascade...")
        boundaries = self.detector.detect(doc)

        for b in boundaries:
            print(f"      [{b.confidence:7s}] para[{b.para_index:04d}] "
                  f"'{b.title[:45]}' {b.signals}")

        needs_review = [b for b in boundaries if b.confidence in ("LOW", "MEDIUM")]
        if needs_review:
            print(f"\n      ⚠  {len(needs_review)} boundaries need human review")

        # ── Step 3: Segment ────────────────────────────────────────────
        print("\n[3/4] Segmenting document...")
        segments = self.segmenter.segment(doc, boundaries)
        print(f"      Segments: {len(segments)}")
        for seg in segments:
            print(f"      [{seg.section_type:14s}] {seg.output_filename} "
                  f"({seg.word_count:,} words)")
# ── Step 4: Write source files ─────────────────────────────────
        print("\n[4/4] Writing source .docx files...")

        # Output folder: output/{BookTitle}/Tamil/source/
        book_title = docx_path.stem.replace(" ", "_")
        target_dir = self.output_root / book_title / target_lang
        source_dir = target_dir / "source"
        source_dir.mkdir(parents=True, exist_ok=True)

        created_files = self.writer.write_segments(doc, segments, source_dir)
        for f in created_files:
            print(f"      ✓ {f.name}")

        # ── Write segmentation_map.json ────────────────────────────────
        # This is Phase 2's input. It reads this file to know:
        # - which segments exist
        # - what paragraphs they contain
        # - which ones are still PENDING translation
        seg_map = {
            "book": docx_path.name,
            "target_language": target_lang,
            "total_paragraphs": total_paras,
            "total_words": total_words,
            "has_chapters": any(
                b.confidence in ("HIGH", "CERTAIN") for b in boundaries
            ),
            "segments": [
                {
                    "unit_id": s.unit_id,
                    "title": s.title,
                    "section_type": s.section_type,
                    "para_start": s.para_start,
                    "para_end": s.para_end,
                    "word_count": s.word_count,
                    "confidence": s.confidence,
                    "output_filename": s.output_filename,
                    "signals": s.signals,
                    "source_path": str(source_dir / s.output_filename),
                    "status": "PENDING",   # Phase 2 changes this to TRANSLATED
                }
                for s in segments
            ],
        }

        seg_map_path = target_dir / "segmentation_map.json"
        with open(seg_map_path, "w", encoding="utf-8") as f:
            json.dump(seg_map, f, indent=2, ensure_ascii=False)
        print(f"\n      segmentation_map.json → {seg_map_path}")

        # ── Write detection_report.json ────────────────────────────────
        # Open this after running and verify chapter splits look right.
        # Anything with confidence LOW or MEDIUM → review manually.
        det_report = {
            "boundaries_detected": len(boundaries),
            "needs_review": len(needs_review),
            "boundaries": [
                {
                    "para_index": b.para_index,
                    "title": b.title,
                    "score": b.score,
                    "confidence": b.confidence,
                    "section_type": b.section_type,
                    "content_start": b.content_start,
                    "signals": b.signals,
                }
                for b in boundaries
            ],
        }

        det_path = target_dir / "detection_report.json"
        with open(det_path, "w", encoding="utf-8") as f:
            json.dump(det_report, f, indent=2, ensure_ascii=False)
        print(f"      detection_report.json → {det_path}")

        print(f"\n{'='*60}")
        print(f"PHASE 1 COMPLETE → {target_dir}")
        print(f"{'='*60}\n")

        return seg_map