import pytest

from filescour import indexer, jobs
from filescour.search import BadQuery, Filters, search

from .helpers import docx_bytes, odt_bytes, pdf_with_text, write, zip_bytes


def build(conn, tmp_path, files):
    for rel, data in files.items():
        write(tmp_path / rel, data)
    rid = indexer.add_root(conn, str(tmp_path))
    indexer.scan_root(conn, rid)
    jobs.run(conn)


def test_regex_returns_the_first_5_matching_lines_in_order(conn, tmp_path):
    text = "".join(f"hit {i}\nmiss\n" for i in range(1, 9))
    build(conn, tmp_path, {"many.txt": text})
    (hit,) = search(conn, "^hit", "regex").hits
    assert [(ln.line, ln.text) for ln in hit.lines] == [
        (1, "hit 1"), (3, "hit 2"), (5, "hit 3"), (7, "hit 4"), (9, "hit 5")]


def test_anchors_work_on_every_line(conn, tmp_path):
    build(conn, tmp_path, {"a.txt": "first\nsecond line\n"})
    for pattern in (r"^second", r"\Asecond", r"line$", r"line\Z", r"(?i)^SECOND"):
        (hit,) = search(conn, pattern, "regex").hits
        assert [ln.line for ln in hit.lines] == [2], pattern


def test_long_lines_are_cut_to_300_characters(conn, tmp_path):
    build(conn, tmp_path, {"long.txt": "x" * 1000 + "\n"})
    (hit,) = search(conn, "x+", "regex").hits
    assert hit.lines[0].text == "x" * 300


def test_windows_line_endings_are_dropped_from_lines(conn, tmp_path):
    build(conn, tmp_path, {"crlf.txt": "first\r\nsecond\r\n"})
    (hit,) = search(conn, "sec", "regex").hits
    assert [(ln.line, ln.text) for ln in hit.lines] == [(2, "second")]


def test_like_wildcards_in_a_name_query_are_literal(conn, tmp_path):
    build(conn, tmp_path, {"100%_done.txt": "a", "100x_done.txt": "b", "1000done.txt": "c"})
    assert [h.name for h in search(conn, "%_", "name").hits] == ["100%_done.txt"]
    assert [h.name for h in search(conn, "x_", "name").hits] == ["100x_done.txt"]
    # As a LIKE wildcard, "_" would match "0d", "0%" and "0x" in all 3 names.
    assert search(conn, "0_", "name").hits == []


def test_case_sensitive_search(conn, tmp_path):
    build(conn, tmp_path, {"Report.txt": "Total due\n", "report.md": "total due\n"})
    assert [h.name for h in search(conn, "Report", "name", ignore_case=False).hits] == ["Report.txt"]
    assert [h.name for h in search(conn, "Report", "name").hits] == ["Report.txt", "report.md"]
    assert [h.name for h in search(conn, "^Total", "regex", ignore_case=False).hits] == ["Report.txt"]


def test_word_and_odf_text_is_searchable_and_filterable(conn, tmp_path):
    build(conn, tmp_path, {
        "letter.docx": docx_bytes(["Dear Ms Rao,", "Order 4417 ships on Monday."]),
        "minutes.odt": odt_bytes("<text:p>Order 4417 was discussed.</text:p>"),
        "order.txt": "Order 4417",
    })
    assert [h.name for h in search(conn, "4417", "words", Filters(source="doc")).hits] == ["letter.docx", "minutes.odt"]
    (hit,) = search(conn, "ships", "regex", Filters(source="doc")).hits
    assert [(ln.line, ln.text) for ln in hit.lines] == [(2, "Order 4417 ships on Monday.")]
    assert len(search(conn, "4417", "words").hits) == 3


def test_a_word_file_inside_a_zip_gets_its_text(conn, tmp_path):
    build(conn, tmp_path, {"backup.zip": zip_bytes([("letters/a.docx", docx_bytes(["Receipt 9921 paid"]))])})
    (hit,) = search(conn, "9921", "words", Filters(source="doc")).hits
    assert hit.path.endswith("backup.zip!/letters/a.docx")
    assert hit.source == "doc"


def test_pdf_text_is_searchable_and_filterable(conn, tmp_path):
    build(conn, tmp_path, {"inv.pdf": pdf_with_text("Invoice 4417 paid"), "inv.txt": "Invoice 4417"})
    assert [h.name for h in search(conn, "4417", "words", Filters(source="pdf")).hits] == ["inv.pdf"]
    assert [h.name for h in search(conn, "4417", "words").hits] == ["inv.pdf", "inv.txt"]


def test_words_search_reports_a_snippet(conn, tmp_path):
    build(conn, tmp_path, {"a.txt": "the quarterly\nreport is late\n"})
    (hit,) = search(conn, "quarterly report", "words").hits
    assert hit.lines[0].line is None
    assert hit.lines[0].text == "the quarterly report is late"


@pytest.mark.parametrize("q, mode, msg", [
    ("(", "regex", "invalid regular expression"),
    ("", "regex", "regular expression is needed"),
    ("  ", "words", "at least 1 word"),
    ("x", "sql", "mode must be"),
])
def test_bad_queries(conn, q, mode, msg):
    with pytest.raises(BadQuery, match=msg):
        search(conn, q, mode)


def test_limit_bounds_and_total(conn, tmp_path):
    build(conn, tmp_path, {f"f{i}.txt": "x" for i in range(5)})
    r = search(conn, "", "name", limit=2)
    assert (r.total, [h.name for h in r.hits]) == (5, ["f0.txt", "f1.txt"])
    for bad in (0, 1001):
        with pytest.raises(BadQuery, match="limit"):
            search(conn, "", "name", limit=bad)


def test_sort_by_name_size_modified_either_way(conn, tmp_path):
    day = 86_400 * 10**9
    for rel, data, age in [("b/zeta.txt", "1234", 3), ("a/alpha.txt", "12", 1),
                           ("c/Beta.txt", "1234", 2), ("a/gamma.txt", "123456", 0)]:
        write(tmp_path / rel, data, mtime_ns=1_700_000_000 * 10**9 - age * day)
    rid = indexer.add_root(conn, str(tmp_path))
    indexer.scan_root(conn, rid)

    def order(sort):
        r = search(conn, "", "name", sort=sort)
        assert r.sort == sort
        return [h.path.removeprefix(str(tmp_path) + "/") for h in r.hits]

    by_path = ["a/alpha.txt", "a/gamma.txt", "b/zeta.txt", "c/Beta.txt"]
    assert order("path") == by_path
    assert order("-path") == by_path[::-1]
    # Byte order, as for the path: upper case before lower case.
    assert order("name") == ["c/Beta.txt", "a/alpha.txt", "a/gamma.txt", "b/zeta.txt"]
    # zeta.txt and Beta.txt are both 4 bytes, so the path breaks the tie,
    # A to Z in both directions.
    assert order("size") == ["a/alpha.txt", "b/zeta.txt", "c/Beta.txt", "a/gamma.txt"]
    assert order("-size") == ["a/gamma.txt", "b/zeta.txt", "c/Beta.txt", "a/alpha.txt"]
    assert order("modified") == ["b/zeta.txt", "c/Beta.txt", "a/alpha.txt", "a/gamma.txt"]
    assert order("-modified") == ["a/gamma.txt", "a/alpha.txt", "c/Beta.txt", "b/zeta.txt"]
    # The limit applies after the sort.
    assert [h.name for h in search(conn, "", "name", limit=1, sort="-size").hits] == ["gamma.txt"]
    with pytest.raises(BadQuery, match="sort must be 1 of path, -path, name"):
        search(conn, "", "name", sort="mtime")

