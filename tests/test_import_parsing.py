"""services/import_parsing.py: reading CSV / .xlsx uploads and parsing typed values. No database needed."""
import io
import re
from datetime import date, datetime
from decimal import Decimal

import pytest
from openpyxl import Workbook

from services.import_parsing import (
    MAX_ROWS,
    ROW_NUMBER,
    ImportFileError,
    InvalidValueError,
    match_columns,
    normalise_header,
    parse_amount,
    parse_currency,
    parse_date,
    read_table,
    uses_decimal_comma,
)

KNOWN = ["Item", "Vendor", "Amount", "Invoice No", "Date"]


def _xlsx(build) -> bytes:
    workbook = Workbook()
    build(workbook)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


# --- read_table ---

def test_csv_with_bom_and_commas():
    data = "﻿Item,Vendor,Amount\nCarpet,Freeman,600\n,,\nLighting,Freeman,450\n".encode("utf-8")

    rows = read_table("costs.csv", data, KNOWN)

    assert rows == [
        {"Item": "Carpet", "Vendor": "Freeman", "Amount": "600", ROW_NUMBER: 2},
        {"Item": "Lighting", "Vendor": "Freeman", "Amount": "450", ROW_NUMBER: 4},  # blank row 3 skipped
    ]


def test_csv_with_semicolons_and_decimal_commas():
    data = "Item;Amount\nCarpet;1.200,50\n".encode("utf-8")

    rows = read_table("costs.CSV", data, KNOWN)

    assert rows == [{"Item": "Carpet", "Amount": "1.200,50", ROW_NUMBER: 2}]


def test_xlsx_skips_title_rows_and_reads_values_not_formulas():
    def build(workbook):
        hidden = workbook.active
        hidden.title = "Notes"
        hidden.sheet_state = "hidden"  # the first visible sheet is read
        sheet = workbook.create_sheet("Costs")
        sheet.append(["Facility Expo 2026 — cost sheet"])  # title row above the header
        sheet.append([])
        sheet.append(["  Item ", "Vendor", "AMOUNT", "invoice_no", "Date", "", "Notes 2"])
        sheet.append(["Carpet", "Freeman", 600, "INV-1", datetime(2026, 11, 4), "", "ignore me"])
        sheet.append(["Total", None, "=SUM(C4:C4)", None, None])  # a formula cell

    rows = read_table("Expo costs.xlsx", _xlsx(build), KNOWN)

    assert rows[0] == {
        "Item": "Carpet", "Vendor": "Freeman", "AMOUNT": "600", "invoice_no": "INV-1",
        "Date": "2026-11-04", "Notes 2": "ignore me", ROW_NUMBER: 4,
    }
    # The formula itself is never returned (openpyxl saved no calculated value, so it reads as blank)
    assert rows[1]["AMOUNT"] == "" and rows[1][ROW_NUMBER] == 5


def test_header_must_have_known_columns():
    data = b"Name,Phone\nAda,123\n"

    with pytest.raises(ImportFileError, match="header row"):
        read_table("people.csv", data, KNOWN)


def test_first_non_blank_row_is_the_header_without_known_columns():
    rows = read_table("a.csv", b"\n\nA,B\n1,2\n")

    assert rows == [{"A": "1", "B": "2", ROW_NUMBER: 4}]


def test_repeated_headers_are_kept_apart():
    rows = read_table("a.csv", b"Item,Amount,Amount\nCarpet,1,2\n", KNOWN)

    assert rows[0]["Amount"] == "1" and rows[0]["Amount (2)"] == "2"


def test_too_many_rows():
    data = ("Item,Amount\n" + "Carpet,1\n" * (MAX_ROWS + 1)).encode()

    with pytest.raises(ImportFileError, match="more than the 5,000 limit"):
        read_table("big.csv", data, KNOWN)


def test_exactly_the_limit_is_fine():
    data = ("Item,Amount\n" + "Carpet,1\n" * MAX_ROWS).encode()

    assert len(read_table("ok.csv", data, KNOWN)) == MAX_ROWS


@pytest.mark.parametrize(
    ("filename", "data", "message"),
    [
        ("costs.pdf", b"%PDF-1.4", "Upload an Excel (.xlsx) or CSV file"),
        ("costs.xls", b"\xd0\xcf\x11\xe0anything", "Save it as .xlsx and try again"),
        ("costs.xlsx", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1encrypted", "password-protected"),
        ("costs.xlsx", b"not a zip", "isn't a valid .xlsx file"),
        ("costs.csv", b"", "empty"),
        ("costs.csv", b"\n , \n", "no rows"),
        ("costs.csv", "Item,Amount\nCafé,1\n".encode("cp1252"), "UTF-8"),
    ],
)
def test_unreadable_files(filename, data, message):
    with pytest.raises(ImportFileError, match=re.escape(message)):
        read_table(filename, data, KNOWN)


# --- match_columns ---

ALIASES = {
    "description": ["Item", "Description"],
    "amount": ["Amount", "Actual", "Cost"],
    "invoice_ref": ["Invoice No", "Invoice #", "Invoice number"],
}


def test_match_columns_uses_aliases_and_reports_ignored_headers():
    match = match_columns(["Item ", "COST", "Invoice-No", "Notes 2", "Internal", ROW_NUMBER], ALIASES)

    assert match.fields == {"description": "Item ", "amount": "COST", "invoice_ref": "Invoice-No"}
    assert match.ignored == ["Notes 2", "Internal"]


def test_match_columns_first_matching_header_wins():
    match = match_columns(["Amount", "Actual"], ALIASES)

    assert match.fields == {"amount": "Amount"}
    assert match.ignored == ["Actual"]


def test_normalise_header():
    assert normalise_header(" Invoice__No ") == normalise_header("invoice-no") == normalise_header("INVOICE  NO") == "invoice no"


# --- parse_amount ---

@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("$1,200.50", Decimal("1200.50")),
        ("£300", Decimal("300")),
        ("1 200", Decimal("1200")),
        ("1 200,50", Decimal("1200.50")),  # non-breaking space thousands, comma decimals
        ("1.200,50", Decimal("1200.50")),
        ("(150.00)", Decimal("-150.00")),
        ("-75", Decimal("-75")),
        ("USD 99.99", Decimal("99.99")),
        ("US$5", Decimal("5")),
        ("A$ 1,000", Decimal("1000")),
        ("250 GBP", Decimal("250")),
        ("12,5", Decimal("12.5")),
        (600, Decimal("600")),
        (12.25, Decimal("12.25")),
    ],
)
def test_parse_amount(text, expected):
    assert parse_amount(text) == expected


@pytest.mark.parametrize("text", ["", ".", "-", "  ", None])
def test_parse_amount_blank(text):
    assert parse_amount(text) is None


@pytest.mark.parametrize("text", ["#N/A", "#DIV/0!", "#VALUE!", "#REF!", "twelve", "12 units", "1-2"])
def test_parse_amount_invalid(text):
    with pytest.raises(InvalidValueError):
        parse_amount(text)


def test_comma_decimal_files():
    column = ["1.200,50", "300,00", "45"]

    assert uses_decimal_comma(column) is True
    assert parse_amount("1,200", decimal_comma=True) == Decimal("1.2")
    assert parse_amount("1.200", decimal_comma=True) == Decimal("1200")
    assert uses_decimal_comma(["1,200.50", "300.00"]) is False
    # Without the file-level hint, 1,200 is twelve hundred
    assert parse_amount("1,200") == Decimal("1200")


# --- parse_date ---

@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-11-04", date(2026, 11, 4)),
        ("2026-11-04 00:00:00", date(2026, 11, 4)),
        ("04/11/2026", date(2026, 11, 4)),  # DD/MM
        ("03/04/2026", date(2026, 4, 3)),  # ambiguous → DD/MM (3 April)
        ("11/25/2026", date(2026, 11, 25)),  # only works as MM/DD
        ("4.11.2026", date(2026, 11, 4)),
        ("04-11-26", date(2026, 11, 4)),
        ("4-Nov-2026", date(2026, 11, 4)),
        ("04 Nov 2026", date(2026, 11, 4)),
        ("4-Nov-26", date(2026, 11, 4)),
        ("4 November 2026", date(2026, 11, 4)),
        ("46330", date(2026, 11, 4)),  # Excel serial number
        (46330, date(2026, 11, 4)),
        (datetime(2026, 11, 4, 9, 30), date(2026, 11, 4)),
        (date(2026, 11, 4), date(2026, 11, 4)),
    ],
)
def test_parse_date(value, expected):
    assert parse_date(value) == expected


@pytest.mark.parametrize("value", ["", None, "  "])
def test_parse_date_blank(value):
    assert parse_date(value) is None


@pytest.mark.parametrize("value", ["31/02/2026", "next Tuesday", "2026-13-01", "13/13/2026", "#N/A"])
def test_parse_date_invalid(value):
    with pytest.raises(InvalidValueError):
        parse_date(value)


# --- parse_currency ---

@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("$", "USD"), ("US$", "USD"), ("£", "GBP"), ("€", "EUR"), ("A$", "AUD"), ("AU$", "AUD"),
        ("C$", "CAD"), ("CA$", "CAD"), ("₩", "KRW"), ("gbp", "GBP"), (" Aud ", "AUD"), ("", "USD"), (None, "USD"),
    ],
)
def test_parse_currency(text, expected):
    assert parse_currency(text, "USD") == expected


@pytest.mark.parametrize("text", ["dollars", "US", "12"])
def test_parse_currency_invalid(text):
    with pytest.raises(InvalidValueError):
        parse_currency(text, "USD")
