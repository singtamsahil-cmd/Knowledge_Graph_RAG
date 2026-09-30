"""PDF text extraction, page by page, with provenance preserved."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class PageSegment:
    document: str
    page_number: int  # 1-indexed
    text: str
    char_count: int = 0
    is_empty: bool = False
    error: str | None = None

    def __post_init__(self):
        self.char_count = len((self.text or "").strip())
        self.is_empty = self.char_count == 0


@dataclass
class PDFResult:
    path: str
    segments: list[PageSegment] = field(default_factory=list)
    num_pages: int = 0
    empty_pages: int = 0
    error: str | None = None


def extract_pdf_pages(pdf_path: str | Path, ocr_threshold: int = 50) -> PDFResult:
    """Extract text page by page using pypdf. Never silently discards pages."""
    from pypdf import PdfReader

    pdf_path = Path(pdf_path)
    result = PDFResult(path=str(pdf_path))
    try:
        reader = PdfReader(str(pdf_path))
        result.num_pages = len(reader.pages)
        for i, page in enumerate(reader.pages):
            page_no = i + 1
            try:
                text = page.extract_text() or ""
            except Exception as exc:  # keep page, record error
                log.warning("Page %d of %s extraction error: %s", page_no, pdf_path.name, exc)
                result.segments.append(
                    PageSegment(document=pdf_path.name, page_number=page_no, text="", error=str(exc))
                )
                continue
            seg = PageSegment(document=pdf_path.name, page_number=page_no, text=text)
            result.segments.append(seg)
            if seg.is_empty:
                log.warning("Page %d of %s has no extractable text.", page_no, pdf_path.name)
    except Exception as exc:
        result.error = str(exc)
        log.error("Failed to read PDF %s: %s", pdf_path, exc)
        return result

    result.empty_pages = sum(1 for s in result.segments if s.is_empty)
    total_chars = sum(s.char_count for s in result.segments)
    if result.num_pages > 0 and total_chars < ocr_threshold:
        log.warning(
            "PDF %s has only %d extractable chars across %d pages. OCR may be required "
            "(scanned document?).",
            pdf_path.name, total_chars, result.num_pages,
        )
    return result


def collect_pdfs(pdf: str | Path | None, input_dir: str | Path | None) -> list[Path]:
    paths: list[Path] = []
    if pdf:
        p = Path(pdf)
        if not p.exists():
            raise FileNotFoundError(f"PDF not found: {pdf}")
        paths.append(p)
    if input_dir:
        d = Path(input_dir)
        if not d.is_dir():
            raise NotADirectoryError(f"Input directory not found: {input_dir}")
        paths.extend(sorted(d.glob("*.pdf")) + sorted(d.glob("*.PDF")))
    # de-duplicate while preserving order
    seen, unique = set(), []
    for p in paths:
        key = str(p.resolve())
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique


def process_pdfs(paths: list[Path], ocr_threshold: int = 50) -> tuple[list[PageSegment], list[PDFResult]]:
    segments: list[PageSegment] = []
    results: list[PDFResult] = []
    for p in paths:
        r = extract_pdf_pages(p, ocr_threshold=ocr_threshold)
        results.append(r)
        segments.extend(r.segments)
    log.info("Processed %d PDFs, %d pages (%d empty).", len(results),
             sum(r.num_pages for r in results), sum(r.empty_pages for r in results))
    return segments, results
