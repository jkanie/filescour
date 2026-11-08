"""Turn a file into searchable text.

Plain-text files are read as UTF-8. PDFs give the text of their text layer.
Anything else keeps its name, path, size and date in the index, but no content.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

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


class ExtractError(Exception):
    """The file was read, but it holds no text that can be indexed."""


@dataclass(frozen=True)
class Extracted:
    text: str
    source: str  # "file" or "pdf"
    truncated: bool


def wants_content(ext: str) -> bool:
    return ext in TEXT_EXTS or ext in PDF_EXTS


def _clip(text: str) -> tuple[str, bool]:
    # PostgreSQL text cannot hold NUL characters.
    text = text.replace("\x00", "")
    if len(text) > MAX_CHARS:
        return text[:MAX_CHARS], True
    return text, False


def _read_text(path: Path) -> str:
    data = path.read_bytes()
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


def _read_pdf(path: Path) -> str:
    try:
        reader = PdfReader(path)
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:  # pypdf raises many types for broken files
        raise ExtractError(f"unreadable PDF: {exc}") from exc
    text = "\n".join(pages)
    if not text.strip():
        raise ExtractError("PDF has no text layer")
    return text


def extract(path: Path, ext: str) -> Extracted:
    if ext in PDF_EXTS:
        text, cut = _clip(_read_pdf(path))
        return Extracted(text, "pdf", cut)
    if ext in TEXT_EXTS:
        text, cut = _clip(_read_text(path))
        return Extracted(text, "file", cut)
    raise ExtractError(f"no text reader for .{ext}")
