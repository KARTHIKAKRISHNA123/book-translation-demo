"""
app.py — Split a DOCX book into one file per chapter. 100% formatting preserved.

HOW IT WORKS
  A DOCX is a ZIP. This script never parses or re-serializes the XML.
  It finds each chapter's paragraph byte positions in the raw document.xml
  and slices the bytes directly — so drawings, images, colors, fonts,
  backgrounds, and all formatting are preserved exactly as-is.

USAGE
  python app.py "book.docx"
  python app.py "book.docx" --output "D:/chapters"
  python app.py "book.docx" --output "D:/chapters" --skip-preamble

INSTALL
  pip install python-docx
"""

import argparse
import os
import re
import sys
import zipfile

from docx import Document


# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 — DETECT CHAPTER HEADINGS
# ─────────────────────────────────────────────────────────────────────────────

def _is_heading(para):
    """
    Detect chapter title headings.

    All chapter headings in this book are:
      - bold
      - font size exactly 254000 EMU (20pt)
      - Title Case  (NOT all-caps)

    Most are also italic, but "Meet the Goddess of Good Luck" is bold-only,
    so we do NOT require italic. Instead we reject all-caps text (which covers
    decorative repeats like "THE FIVE LAWS OF GOLD" and back-matter ad pages).
    """
    runs = [r for r in para.runs if r.text.strip()]
    if not runs:
        return False
    text = para.text.strip()
    if not text:
        return False
    # Must be bold
    if not any(r.bold for r in runs):
        return False
    # Must be exactly 20pt (254000 EMU) — rules out cover text, ad pages
    if (runs[0].font.size or 0) != 254000:
        return False
    # Reject ALL-CAPS paragraphs (decorative repeats, back-matter headings)
    if text == text.upper():
        return False
    return True

_SKIP = {"contents", "sound financial advice for everyone"}

def find_chapters(paragraphs):
    """
    Returns list of {"title": str, "para_start": int}.
    Two-line titles separated by a blank paragraph are merged.
    """
    chapters = []
    i = 0
    n = len(paragraphs)
    while i < n:
        if _is_heading(paragraphs[i]):
            parts = []
            while i < n:
                p = paragraphs[i]
                if _is_heading(p):
                    t = p.text.strip()
                    if t:
                        parts.append(t)
                    i += 1
                elif not p.text.strip():
                    ahead = any(
                        _is_heading(paragraphs[j])
                        for j in range(i + 1, min(i + 4, n))
                        if paragraphs[j].text.strip()
                    )
                    if ahead:
                        i += 1
                    else:
                        break
                else:
                    break
            title = " ".join(parts)
            if title.lower().strip() not in _SKIP:
                # para_start = index of first line of this heading in paragraphs[]
                # We need to subtract parts we just consumed
                # Actually i now points AFTER the heading block.
                # The heading_start was before we entered the while loop.
                # Re-compute: heading_start = i - number_of_heading_paras_consumed
                # Simpler: track it explicitly
                chapters.append({"title": title, "_end_i": i, "_parts": len(parts)})
        else:
            i += 1

    # Fix: we didn't track start index well above. Redo cleanly:
    return _find_chapters_clean(paragraphs)


def _find_chapters_clean(paragraphs):
    chapters = []
    i = 0
    n = len(paragraphs)
    while i < n:
        if _is_heading(paragraphs[i]):
            heading_start = i
            parts = []
            while i < n:
                p = paragraphs[i]
                if _is_heading(p):
                    t = p.text.strip()
                    if t:
                        parts.append(t)
                    i += 1
                elif not p.text.strip():
                    ahead = any(
                        _is_heading(paragraphs[j])
                        for j in range(i + 1, min(i + 4, n))
                        if paragraphs[j].text.strip()
                    )
                    if ahead:
                        i += 1
                    else:
                        break
                else:
                    break
            title = " ".join(parts)
            if title.lower().strip() not in _SKIP:
                chapters.append({"title": title, "para_start": heading_start})
        else:
            i += 1
    return chapters


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 — FIND BODY-LEVEL PARAGRAPH BYTE POSITIONS IN RAW document.xml
# ─────────────────────────────────────────────────────────────────────────────

def find_body_para_positions(doc_xml_bytes):
    """
    Walk the raw XML bytes and record the byte position of every <w:p>
    that is a DIRECT CHILD of <w:body> (not inside a table or other element).

    Returns:
        para_positions : list of int  — byte offset of each body-level <w:p>
        body_start     : int          — byte offset of <w:body>
        body_close     : int          — byte offset of </w:body>
    """
    body_start = doc_xml_bytes.find(b'<w:body>')
    body_close = doc_xml_bytes.rfind(b'</w:body>')

    body = doc_xml_bytes[body_start:body_close]
    depth = 0   # table nesting depth — we only want depth==0 paragraphs
    para_positions = []
    i = 0

    while i < len(body):
        # Enter table
        if body[i:i+7] in (b'<w:tbl ', b'<w:tbl>'):
            depth += 1
            i += 7
            continue
        # Exit table
        if body[i:i+8] == b'</w:tbl>':
            depth -= 1
            i += 8
            continue
        # Body-level paragraph (not inside a table)
        if depth == 0 and body[i:i+5] in (b'<w:p >', b'<w:p >') or \
           depth == 0 and (body[i:i+5] == b'<w:p ' or body[i:i+4] == b'<w:p>'):
            para_positions.append(body_start + i)
            i += 4
            continue
        i += 1

    return para_positions, body_start, body_close


def slice_chapter_xml(doc_xml_bytes, para_positions, body_start, body_close,
                      p_start, p_end):
    """
    Return a valid document.xml (bytes) containing only paragraphs [p_start:p_end].

    Structure:
      <everything up to first body paragraph>   ← preserves all namespace decls
      <w:p>...</w:p> × chapter paragraphs       ← raw bytes, untouched
      <w:sectPr>...</w:sectPr>                  ← final doc sectPr for page settings
      </w:body></w:document>
    """
    total = len(para_positions)

    # Byte where our chapter content starts
    content_start = para_positions[p_start]

    # Byte where our chapter content ends
    content_end = para_positions[p_end] if p_end < total else body_close

    # Document header = everything up to the first body paragraph
    # (includes XML declaration, <w:document>, all namespace decls, <w:body>)
    header = doc_xml_bytes[:para_positions[0]]

    # Chapter paragraph bytes — taken byte-for-byte from source
    chapter_content = doc_xml_bytes[content_start:content_end]

    # Final <w:sectPr> from the original body (page size, margins, orientation)
    # It sits between the last </w:p> and </w:body>
    tail = doc_xml_bytes[para_positions[-1]:body_close]   # last para to body close
    secpr_match = re.search(rb'<w:sectPr\b.*?</w:sectPr>', tail, re.DOTALL)
    final_secpr = secpr_match.group() if secpr_match else b''

    return header + chapter_content + final_secpr + b'</w:body></w:document>'


# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 — WRITE CHAPTER DOCX
# ─────────────────────────────────────────────────────────────────────────────

def write_chapter_docx(src_path, out_path, new_doc_xml_bytes):
    """
    Exact copy of the source ZIP with only word/document.xml replaced.
    All fonts, images, styles, theme, relationships = identical to source.
    """
    with zipfile.ZipFile(src_path, 'r') as src_zip:
        with zipfile.ZipFile(out_path, 'w', compression=zipfile.ZIP_DEFLATED) as out_zip:
            for item in src_zip.infolist():
                if item.filename == 'word/document.xml':
                    out_zip.writestr(item, new_doc_xml_bytes)
                else:
                    out_zip.writestr(item, src_zip.read(item.filename))


# ─────────────────────────────────────────────────────────────────────────────
# STEP 4 — HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def safe_name(text):
    text = re.sub(r'[\\/:*?"<>|]', '', text)
    return re.sub(r'\s+', '_', text.strip())

def book_folder(docx_path):
    stem = os.path.splitext(os.path.basename(docx_path))[0]
    return re.sub(r'\s+', ' ', re.sub(r'[_\-]+', ' ', stem).strip())


# ─────────────────────────────────────────────────────────────────────────────
# STEP 5 — MAIN PIPELINE
# ─────────────────────────────────────────────────────────────────────────────

def split_book(docx_path, output_dir, skip_preamble):
    print(f"\n📖  Loading: {docx_path}")

    doc        = Document(docx_path)      # read-only: chapter detection
    paragraphs = doc.paragraphs
    print(f"    Paragraphs       : {len(paragraphs)}")

    # Raw bytes
    with zipfile.ZipFile(docx_path, 'r') as z:
        doc_xml_bytes = z.read('word/document.xml')

    # Body-level paragraph byte positions (skips table-nested <w:p>)
    para_positions, body_start, body_close = find_body_para_positions(doc_xml_bytes)
    print(f"    Body <w:p> found : {len(para_positions)}")

    if len(para_positions) != len(paragraphs):
        print(f"    ⚠️  Count mismatch ({len(para_positions)} vs {len(paragraphs)}). "
              f"Using raw XML positions.")

    chapters = _find_chapters_clean(paragraphs)
    print(f"    Chapters found   : {len(chapters)}")

    if not chapters:
        print("\n⚠️  No chapter headings detected.")
        sys.exit(1)

    folder = os.path.join(output_dir, book_folder(docx_path))
    os.makedirs(folder, exist_ok=True)
    print(f"\n📁  Output: {folder}\n")

    total = len(para_positions)
    sections = []

    if not skip_preamble:
        pre_end = chapters[0]["para_start"]
        if pre_end > 0:
            sections.append((0, pre_end, "Preamble", "00_Preamble.docx"))

    for idx, ch in enumerate(chapters, start=1):
        p_start = ch["para_start"]
        p_end   = chapters[idx]["para_start"] if idx < len(chapters) else total
        fname   = f"{idx:02d}_{safe_name(ch['title'])}.docx"
        sections.append((p_start, p_end, ch["title"], fname))

    for (p_start, p_end, title, fname) in sections:
        out_path = os.path.join(folder, fname)

        chapter_xml = slice_chapter_xml(
            doc_xml_bytes, para_positions, body_start, body_close,
            p_start, min(p_end, total)
        )

        write_chapter_docx(docx_path, out_path, chapter_xml)

        wc = sum(
            len(paragraphs[i].text.split())
            for i in range(p_start, min(p_end, len(paragraphs)))
            if paragraphs[i].text.strip()
        )
        print(f"  ✓  {fname}  (~{wc} words)")

    print(f"\n✅  Done — {len(sections)} files in:\n    {folder}\n")


# ─────────────────────────────────────────────────────────────────────────────
# STEP 6 — CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="app.py",
        description="Split a DOCX book into one file per chapter. 100% formatting preserved.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
EXAMPLES
  python app.py book.docx
  python app.py book.docx --output D:/chapters
  python app.py book.docx --output D:/chapters --skip-preamble
        """
    )
    parser.add_argument("docx", help="Path to the source DOCX file")
    parser.add_argument("--output", "-o", default=".", metavar="DIR",
        help="Where to create the output folder (default: current directory)")
    parser.add_argument("--skip-preamble", action="store_true",
        help="Don't write 00_Preamble.docx")

    args = parser.parse_args()
    if not os.path.isfile(args.docx):
        print(f"\n❌  File not found: {args.docx}\n")
        sys.exit(1)

    split_book(args.docx, args.output, args.skip_preamble)

if __name__ == "__main__":
    main()