import zipfile
import copy
from pathlib import Path
import lxml.etree as etree

from .parser import ParsedDocument
from .segmenter import Segment

NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

def _tag(name: str) -> str:
    return f"{{{NS_W}}}{name}"


class SourceWriter:
    """
    How it works:
    1. Read the original .docx ZIP entirely into memory (once)
    2. Parse document.xml → get all <w:p> elements as a list
    3. For each Segment:
       a. Slice all_p_elements[para_start:para_end]
       b. Build a new document.xml containing only those paragraphs
       c. Write a new ZIP identical to the original EXCEPT document.xml
          is replaced with our sliced version

    Why this preserves formatting perfectly:
    - styles.xml stays identical → all style definitions intact
    - fonts, images, relationships all unchanged
    - We copy <w:p> elements with deep copy → all <w:rPr> formatting preserved
    - We keep the original <w:sectPr> (page size/margins) at end of body
    """

    def write_segments(self, doc: ParsedDocument, segments: list[Segment],
                       output_dir: Path) -> list[Path]:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        # Read entire original ZIP into memory — do this ONCE
        with zipfile.ZipFile(doc.source_path, "r") as zin:
            zip_contents = {name: zin.read(name) for name in zin.namelist()}

        # Parse document.xml and collect all <w:p> elements in order
        doc_xml = zip_contents["word/document.xml"]
        root = etree.fromstring(doc_xml)
        body = root.find(f".//{_tag('body')}")

        all_p_elements = []
        for child in body:
            local = etree.QName(child.tag).localname
            if local == "p":
                all_p_elements.append(child)
            elif local == "tbl":
                # Flatten table cell paragraphs into the same list
                # This keeps para indices aligned with the parser's output
                for cell_p in child.iter(_tag("p")):
                    all_p_elements.append(cell_p)

        # Write one .docx per segment
        created: list[Path] = []
        for segment in segments:
            out_path = output_dir / segment.output_filename
            self._write_segment(segment, all_p_elements, zip_contents, out_path)
            created.append(out_path)

        return created

    def _write_segment(self, segment: Segment, all_p_elements: list,
                       zip_contents: dict, out_path: Path) -> None:
        # Slice: take only the paragraphs belonging to this segment
        selected_p = all_p_elements[segment.para_start:segment.para_end]

        # Build new document.xml with just these paragraphs
        new_doc_xml = self._build_document_xml(selected_p, zip_contents)

        # Write new ZIP: everything from original, except document.xml is ours
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zout:
            for name, data in zip_contents.items():
                if name == "word/document.xml":
                    zout.writestr(name, new_doc_xml)
                else:
                    zout.writestr(name, data)   # unchanged: styles, fonts, images

    def _build_document_xml(self, p_elements: list, zip_contents: dict) -> bytes:
        """
        Constructs a valid document.xml with ONLY the selected paragraphs.

        Structure of output:
        <w:document>          ← copied from original (all namespaces intact)
          <w:body>
            <w:p>...</w:p>    ← our selected paragraphs (deep copied)
            <w:p>...</w:p>
            ...
            <w:sectPr>...</w:sectPr>   ← page settings: MUST be last child of body
          </w:body>
        </w:document>
        """
        # Start from deep copy of original root (preserves namespace declarations)
        original = etree.fromstring(zip_contents["word/document.xml"])
        new_root = copy.deepcopy(original)
        new_body = new_root.find(f".//{_tag('body')}")

        # Grab sectPr BEFORE clearing (it's usually the last element in body)
        sectPr = new_body.find(_tag("sectPr"))

        # Clear all existing body children
        for child in list(new_body):
            new_body.remove(child)

        # Add our selected paragraphs
        for p_el in p_elements:
            new_body.append(copy.deepcopy(p_el))

        # sectPr must be the last child of <w:body> for a valid DOCX
        if sectPr is not None:
            new_body.append(copy.deepcopy(sectPr))

        return etree.tostring(
            new_root,
            xml_declaration=True,
            encoding="UTF-8",
            standalone=True
        )