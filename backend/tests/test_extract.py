import pytest

from filescour import extract
from filescour.extract import ExtractError

from .helpers import pdf_with_text, write


def test_utf8_text(tmp_path):
    p = write(tmp_path / "a.txt", "café line\n")
    got = extract.extract(p, "txt")
    assert (got.text, got.source, got.truncated) == ("café line\n", "file", False)


@pytest.mark.parametrize("data", [
    b"\xef\xbb\xbfwith bom",
    "with bom".encode("utf-16"),
])
def test_byte_order_marks(tmp_path, data):
    p = write(tmp_path / "b.txt", data)
    assert extract.extract(p, "txt").text == "with bom"


def test_binary_in_text_type_is_refused(tmp_path):
    p = write(tmp_path / "c.txt", b"abc\x00def")
    with pytest.raises(ExtractError, match="binary"):
        extract.extract(p, "txt")


def test_non_utf8_is_refused(tmp_path):
    p = write(tmp_path / "d.txt", b"caf\xe9")
    with pytest.raises(ExtractError, match="UTF-8"):
        extract.extract(p, "txt")


def test_pdf_text_layer(tmp_path):
    p = write(tmp_path / "e.pdf", pdf_with_text("Invoice 4417 paid"))
    got = extract.extract(p, "pdf")
    assert got.source == "pdf"
    assert "Invoice 4417 paid" in got.text


def test_pdf_without_text_layer_is_refused(tmp_path):
    p = write(tmp_path / "f.pdf", pdf_with_text(None))
    with pytest.raises(ExtractError, match="no text layer"):
        extract.extract(p, "pdf")


def test_broken_pdf_is_refused(tmp_path):
    p = write(tmp_path / "g.pdf", b"not a pdf at all")
    with pytest.raises(ExtractError, match="unreadable PDF"):
        extract.extract(p, "pdf")


def test_long_text_is_cut_and_marked(tmp_path, monkeypatch):
    monkeypatch.setattr(extract, "MAX_CHARS", 10)
    p = write(tmp_path / "h.txt", "0123456789abcdef")
    got = extract.extract(p, "txt")
    assert (got.text, got.truncated) == ("0123456789", True)


def test_unknown_type_has_no_reader(tmp_path):
    p = write(tmp_path / "i.bin", b"\x01\x02")
    assert not extract.wants_content("bin")
    with pytest.raises(ExtractError):
        extract.extract(p, "bin")
