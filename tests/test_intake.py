import io

import pymupdf
import pytest
from PIL import Image

from ledgersync.errors import FileTooLarge, UnreadableFile, UnsupportedFile
from ledgersync.intake import from_text, load_upload, read_limited

OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def png_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (40, 20), "white").save(buf, "PNG")
    return buf.getvalue()


def pdf_bytes(pages=1, **save_kwargs):
    doc = pymupdf.open()
    for i in range(pages):
        doc.new_page().insert_text((72, 72), f"Page {i + 1}")
    return doc.tobytes(**save_kwargs)


def xlsx_bytes():
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Sept"
    ws.append(["Date", "Description", "Amount"])
    ws.append(["01/09/2026", "BT BROADBAND", -72])
    wb.create_sheet("Oct").append(["02/10/2026", "ACME LTD", 3600])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def load(name, data, pages=30):
    return load_upload(name, data, max_pdf_pages=pages)


def test_read_limited_rejects_oversized_stream():
    with pytest.raises(FileTooLarge):
        read_limited(io.BytesIO(b"x" * 11), max_bytes=10)


def test_read_limited_accepts_exact_limit():
    assert read_limited(io.BytesIO(b"x" * 10), max_bytes=10) == b"x" * 10


def test_csv_utf8_bom_is_stripped():
    intake = load("s.csv", "﻿Date,Amount\n01/09/2026,72.00".encode("utf-8"))
    assert intake.kind == "table" and intake.text.startswith("Date,Amount")


def test_csv_in_windows_1252_keeps_pound_sign():
    assert "£72.00" in load("s.csv", "Date,Amount\n01/09/2026,£72.00".encode("cp1252")).text


def test_utf16_unicode_text_export_is_read():
    assert "£72.00" in load("s.txt", "Date\tAmount\n01/09/2026\t£72.00".encode("utf-16")).text


def test_binary_disguised_as_csv_is_rejected():
    with pytest.raises(UnreadableFile):
        load("s.csv", b"\x00\x01\x02binary")


def test_empty_file_is_rejected():
    with pytest.raises(UnreadableFile, match="empty"):
        load("s.csv", b"")


def test_xlsx_reads_every_sheet():
    intake = load("s.xlsx", xlsx_bytes())
    assert intake.kind == "table"
    assert "BT BROADBAND" in intake.text and "ACME LTD" in intake.text and "# Sheet: Oct" in intake.text


def test_corrupt_xls_is_rejected_not_decoded_as_text():
    with pytest.raises(UnreadableFile):
        load("s.xls", OLE + b"\x00garbage" * 50)


def test_xls_uses_xlrd(monkeypatch):
    import pandas as pd
    seen = {}

    def fake_read_excel(buf, **kwargs):
        seen.update(kwargs)
        return {"Sheet1": pd.DataFrame([["01/09/2026", "BT", -72]])}

    monkeypatch.setattr(pd, "read_excel", fake_read_excel)
    intake = load("s.xls", OLE + b"\x00" * 100)
    assert seen["engine"] == "xlrd" and "BT" in intake.text


def test_tab_separated_text_named_xls_is_read_as_table():
    intake = load("export.xls", b"Date\tAmount\n01/09/2026\t72.00")
    assert intake.kind == "table" and "72.00" in intake.text


def test_pdf_is_accepted():
    intake = load("s.pdf", pdf_bytes(2))
    assert intake.kind == "pdf" and intake.data.startswith(b"%PDF")


def test_password_protected_pdf_is_rejected_with_advice():
    data = pdf_bytes(1, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="u", owner_pw="o")
    with pytest.raises(UnreadableFile, match="password"):
        load("s.pdf", data)


def test_pdf_over_page_limit_is_rejected():
    with pytest.raises(FileTooLarge, match="4 pages"):
        load("s.pdf", pdf_bytes(4), pages=3)


def test_corrupt_pdf_is_rejected():
    with pytest.raises(UnreadableFile):
        load("s.pdf", b"%PDF-1.4\n%garbage")


def test_png_is_accepted_even_with_wrong_extension():
    assert load("receipt.pdf", png_bytes()).kind == "image"


def test_corrupt_png_is_rejected():
    with pytest.raises(UnreadableFile):
        load("r.png", b"\x89PNG\r\n\x1a\n" + b"junk")


def test_text_starting_with_bm_is_not_mistaken_for_a_bitmap():
    assert load("s.csv", b"BMW Finance,72.00\n").kind == "table"


def test_unknown_binary_is_unsupported():
    with pytest.raises(UnsupportedFile):
        load("notes.docx", b"PK\x03\x04" + b"\x00" * 50)


def test_whitespace_text_is_rejected():
    with pytest.raises(UnreadableFile):
        from_text("   \n  ", max_bytes=1000)


def test_oversized_text_is_rejected():
    with pytest.raises(FileTooLarge):
        from_text("x" * 2000, max_bytes=1000)
