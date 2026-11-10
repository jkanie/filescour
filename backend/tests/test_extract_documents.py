"""Word (.docx) and OpenDocument (.odt) text, from small documents built in the tests."""

import pytest

from filescour import extract
from filescour.extract import ExtractError

from .helpers import docx_bytes, odt_bytes, write, zip_bytes


def test_docx_paragraphs_tabs_breaks(tmp_path):
    p = write(tmp_path / "letter.docx", docx_bytes(
        ["Dear Ms Rao,", "Your order 4417 ships on Monday.", "Item\tQty", "Line 1\nLine 2", ""]))
    got = extract.extract(p, "docx")
    assert got.source == "doc"
    assert got.truncated is False
    assert got.text == "Dear Ms Rao,\nYour order 4417 ships on Monday.\nItem\tQty\nLine 1\nLine 2\n"


def test_docx_reads_the_body_then_the_other_parts_in_name_order(tmp_path):
    p = write(tmp_path / "report.docx", docx_bytes(
        ["Quarterly report"], header=["Draft 3"], footnotes=["Source: ledger"]))
    assert extract.extract(p, "docx").text == "Quarterly report\nSource: ledger\nDraft 3"


def test_docx_keeps_special_characters(tmp_path):
    p = write(tmp_path / "s.docx", docx_bytes(["Café & Co <draft> — 5 µm"]))
    assert extract.extract(p, "docx").text == "Café & Co <draft> — 5 µm"


def test_odt_paragraphs_headings_spaces_tabs_breaks(tmp_path):
    body = (
        '<text:h text:outline-level="1">Minutes</text:h>'
        '<text:p>Present:<text:s text:c="3"/>4 people<text:tab/>room <text:span>B</text:span>12</text:p>'
        "<text:p>Next meeting<text:line-break/>on Friday</text:p>"
        "<text:list><text:list-item><text:p>Budget</text:p></text:list-item></text:list>"
        "<text:p>See note<text:note><text:note-citation>1</text:note-citation>"
        "<text:note-body><text:p>Room booked.</text:p></text:note-body></text:note> above.</text:p>"
    )
    got = extract.extract(write(tmp_path / "m.odt", odt_bytes(body)), "odt")
    assert got.source == "doc"
    assert got.text.split("\n") == [
        "Minutes", "Present:   4 people\troom B12", "Next meeting", "on Friday",
        "Budget", "See note above.", "Room booked."]


def test_documents_inside_a_zip_entry():
    got = extract.extract_bytes(docx_bytes(["Packed inside a backup"]), "docx")
    assert (got.text, got.source) == ("Packed inside a backup", "doc")


@pytest.mark.parametrize("ext, data, message", [
    ("docx", b"not a zip", "unreadable .docx"),
    ("odt", b"PK\x03\x04 broken", "unreadable .odt"),
    ("docx", zip_bytes([("other.xml", "<a/>")]), "no word/document.xml"),
    ("odt", zip_bytes([("mimetype", "x")]), "no content.xml"),
    ("docx", docx_bytes([], document_xml="<w:document"), "not well-formed"),
    ("docx", docx_bytes([""]), "has no text"),
])
def test_bad_documents_are_refused(tmp_path, ext, data, message):
    p = write(tmp_path / f"bad.{ext}", data)
    with pytest.raises(ExtractError, match=message):
        extract.extract(p, ext)


def test_damaged_compressed_data_is_refused(tmp_path):
    data = bytearray(docx_bytes(["Some text that deflate packs into a stream of its own"]))
    start = data.find(b"word/document.xml") + len("word/document.xml")
    data[start + 5:start + 25] = b"\xff" * 20  # inside the deflate stream of the main part
    p = write(tmp_path / "damaged.docx", bytes(data))
    with pytest.raises(ExtractError, match="unreadable .docx"):
        extract.extract(p, "docx")
    with pytest.raises(ExtractError, match="unreadable .docx"):
        extract.extract_bytes(bytes(data), "docx")


def test_a_doctype_is_refused_before_parsing(tmp_path):
    laughs = ('<?xml version="1.0"?><!DOCTYPE d [<!ENTITY a "aaaaaaaaaa">'
              '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]><d>&b;</d>')
    p = write(tmp_path / "bomb.odt", odt_bytes("", content_xml=laughs))
    with pytest.raises(ExtractError, match="DOCTYPE"):
        extract.extract(p, "odt")


def test_a_part_over_the_size_limit_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(extract, "MAX_DOC_XML_BYTES", 200)
    p = write(tmp_path / "big.docx", docx_bytes(["x" * 1000]))
    with pytest.raises(ExtractError, match="larger than"):
        extract.extract(p, "docx")


def test_long_document_text_is_cut_and_marked(tmp_path, monkeypatch):
    monkeypatch.setattr(extract, "MAX_CHARS", 5)
    got = extract.extract(write(tmp_path / "l.docx", docx_bytes(["0123456789"])), "docx")
    assert (got.text, got.truncated) == ("01234", True)


def test_docx_and_odt_are_read():
    assert extract.wants_content("docx") and extract.wants_content("odt")
    assert not extract.wants_content("doc")  # the old binary Word format has no reader
