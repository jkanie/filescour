"""Builders for test files and folders."""

from __future__ import annotations

import io
import os
import struct
import warnings
import zipfile
from pathlib import Path


def write(path: Path, data: bytes | str, mtime_ns: int | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.write_bytes(data)
    if mtime_ns is not None:
        os.utime(path, ns=(mtime_ns, mtime_ns))
    return path


def pdf_with_text(text: str | None) -> bytes:
    """A 1-page PDF. With text, the page carries a real text layer."""
    stream = b"" if text is None else (
        b"BT /F1 12 Tf 72 720 Td (" + text.encode("latin-1") + b") Tj ET"
    )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode()
    return bytes(out)


ZIP_DATE = (2024, 5, 31, 16, 8, 38)  # zips keep even seconds only


def zip_bytes(entries: list[tuple[str, bytes | str]], encrypted: tuple[str, ...] = (),
              date_time: tuple = ZIP_DATE) -> bytes:
    """A zip of (name, data) entries, compressed with deflate.

    Names go in as given, ".." and all. Python's zipfile cannot encrypt, so
    an entry named in encrypted only gets the "encrypted" flag set in its
    headers, which is all a reader goes by before it asks for a password.
    """
    buf = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # a duplicate name is wanted at times
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, data in entries:
                info = zipfile.ZipInfo(name, date_time=date_time)
                info.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(info, data)
    out = bytearray(buf.getvalue())
    if encrypted:
        with zipfile.ZipFile(io.BytesIO(bytes(out))) as zf:
            infos = zf.infolist()
        for info in infos:
            if info.filename not in encrypted:
                continue
            local = info.header_offset + 6  # flags in the local file header
            out[local:local + 2] = struct.pack("<H", struct.unpack_from("<H", out, local)[0] | 1)
        pos = out.find(b"PK\x01\x02")
        while pos != -1:  # each central directory header
            name_len = struct.unpack_from("<H", out, pos + 28)[0]
            name = out[pos + 46:pos + 46 + name_len].decode("utf-8")
            if name in encrypted:
                flags = struct.unpack_from("<H", out, pos + 8)[0] | 1
                out[pos + 8:pos + 10] = struct.pack("<H", flags)
            pos = out.find(b"PK\x01\x02", pos + 46 + name_len)
    return bytes(out)


_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _docx_part(paragraphs: list[str], root: str = "document") -> str:
    """WordprocessingML paragraphs. A tab in the text becomes <w:tab/>, a new
    line <w:br/>, and every 2nd word goes in a run of its own, as Word often splits them."""
    body = []
    for para in paragraphs:
        runs = []
        for line_no, line in enumerate(para.split("\n")):
            if line_no:
                runs.append("<w:r><w:br/></w:r>")
            for tab_no, chunk in enumerate(line.split("\t")):
                if tab_no:
                    runs.append("<w:r><w:tab/></w:r>")
                words = chunk.split(" ")
                for i in range(0, len(words), 2):
                    piece = " ".join(words[i:i + 2]) + (" " if i + 2 < len(words) else "")
                    runs.append(f'<w:r><w:rPr><w:b/></w:rPr><w:t xml:space="preserve">{xml_escape(piece)}</w:t></w:r>')
        body.append("<w:p>" + "".join(runs) + "</w:p>")
    inner = "".join(body)
    if root == "document":
        inner = f"<w:body>{inner}<w:sectPr/></w:body>"
    return f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:{root} xmlns:w="{_W_NS}">{inner}</w:{root}>'


def xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def docx_bytes(paragraphs: list[str], header: list[str] | None = None,
               footnotes: list[str] | None = None, document_xml: str | None = None) -> bytes:
    """A minimal .docx: content types, the main part, optional header and footnotes."""
    entries = [
        ("[Content_Types].xml",
         '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
         '<Default Extension="xml" ContentType="application/xml"/></Types>'),
        ("word/document.xml", document_xml if document_xml is not None else _docx_part(paragraphs)),
    ]
    if header is not None:
        entries.append(("word/header1.xml", _docx_part(header, "hdr")))
    if footnotes is not None:
        entries.append(("word/footnotes.xml", _docx_part(footnotes, "footnotes")))
    return zip_bytes(entries)


_ODF_NS = ('xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
           'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"')


def odt_bytes(body_xml: str, content_xml: str | None = None) -> bytes:
    """A minimal .odt: the mimetype entry 1st, then content.xml around body_xml."""
    content = content_xml if content_xml is not None else (
        f'<?xml version="1.0" encoding="UTF-8"?><office:document-content {_ODF_NS}>'
        f"<office:body><office:text>{body_xml}</office:text></office:body></office:document-content>")
    return zip_bytes([("mimetype", "application/vnd.oasis.opendocument.text"), ("content.xml", content)])
