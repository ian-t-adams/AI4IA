"""Local, dependency-light extraction of plain text from uploaded documents.

The extracted text is later injected (capped) into a chat turn as untrusted
reference context, so extraction must be cheap, safe, and never block the event
loop. Supported formats: a broad text/code family (decoded as UTF-8 with
replacement), PDF (``pypdf``), and Office Open XML ``.docx`` / ``.pptx`` (parsed
with the stdlib ``zipfile`` + ``ElementTree`` — no extra dependency).

Safety properties:
- CPU-bound parsing runs in a worker thread (``run_in_threadpool``).
- Zip archives (docx/pptx) are bounded against zip-bombs (total uncompressed
  byte cap) and PDFs against an excessive page count.
- Format is decided by magic bytes first, then extension / content-type — a
  ``.docx`` whose bytes aren't a zip is reported as corrupt, not mis-parsed.
- Empty extraction (e.g. a scanned/image-only PDF) raises
  :class:`EmptyDocumentError` (mapped to 422) rather than storing a blank doc.
- Null / control bytes are stripped and the result is capped at
  :data:`MAX_DOC_CHARS`.

Out of scope here (deferred): OCR / Azure AI Content Understanding for scanned
images, spreadsheets (xlsx), and audio/video.
"""
from __future__ import annotations

import io
import re
import zipfile
import zlib
from xml.etree import ElementTree as ET

from fastapi.concurrency import run_in_threadpool

# Per-document storage cap (characters of extracted text). The chat-time context
# budget (see chat.py) is separate and usually smaller.
MAX_DOC_CHARS = 32_000

# Zip-bomb guard: refuse Office files whose members exceed this uncompressed size.
_MAX_ZIP_TOTAL_UNCOMPRESSED = 60_000_000

# Cap pages scanned in a PDF (defensive against pathologically large files).
_MAX_PDF_PAGES = 300

# Stop accumulating once we have comfortably more than the cap (we trim later).
_HARVEST_LIMIT = MAX_DOC_CHARS * 2

# OOXML namespaces.
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"

# Extensions parsed as plain UTF-8 text/code.
_TEXT_EXTS = {
    "txt", "text", "md", "markdown", "rst", "csv", "tsv", "json", "jsonl",
    "ndjson", "log", "yaml", "yml", "xml", "html", "htm", "ini", "toml",
    "cfg", "conf", "env", "properties",
    "py", "js", "ts", "tsx", "jsx", "mjs", "cjs", "java", "c", "h", "cpp",
    "cc", "hpp", "cs", "go", "rs", "rb", "php", "sh", "bash", "zsh", "ps1",
    "sql", "r", "kt", "kts", "swift", "scala", "lua", "pl", "dart", "groovy",
    "tf", "bicep", "dockerfile", "make", "mk", "gradle",
}

_TEXT_CTYPES = {
    "application/json", "application/xml", "application/x-yaml",
    "application/yaml", "application/javascript", "application/x-sh",
    "application/toml",
}


class DocumentError(Exception):
    """Base for extraction failures (the router maps subclasses to 4xx)."""


class UnsupportedDocumentError(DocumentError):
    """The file type isn't supported (mapped to 415)."""


class EmptyDocumentError(DocumentError):
    """The file yielded no readable text (mapped to 422)."""


class CorruptDocumentError(DocumentError):
    """The file claimed a type its bytes don't match, or couldn't be parsed."""


def _ext(filename: str) -> str:
    name = (filename or "").lower()
    dot = name.rfind(".")
    return name[dot + 1:] if dot != -1 else ""


def _base_ctype(content_type: str) -> str:
    return (content_type or "").split(";", 1)[0].strip().lower()


def _looks_like_zip(data: bytes) -> bool:
    return data[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")


def _looks_like_pdf(data: bytes) -> bool:
    return data[:5] == b"%PDF-"


def _clean(text: str) -> str:
    """Drop null + C0 control bytes (keeping tab/newline) and normalize newlines."""
    out: list[str] = []
    for ch in text:
        if ch in ("\t", "\n", "\r"):
            out.append(ch)
            continue
        o = ord(ch)
        if o < 0x20 or o == 0x7F:
            continue
        out.append(ch)
    return "".join(out).replace("\r\n", "\n").replace("\r", "\n")


def _cap(text: str) -> tuple[str, bool]:
    text = text.strip()
    if len(text) > MAX_DOC_CHARS:
        return text[:MAX_DOC_CHARS].rstrip(), True
    return text, False


def _open_zip(data: bytes) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise CorruptDocumentError("not a valid Office Open XML file") from exc


def _guard_zip_size(zf: zipfile.ZipFile, names: list[str]) -> None:
    total = 0
    for name in names:
        try:
            total += zf.getinfo(name).file_size
        except KeyError:
            continue
        if total > _MAX_ZIP_TOTAL_UNCOMPRESSED:
            raise CorruptDocumentError("document is too large to process")


def _safe_zip_read(zf: zipfile.ZipFile, name: str) -> bytes:
    """Read a zip member, converting every failure mode (encrypted member,
    unsupported compression, corrupt data) into a clean ``CorruptDocumentError``
    rather than letting a raw RuntimeError/zlib error escape as a 500."""
    try:
        return zf.read(name)
    except (RuntimeError, NotImplementedError, zipfile.BadZipFile, zlib.error, OSError) as exc:
        raise CorruptDocumentError("could not read document part") from exc


def _ooxml_text(xml: bytes, *, text_tag: str, para_tag: str) -> str:
    """Concatenate text nodes in document order, inserting a newline at each
    paragraph boundary so word/slide structure survives as line breaks."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise CorruptDocumentError("malformed document XML") from exc
    parts: list[str] = []
    for el in root.iter():
        if el.tag == para_tag:
            parts.append("\n")
        elif el.tag == text_tag and el.text:
            parts.append(el.text)
    return "".join(parts)


def _slide_number(name: str) -> int:
    match = re.search(r"slide(\d+)\.xml$", name)
    return int(match.group(1)) if match else 0


def _extract_text_family(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _extract_docx(data: bytes) -> str:
    zf = _open_zip(data)
    with zf:
        if "word/document.xml" not in zf.namelist():
            raise CorruptDocumentError("not a valid .docx file")
        _guard_zip_size(zf, ["word/document.xml"])
        xml = _safe_zip_read(zf, "word/document.xml")
    return _ooxml_text(xml, text_tag=f"{_W}t", para_tag=f"{_W}p")


def _extract_pptx(data: bytes) -> str:
    zf = _open_zip(data)
    chunks: list[str] = []
    with zf:
        slides = [
            n for n in zf.namelist()
            if n.startswith("ppt/slides/slide") and n.endswith(".xml")
        ]
        if not slides:
            raise CorruptDocumentError("not a valid .pptx file")
        slides.sort(key=_slide_number)
        _guard_zip_size(zf, slides)
        for name in slides:
            chunks.append(_ooxml_text(_safe_zip_read(zf, name), text_tag=f"{_A}t", para_tag=f"{_A}p"))
            if sum(len(c) for c in chunks) > _HARVEST_LIMIT:
                break
    return "\n\n".join(c for c in chunks if c.strip())


def _extract_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise UnsupportedDocumentError("PDF support is unavailable") from exc

    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - pypdf raises a variety of types
        raise CorruptDocumentError("could not read the PDF") from exc

    if reader.is_encrypted:
        # Many PDFs are "encrypted" with an empty owner password; try that once.
        try:
            reader.decrypt("")
        except Exception:  # noqa: BLE001
            raise UnsupportedDocumentError(
                "password-protected PDFs aren't supported"
            ) from None

    parts: list[str] = []
    try:
        pages = reader.pages
    except Exception as exc:  # noqa: BLE001
        raise CorruptDocumentError("could not read the PDF") from exc

    for index, page in enumerate(pages):
        if index >= _MAX_PDF_PAGES:
            break
        try:
            parts.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001 - skip a single unreadable page
            continue
        if sum(len(p) for p in parts) > _HARVEST_LIMIT:
            break
    return "\n".join(p for p in parts if p)


def _extract_sync(filename: str, content_type: str, data: bytes) -> tuple[str, bool]:
    if not data:
        raise EmptyDocumentError("the file is empty")

    ext = _ext(filename)
    ctype = _base_ctype(content_type)
    is_zip = _looks_like_zip(data)

    if _looks_like_pdf(data) or ext == "pdf" or ctype == "application/pdf":
        raw = _extract_pdf(data)
    elif is_zip and (ext == "docx" or "wordprocessingml" in ctype):
        raw = _extract_docx(data)
    elif is_zip and (ext == "pptx" or "presentationml" in ctype):
        raw = _extract_pptx(data)
    elif ext == "docx" or "wordprocessingml" in ctype:
        raise CorruptDocumentError("not a valid .docx file")
    elif ext == "pptx" or "presentationml" in ctype:
        raise CorruptDocumentError("not a valid .pptx file")
    elif (
        ext in _TEXT_EXTS
        or ctype.startswith("text/")
        or ctype in _TEXT_CTYPES
    ):
        raw = _extract_text_family(data)
    else:
        raise UnsupportedDocumentError(
            f"unsupported file type: {ext or ctype or 'unknown'}"
        )

    text, truncated = _cap(_clean(raw))
    if not text.strip():
        raise EmptyDocumentError(
            "no readable text found (scanned or image-only documents "
            "aren't supported yet)"
        )
    return text, truncated


async def extract_text(
    filename: str, content_type: str, data: bytes
) -> tuple[str, bool]:
    """Extract ``(text, truncated)`` from an uploaded file, off the event loop.

    Raises a :class:`DocumentError` subclass on unsupported/empty/corrupt input.
    """
    return await run_in_threadpool(_extract_sync, filename, content_type, data)
