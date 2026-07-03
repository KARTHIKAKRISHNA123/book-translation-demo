from pathlib import Path
from book_translator.orchestrator.pipeline import Phase1Pipeline

if __name__ == "__main__":
    pipeline = Phase1Pipeline(output_root=Path("output"))
    result = pipeline.run(
        docx_path=Path("The Richest Man In Babylon_Full_Book_Source.docx"),
        target_lang="Tamil",
    )