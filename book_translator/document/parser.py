import zipfile          # .docx is literally a ZIP file — we open it ourselves
import re               # regex, used later for text cleaning
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional
import lxml.etree as etree   # fast XML parser — we read OOXML with this

# A .docx file has XML namespaces. Every tag is prefixed.
NS = {
    "w":  "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "r":  "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
}

def _tag(prefix: str, name: str) -> str:
    # Converts _tag("w", "p") → "{http://schemas.openxml...}p"
    return f"{{{NS[prefix]}}}{name}"


@dataclass
class Run:
    """
    A <w:r> run is the smallest unit of text in DOCX.
    """
    text: str
    bold: bool = False
    italic: bool = False
    underline: bool = False
    style_id: Optional[str] = None    # e.g. "Strong", "Emphasis" — from rStyle


@dataclass
class Paragraph:
    """
    A <w:p> paragraph — one logical block of text.
    """
    index: int                          # position in document (0, 1, 2, ...)
    runs: list[Run] = field(default_factory=list)
    style_name: str = "Normal"          # paragraph-level style: "Heading 1", "Normal", etc.
    has_page_break: bool = False        # true if Word inserted a manual page break here
    has_section_break: bool = False     # true if <w:sectPr> is inside this paragraph
    outline_level: Optional[int] = None # 0 = Heading1, 1 = Heading2, None = body text
    is_list_item: bool = False          # true if this is a bullet/numbered list item

    @property
    def text(self) -> str:
        return "".join(r.text for r in self.runs)

    @property
    def all_bold(self) -> bool:
        active = [r for r in self.runs if r.text.strip()]
        return bool(active) and all(r.bold for r in active)

    @property
    def all_italic(self) -> bool:
        active = [r for r in self.runs if r.text.strip()]
        return bool(active) and all(r.italic for r in active)

    @property
    def word_count(self) -> int:
        return len(self.text.split())

    @property
    def is_all_caps(self) -> bool:
        t = self.text.strip()
        return bool(t) and t == t.upper() and any(c.isalpha() for c in t)


@dataclass
class ParsedDocument:
    """
    The full output of the parser.
    """
    paragraphs: list[Paragraph]
    style_map: dict[str, str]   # styleId → styleName (e.g. "Heading1" → "Heading 1")
    source_path: Path           # original .docx path (needed by SourceWriter)
    raw_xml: bytes              # raw document.xml bytes (needed by SourceWriter for diff)


class DocxParser:
    """
    The main parser class.
    Call: doc = DocxParser().parse(Path("my_book.docx"))
    """

    def parse(self, docx_path: Path) -> ParsedDocument:
        docx_path = Path(docx_path)
        if not docx_path.exists():
            raise FileNotFoundError(f"DOCX not found: {docx_path}")

        # Step 1: Open the ZIP, extract the two XML files we need
        with zipfile.ZipFile(docx_path, "r") as z:
            raw_xml = z.read("word/document.xml")
            try:
                styles_xml = z.read("word/styles.xml")
            except KeyError:
                styles_xml = b""

        # Step 2: Build styleId → styleName lookup
        style_map = self._parse_styles(styles_xml)

        # Step 3: Parse all paragraphs from document.xml
        paragraphs = self._parse_document(raw_xml, style_map)

        return ParsedDocument(
            paragraphs=paragraphs,
            style_map=style_map,
            source_path=docx_path,
            raw_xml=raw_xml,
        )

    def _parse_styles(self, styles_xml: bytes) -> dict[str, str]:
        if not styles_xml:
            return {}

        root = etree.fromstring(styles_xml)
        style_map: dict[str, str] = {}

        for style_el in root.findall(f".//{_tag('w', 'style')}"):
            sid = style_el.get(_tag("w", "styleId"), "")
            name_el = style_el.find(_tag("w", "name"))
            if name_el is not None:
                sname = name_el.get(_tag("w", "val"), sid)
                style_map[sid] = sname

        return style_map

    def _parse_document(self, raw_xml: bytes, style_map: dict[str, str]) -> list[Paragraph]:
        root = etree.fromstring(raw_xml)
        body = root.find(f".//{_tag('w', 'body')}")
        if body is None:
            return []

        paragraphs: list[Paragraph] = []
        para_index = 0

        for child in body:
            local = etree.QName(child.tag).localname

            if local == "p":
                para = self._parse_paragraph(child, para_index, style_map)
                paragraphs.append(para)
                para_index += 1

            elif local == "tbl":
                for cell_p in child.iter(_tag("w", "p")):
                    para = self._parse_paragraph(cell_p, para_index, style_map)
                    paragraphs.append(para)
                    para_index += 1

        return paragraphs
    
    def _parse_paragraph(self, p_el, index: int, style_map: dict[str, str]) -> Paragraph:
        pPr = p_el.find(_tag("w", "pPr"))
        style_name = "Normal"
        outline_level = None
        has_section_break = False
        is_list_item = False

        if pPr is not None:
            pStyle = pPr.find(_tag("w", "pStyle"))
            if pStyle is not None:
                sid = pStyle.get(_tag("w", "val"), "")
                style_name = style_map.get(sid, sid)

            outlineLvl = pPr.find(_tag("w", "outlineLvl"))
            if outlineLvl is not None:
                try:
                    outline_level = int(outlineLvl.get(_tag("w", "val"), "9"))
                except ValueError:
                    pass

            if pPr.find(_tag("w", "sectPr")) is not None:
                has_section_break = True

            if pPr.find(_tag("w", "numPr")) is not None:
                is_list_item = True

        has_page_break = False
        for br in p_el.iter(_tag("w", "br")):
            btype = br.get(_tag("w", "type"), "")
            if btype in ("page", "column"):
                has_page_break = True
                break

        runs = self._parse_runs(p_el)

        return Paragraph(
            index=index,
            runs=runs,
            style_name=style_name,
            has_page_break=has_page_break,
            has_section_break=has_section_break,
            outline_level=outline_level,
            is_list_item=is_list_item,
        )

    def _parse_runs(self, p_el) -> list[Run]:
        runs: list[Run] = []

        for r_el in p_el.findall(_tag("w", "r")):
            rPr = r_el.find(_tag("w", "rPr"))
            bold = False
            italic = False
            underline = False
            style_id = None

            if rPr is not None:
                bold      = rPr.find(_tag("w", "b")) is not None
                italic    = rPr.find(_tag("w", "i")) is not None
                underline = rPr.find(_tag("w", "u")) is not None
                rStyle    = rPr.find(_tag("w", "rStyle"))
                if rStyle is not None:
                    style_id = rStyle.get(_tag("w", "val"))

            text_parts: list[str] = []
            for t_el in r_el.findall(_tag("w", "t")):
                text_parts.append(t_el.text or "")
            text = "".join(text_parts)

            if text:
                runs.append(Run(
                    text=text,
                    bold=bold,
                    italic=italic,
                    underline=underline,
                    style_id=style_id,
                ))

        return runs