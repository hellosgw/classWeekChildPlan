"""
Robust PDF Parser Module — classWeekChildPlan
=============================================
Multi-engine PDF text extraction with table detection, scanned-page heuristics,
and Chinese text cleaning.  Falls back gracefully when preferred engines are
unavailable.

Exports:
    parse_pdf(file_bytes: bytes) -> dict
"""

from __future__ import annotations

import io
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Engine imports (optional – graceful degradation)
# ---------------------------------------------------------------------------
_pdfplumber_available = False
_pypdf2_available = False

try:
    import pdfplumber  # type: ignore[import-untyped]
    _pdfplumber_available = True
except ImportError:
    logger.warning("pdfplumber not installed – falling back to PyPDF2.")

try:
    from PyPDF2 import PdfReader
    _pypdf2_available = True
except ImportError:
    logger.warning("PyPDF2 not installed – PDF parsing will be unavailable.")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def parse_pdf(file_bytes: bytes) -> Dict[str, Any]:
    """Parse a PDF from raw bytes and return structured extraction results.

    Args:
        file_bytes: The raw PDF file content.

    Returns:
        A dictionary with keys:
            - text (str): Full cleaned text of the PDF.
            - pages (int): Total page count.
            - raw_text_per_page (List[str]): Raw text per page before cleaning.
            - has_tables (bool): Whether any tables were detected.
            - is_scanned (bool): Heuristic guess — True when very little text
              is extractable.
            - engine (str): Which backend was used ("pdfplumber" or "pypdf2").
            - warnings (List[str]): Non-fatal diagnostics.

    Raises:
        ValueError: If the file is empty, encrypted, or irrecoverably corrupt.
        RuntimeError: If no PDF engine is available.
    """
    # --- Pre-flight checks -------------------------------------------------
    if not file_bytes:
        raise ValueError("Empty file: received 0 bytes.")

    if len(file_bytes) < 5:
        raise ValueError("File too small to be a valid PDF.")

    # PDF magic bytes check
    if not file_bytes.startswith(b"%PDF"):
        raise ValueError("Not a valid PDF: missing %PDF header.")

    # Check for linearized PDF – still valid
    if not file_bytes.startswith(b"%PDF-"):
        logger.warning("Non-standard PDF header – proceeding anyway.")

    warnings: List[str] = []

    # --- Engine selection ---------------------------------------------------
    # Prefer pdfplumber for its layout awareness and table extraction.
    engine: str
    raw_per_page: List[str]
    page_count: int
    has_tables: bool
    is_scanned: bool

    if _pdfplumber_available:
        engine = "pdfplumber"
        raw_per_page, page_count, has_tables = _parse_with_pdfplumber(
            file_bytes, warnings
        )
    elif _pypdf2_available:
        engine = "pypdf2"
        raw_per_page, page_count, has_tables = _parse_with_pypdf2(
            file_bytes, warnings
        )
    else:
        raise RuntimeError(
            "No PDF parsing engine available. "
            "Install pdfplumber or PyPDF2."
        )

    # --- Scanned-page heuristic --------------------------------------------
    is_scanned = _detect_scanned(raw_per_page, page_count)
    if is_scanned:
        warnings.append(
            "This PDF appears to be a scanned image. "
            "Text extraction may be incomplete – consider OCR."
        )

    # --- Clean text --------------------------------------------------------
    cleaned_text = _clean_text(raw_per_page)

    return {
        "text": cleaned_text,
        "pages": page_count,
        "raw_text_per_page": raw_per_page,
        "has_tables": has_tables,
        "is_scanned": is_scanned,
        "engine": engine,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Engine-specific parsers
# ---------------------------------------------------------------------------
def _parse_with_pdfplumber(
    file_bytes: bytes, warnings: List[str]
) -> tuple[List[str], int, bool]:
    """Extract text page-by-page via pdfplumber, detecting tables."""
    pages_text: List[str] = []
    total_tables = 0

    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            for idx, page in enumerate(pdf.pages):
                # --- Per-page guard against massive pages ---
                text = ""
                try:
                    text = page.extract_text() or ""
                except Exception:
                    warnings.append(
                        f"Page {idx + 1}: text extraction failed – skipped."
                    )
                    text = ""

                pages_text.append(text)

                # --- Table detection ---
                try:
                    tables = page.extract_tables()
                    if tables:
                        total_tables += len(tables)
                        # Embed table content as ASCII-art-ish text
                        for tbl in tables:
                            for row in tbl:
                                if row:
                                    cleaned_row = [
                                        (cell or "").replace("\n", " ")
                                        for cell in row
                                    ]
                                    pages_text[-1] += "\n" + " | ".join(cleaned_row)
                except Exception:
                    pass  # table extraction is best-effort

        return pages_text, len(pdf.pages), total_tables > 0

    except Exception as exc:
        raise ValueError(f"pdfplumber failed to parse PDF: {exc}") from exc


def _parse_with_pypdf2(
    file_bytes: bytes, warnings: List[str]
) -> tuple[List[str], int, bool]:
    """Extract text page-by-page via PyPDF2 (legacy fallback)."""
    pages_text: List[str] = []

    try:
        reader = PdfReader(io.BytesIO(file_bytes))

        # Detect encryption early
        if reader.is_encrypted:
            raise ValueError(
                "PDF is encrypted. Please provide an unencrypted file."
            )

        for idx, page in enumerate(reader.pages):
            try:
                text = page.extract_text() or ""
            except Exception:
                warnings.append(
                    f"Page {idx + 1}: PyPDF2 extraction failed – skipped."
                )
                text = ""
            pages_text.append(text)

        # PyPDF2 cannot reliably detect tables
        return pages_text, len(reader.pages), False

    except ValueError:
        raise  # Re-raise our own encryption error
    except Exception as exc:
        raise ValueError(f"PyPDF2 failed to parse PDF: {exc}") from exc


# ---------------------------------------------------------------------------
# Scanned-page heuristic
# ---------------------------------------------------------------------------
_SCANNED_CHAR_THRESHOLD = 50  # Average chars per page below → likely scanned

def _detect_scanned(raw_per_page: List[str], page_count: int) -> bool:
    """Heuristic: if average chars per page is very low, assume scanned."""
    if page_count == 0:
        return True
    total_chars = sum(len(t) for t in raw_per_page)
    avg = total_chars / page_count
    return avg < _SCANNED_CHAR_THRESHOLD


# ---------------------------------------------------------------------------
# Text cleaning
# ---------------------------------------------------------------------------
# Chinese character range
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]")
# Multiple blank lines
_MULTI_NL_RE = re.compile(r"\n{3,}")
# Lines that are just spaces / short noise
_STRIP_LINE_RE = re.compile(r"^\s*$")
# Fix broken Chinese lines: a CJK char at end of line + CJK at start of next
_BROKEN_CJK_RE = re.compile(r"([\u4e00-\u9fff\u3400-\u4dbf])\s*\n\s*([\u4e00-\u9fff\u3400-\u4dbf])")

def _clean_text(raw_per_page: List[str]) -> str:
    """Build a single cleaned text block from per-page raw strings.

    Steps:
        1. Merge broken CJK lines (Chinese sentences split across lines).
        2. Collapse 3+ consecutive newlines to exactly 2 (paragraph break).
        3. Strip leading/trailing whitespace.
    """
    joined = "\n".join(raw_per_page)

    # Merge broken Chinese lines: "镜\n子" → "镜子"
    cleaned = _BROKEN_CJK_RE.sub(r"\1\2", joined)

    # Collapse excessive blank lines
    cleaned = _MULTI_NL_RE.sub("\n\n", cleaned)

    # Normalize trailing/leading whitespace
    cleaned = cleaned.strip()

    return cleaned
