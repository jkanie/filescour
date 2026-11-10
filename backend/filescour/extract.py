"""Turn a file into searchable text.

Plain-text files are read as UTF-8. PDFs give the text of their text layer.
Word (.docx) and OpenDocument (.odt) files are zip archives of XML: their
paragraphs are read from that XML, 1 line per paragraph.
Anything else keeps its name, path, size and date in the index, but no content.
A zip file has no text of its own: its entries become rows (see archive.py).
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from pypdf import PdfReader

# Text is kept up to this many characters per file. The rest is cut off, and
# the file is marked as truncated, so that 1 huge log cannot fill the database.
MAX_CHARS = 10_000_000

TEXT_EXTS = frozenset(
    """
    txt text md markdown rst csv tsv json jsonl yaml yml toml ini cfg conf
    xml html htm css scss log srt vtt tex
    py js mjs cjs ts tsx jsx java kt go rs c h cpp hpp cc cs swift rb php
    sql sh bash zsh ps1 bat cmd r m pl lua dart scala
    """.split()
)

PDF_EXTS = frozenset({"pdf"})

DOC_EXTS = frozenset({"docx", "odt"})

# The XML parts of 1 document are read up to this size, uncompressed, such
# that a small zip that inflates to gigabytes is refused, not read.
MAX_DOC_XML_BYTES = 64 * 1024 * 1024

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_ODF_TEXT = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
_ODF_OFFICE = "{urn:oasis:names:tc:opendocument:xmlns:office:1.0}"

# A worker opens these to list their entries (see archive.py).
ZIP_EXTS = frozenset({"zip"})


class ExtractError(Exception):
    """The file was read, but it holds no text that can be indexed."""


@dataclass(frozen=True)
class Extracted:
    text: str
    source: str  # "file", "pdf" or "doc"
    truncated: bool


def wants_content(ext: str) -> bool:
    return ext in TEXT_EXTS or ext in PDF_EXTS or ext in DOC_EXTS


def wants_reading(ext: str) -> bool:
    """Whether a worker opens the file: for its text, or for its zip entries."""
    return wants_content(ext) or ext in ZIP_EXTS


def _clip(text: str) -> tuple[str, bool]:
    # PostgreSQL text cannot hold NUL characters.
    text = text.replace("\x00", "")
    if len(text) > MAX_CHARS:
        return text[:MAX_CHARS], True
    return text, False


def _decode_text(data: bytes) -> str:
    if b"\x00" in data[:8192] and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise ExtractError("binary data in a text-type file")
    for bom, codec in ((b"\xef\xbb\xbf", "utf-8-sig"),
                       (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16")):
        if data.startswith(bom):
            return data.decode(codec)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ExtractError("not UTF-8 text") from exc


def _read_pdf(source: Path | io.BytesIO) -> str:
    try:
        reader = PdfReader(source)
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:  # pypdf raises many types for broken files
        raise ExtractError(f"unreadable PDF: {exc}") from exc
    text = "\n".join(pages)
    if not text.strip():
        raise ExtractError("PDF has no text layer")
    return text


def _xml_part(zf: zipfile.ZipFile, name: str) -> ElementTree.Element:
    try:
        info = zf.getinfo(name)
    except KeyError as exc:
        raise ExtractError(f"no {name} in the document") from exc
    if info.file_size > MAX_DOC_XML_BYTES:
        raise ExtractError(f"{name} is larger than {MAX_DOC_XML_BYTES // (1024 * 1024)} MB")
    with zf.open(info) as f:
        data = f.read(MAX_DOC_XML_BYTES + 1)
    if len(data) > MAX_DOC_XML_BYTES:
        raise ExtractError(f"{name} holds more than it declares")
    # Word and ODF parts never carry a DOCTYPE, and entities can only be
    # declared inside 1, so a part with a DOCTYPE is refused unread. XML spells
    # it in capitals only, and in text it would be escaped as &lt;!DOCTYPE.
    if b"<!DOCTYPE" in data:
        raise ExtractError(f"{name} declares a DOCTYPE")
    try:
        return ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise ExtractError(f"{name} is not well-formed XML: {exc}") from exc


def _docx_paragraphs(root: ElementTree.Element) -> list[str]:
    out = []
    for p in root.iter(f"{_W}p"):
        parts = []
        for el in p.iter():
            if el.tag == f"{_W}t" and el.text:
                parts.append(el.text)
            elif el.tag == f"{_W}tab":
                parts.append("\t")
            elif el.tag in (f"{_W}br", f"{_W}cr"):
                parts.append("\n")
        out.append("".join(parts))
    return out


def _read_docx(zf: zipfile.ZipFile) -> str:
    # The body first, then headers, footers, footnotes, endnotes, in name order.
    names = ["word/document.xml"] + sorted(
        n for n in zf.namelist()
        if n.startswith("word/") and n.count("/") == 1
        and n.split("/")[1].startswith(("header", "footer", "footnotes", "endnotes"))
        and n.endswith(".xml")
    )
    lines = []
    for name in names:
        lines += _docx_paragraphs(_xml_part(zf, name))
    return "\n".join(lines)


def _odt_inline(el: ElementTree.Element, parts: list[str]) -> None:
    """The text of 1 paragraph or heading, with ODF's space, tab and break elements."""
    if el.text:
        parts.append(el.text)
    for child in el:
        if child.tag == f"{_ODF_TEXT}s":
            parts.append(" " * int(child.get(f"{_ODF_TEXT}c", "1")))
        elif child.tag == f"{_ODF_TEXT}tab":
            parts.append("\t")
        elif child.tag == f"{_ODF_TEXT}line-break":
            parts.append("\n")
        elif child.tag in (f"{_ODF_TEXT}note", f"{_ODF_TEXT}p", f"{_ODF_TEXT}h"):
            pass  # a note's own paragraphs are read on their own
        else:
            _odt_inline(child, parts)
        if child.tail:
            parts.append(child.tail)


def _read_odt(zf: zipfile.ZipFile) -> str:
    root = _xml_part(zf, "content.xml")
    body = root.find(f"{_ODF_OFFICE}body")
    lines = []
    for el in (body if body is not None else root).iter():
        if el.tag in (f"{_ODF_TEXT}p", f"{_ODF_TEXT}h"):
            parts: list[str] = []
            _odt_inline(el, parts)
            lines.append("".join(parts))
    return "\n".join(lines)


def _read_doc(source: Path | io.BytesIO, ext: str) -> str:
    try:
        zf = zipfile.ZipFile(source)
    except Exception as exc:  # BadZipFile, OSError, ValueError for a damaged directory
        raise ExtractError(f"unreadable .{ext}: {exc}") from exc
    with zf:
        try:
            text = _read_docx(zf) if ext == "docx" else _read_odt(zf)
        except ExtractError:
            raise
        except Exception as exc:  # zipfile, zlib, bz2, lzma raise many types for broken data
            raise ExtractError(f"unreadable .{ext}: {exc}") from exc
    if not text.strip():
        raise ExtractError(f".{ext} has no text")
    return text


def extract(path: Path, ext: str) -> Extracted:
    if ext in DOC_EXTS:
        text, cut = _clip(_read_doc(path, ext))
        return Extracted(text, "doc", cut)
    if ext in PDF_EXTS:
        text, cut = _clip(_read_pdf(path))
        return Extracted(text, "pdf", cut)
    if ext in TEXT_EXTS:
        text, cut = _clip(_decode_text(path.read_bytes()))
        return Extracted(text, "file", cut)
    raise ExtractError(f"no text reader for .{ext}")


def extract_bytes(data: bytes, ext: str) -> Extracted:
    """The same as extract(), for data already in memory (a zip entry)."""
    if ext in DOC_EXTS:
        text, cut = _clip(_read_doc(io.BytesIO(data), ext))
        return Extracted(text, "doc", cut)
    if ext in PDF_EXTS:
        text, cut = _clip(_read_pdf(io.BytesIO(data)))
        return Extracted(text, "pdf", cut)
    if ext in TEXT_EXTS:
        text, cut = _clip(_decode_text(data))
        return Extracted(text, "file", cut)
    raise ExtractError(f"no text reader for .{ext}")
