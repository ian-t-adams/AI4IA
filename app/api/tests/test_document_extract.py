"""Unit tests for document text extraction.

Covers each supported format (text, docx, pptx, pdf), magic-byte vs extension
routing, truncation, control-byte stripping, and the unsupported/empty/corrupt
error mapping plus the zip-bomb guard.
"""
from __future__ import annotations

import io
import zipfile

import pytest

from ai4ia_api.documents import extract as ex
from ai4ia_api.documents.extract import (
    MAX_DOC_CHARS,
    CorruptDocumentError,
    DocumentError,
    EmptyDocumentError,
    UnsupportedDocumentError,
    extract_text,
)

DOCX_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
PPTX_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
PPTX_P = "http://schemas.openxmlformats.org/presentationml/2006/main"


def _docx(*paragraphs: str) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = (
        f'<?xml version="1.0"?><w:document xmlns:w="{DOCX_NS}">'
        f"<w:body>{body}</w:body></w:document>"
    ).encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()


def _pptx(*slides: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, text in enumerate(slides, start=1):
            xml = (
                f'<?xml version="1.0"?><p:sld xmlns:a="{PPTX_A}" xmlns:p="{PPTX_P}">'
                f"<p:cSld><p:spTree><a:p><a:r><a:t>{text}</a:t></a:r></a:p>"
                f"</p:spTree></p:cSld></p:sld>"
            ).encode()
            zf.writestr(f"ppt/slides/slide{i}.xml", xml)
    return buf.getvalue()


def _pdf(text: str) -> bytes:
    """A minimal single-page PDF with correct xref offsets that pypdf extracts."""
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
    ]
    stream = b"BT /F1 24 Tf 72 700 Td (" + text.encode("latin-1") + b") Tj ET"
    objs.append(
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
    )
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    pdf = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(pdf))
        pdf += str(i).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    xref_pos = len(pdf)
    count = len(objs) + 1
    pdf += b"xref\n0 " + str(count).encode() + b"\n0000000000 65535 f \n"
    for off in offsets:
        pdf += ("%010d 00000 n \n" % off).encode()
    pdf += b"trailer\n<< /Size " + str(count).encode() + b" /Root 1 0 R >>\n"
    pdf += b"startxref\n" + str(xref_pos).encode() + b"\n%%EOF"
    return bytes(pdf)


async def test_text_family_decodes_and_keeps_content():
    text, truncated = await extract_text("notes.md", "text/markdown", b"# Title\nHello there")
    assert "Hello there" in text
    assert truncated is False


async def test_unknown_binary_is_unsupported():
    with pytest.raises(UnsupportedDocumentError):
        await extract_text("clip.bin", "application/octet-stream", b"\x01\x02\x03\x04stuff")


async def test_empty_file_is_empty_error():
    with pytest.raises(EmptyDocumentError):
        await extract_text("blank.txt", "text/plain", b"")


async def test_whitespace_only_is_empty_error():
    with pytest.raises(EmptyDocumentError):
        await extract_text("blank.txt", "text/plain", b"   \n\t  ")


async def test_docx_paragraphs_extracted_in_order():
    data = _docx("First paragraph", "Second paragraph")
    text, _ = await extract_text(
        "doc.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        data,
    )
    assert "First paragraph" in text
    assert "Second paragraph" in text
    assert text.index("First") < text.index("Second")


async def test_docx_extension_but_not_zip_is_corrupt():
    with pytest.raises(CorruptDocumentError):
        await extract_text("doc.docx", "application/octet-stream", b"not a zip at all")


async def test_pptx_slides_extracted():
    data = _pptx("Slide one heading", "Slide two heading")
    text, _ = await extract_text(
        "deck.pptx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        data,
    )
    assert "Slide one heading" in text
    assert "Slide two heading" in text


async def test_pdf_text_extracted():
    data = _pdf("Hello from a PDF document")
    text, _ = await extract_text("report.pdf", "application/pdf", data)
    assert "Hello from a PDF" in text


async def test_pdf_magic_overrides_wrong_extension():
    # Bytes are a real PDF even though the name says .txt -> routed to the PDF path.
    data = _pdf("Routed by magic bytes")
    text, _ = await extract_text("mislabeled.txt", "application/octet-stream", data)
    assert "Routed by magic bytes" in text


async def test_corrupt_pdf_raises_document_error():
    with pytest.raises(DocumentError):
        await extract_text("broken.pdf", "application/pdf", b"%PDF-1.4 then total garbage")


async def test_truncation_flag_and_cap():
    text, truncated = await extract_text("big.txt", "text/plain", b"a" * (MAX_DOC_CHARS + 500))
    assert truncated is True
    assert len(text) == MAX_DOC_CHARS


async def test_control_bytes_stripped():
    text, _ = await extract_text("ctrl.txt", "text/plain", b"Hello\x00\x07World")
    assert "\x00" not in text
    assert "\x07" not in text
    assert "Hello" in text and "World" in text


async def test_zip_bomb_guard(monkeypatch):
    monkeypatch.setattr(ex, "_MAX_ZIP_TOTAL_UNCOMPRESSED", 10)
    data = _docx("This document body easily exceeds ten uncompressed bytes")
    with pytest.raises(CorruptDocumentError):
        await extract_text(
            "doc.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            data,
        )


async def test_encrypted_or_unreadable_zip_member_is_corrupt(monkeypatch):
    # A member that raises at read time (e.g. encrypted / unsupported compression)
    # must surface as a clean CorruptDocumentError, not an unhandled 500.
    data = _docx("body text")

    def _boom(self, name, pwd=None):
        raise RuntimeError("File is encrypted, password required for extraction")

    monkeypatch.setattr(zipfile.ZipFile, "read", _boom)
    with pytest.raises(CorruptDocumentError):
        await extract_text(
            "doc.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            data,
        )
