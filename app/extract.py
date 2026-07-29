"""
Extracts plain text from uploaded knowledge-base documents.

Files are identified by sniffing their actual bytes (via `filetype`),
never by trusting the client-supplied filename extension or Content-Type
header alone. Anything that doesn't match a known, safe document type
is rejected outright.
"""
import io

import filetype
from pypdf import PdfReader
from docx import Document

from app.config import settings

ALLOWED_KINDS = {"pdf", "docx"}  # sniffable binary kinds we accept
ALLOWED_TEXT_EXTENSIONS = {".txt", ".md"}


class ExtractionError(Exception):
    pass


def sniff_kind(raw: bytes) -> str | None:
    kind = filetype.guess(raw)
    if kind is None:
        return None
    return kind.extension


def extract_text(raw: bytes, filename: str) -> tuple[str, bool]:
    """
    Returns (text, truncated). Raises ExtractionError on anything that
    isn't a genuinely-recognized, safe document type.
    """
    lower = filename.lower()
    kind = sniff_kind(raw)

    if kind == "pdf" or lower.endswith(".pdf"):
        if kind not in (None, "pdf"):
            raise ExtractionError("File content does not match a PDF.")
        text = _extract_pdf(raw)
    elif kind == "docx" or lower.endswith(".docx"):
        if kind not in (None, "docx", "zip"):  # docx sniffs as a zip container
            raise ExtractionError("File content does not match a Word document.")
        text = _extract_docx(raw)
    elif any(lower.endswith(ext) for ext in ALLOWED_TEXT_EXTENSIONS):
        # Plain text — reject if it actually sniffs as a binary format
        # (i.e. someone renamed a binary file to .txt).
        if kind is not None:
            raise ExtractionError("File content does not match a plain text file.")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace")
    else:
        raise ExtractionError(
            "Unsupported file type. Please upload .txt, .md, .pdf, or .docx."
        )

    truncated = False
    if len(text) > settings.KB_MAX_CHARS_PER_DOC:
        text = text[: settings.KB_MAX_CHARS_PER_DOC]
        truncated = True
    return text.strip(), truncated


def _extract_pdf(raw: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(raw))
        pages = [page.extract_text() or "" for page in reader.pages]
        return "\n".join(pages)
    except Exception as e:
        raise ExtractionError(f"Could not read PDF: {e}")


def _extract_docx(raw: bytes) -> str:
    try:
        doc = Document(io.BytesIO(raw))
        return "\n".join(p.text for p in doc.paragraphs)
    except Exception as e:
        raise ExtractionError(f"Could not read Word document: {e}")
