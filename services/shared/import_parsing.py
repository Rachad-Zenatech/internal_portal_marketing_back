"""Reading spreadsheet uploads (CSV and .xlsx) into rows, and parsing the values people type into them.

Pure functions, no database: every upload (cost sheets, events, ads exports) uses these, then applies its
own rules. Problems with the file as a whole raise ImportFileError; problems with one value raise
InvalidValueError, so the preview can flag that cell and keep going.
"""
import csv
import io
import re
import zipfile
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Iterable, NamedTuple

MAX_ROWS = 5000
# Each row also carries its original sheet row number under this key, so the preview can point at it
ROW_NUMBER = "_row"
# How far down to look for the header row (title rows often sit above it)
_HEADER_SEARCH_ROWS = 20

# Encrypted .xlsx files (and old .xls files) are OLE compound documents, not zip files
_OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


class ImportFileError(Exception):
    """The file can't be read at all; the message is safe to show to users."""


class InvalidValueError(ValueError):
    """One value can't be understood; the message is safe to show to users."""


# --- Reading the file ---

def normalise_header(text: object) -> str:
    """'Invoice_No ', 'invoice-no' and 'Invoice  No' all become 'invoice no'."""
    return re.sub(r"[\s_\-]+", " ", str(text or "")).strip().lower()


def _cell_text(value: object) -> str:
    """A cell as text: dates as ISO, whole numbers without '.0', blanks as ''."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat() if value.time() == datetime.min.time() else value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _csv_rows(data: bytes) -> list[list[str]]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ImportFileError("The CSV isn't saved as UTF-8. In Excel, use Save As → CSV UTF-8 and try again.") from exc
    sample = "\n".join(text.splitlines()[:20])
    # Comma or semicolon (Excel uses semicolons where the comma is the decimal separator)
    delimiter = ";" if sample.count(";") > sample.count(",") else ","
    return [[cell.strip() for cell in row] for row in csv.reader(io.StringIO(text), delimiter=delimiter)]


def _has_header(sheet, known: set[str]) -> bool:
    """Whether one of the sheet's first rows names at least two of the known columns."""
    for row in sheet.iter_rows(max_row=_HEADER_SEARCH_ROWS, values_only=True):
        if sum(normalise_header(cell) in known for cell in row if cell is not None) >= 2:
            return True
    return False


def _xlsx_rows(data: bytes, sheet_names: Iterable[str] | None = None, known: set[str] | None = None) -> list[list[str]]:
    from openpyxl import load_workbook  # only needed for .xlsx uploads

    try:
        # data_only: the values Excel last calculated, never the formulas themselves
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except (zipfile.BadZipFile, KeyError, OSError, ValueError) as exc:
        raise ImportFileError("This isn't a valid .xlsx file. Open it in Excel, save it as .xlsx and try again.") from exc
    try:
        visible = [ws for ws in workbook.worksheets if ws.sheet_state == "visible"]
        if not visible:
            raise ImportFileError("The workbook has no visible sheets.")
        sheet = visible[0]
        if sheet_names is not None:
            # The sheet with one of these names, else the first whose header matches, else the first one
            wanted = {normalise_header(name) for name in sheet_names}
            named = next((ws for ws in visible if normalise_header(ws.title) in wanted), None)
            if named is not None:
                sheet = named
            elif known:
                sheet = next((ws for ws in visible if _has_header(ws, known)), sheet)
        rows = [[_cell_text(value) for value in row] for row in sheet.iter_rows(values_only=True)]
    finally:
        workbook.close()
    return rows


def read_table(
    filename: str, data: bytes, known_columns: Iterable[str] | None = None, sheet_names: Iterable[str] | None = None,
) -> list[dict[str, str | int]]:
    """Read a CSV or .xlsx upload into one dict per row, keyed by the sheet's own header text.

    - .csv: UTF-8 with or without a BOM; comma or semicolon separated.
    - .xlsx: the first visible sheet; calculated values, not formulas. With `sheet_names`, the visible sheet with
      one of those names, else the first whose header has at least two of `known_columns`.
    - The header is the first row with at least two of `known_columns` (compared after normalise_header),
      so title rows above it are skipped. Without `known_columns`, it's the first non-blank row.
    - Fully blank rows are skipped. Each row keeps its sheet row number under ROW_NUMBER.
    - Cells are text: dates become YYYY-MM-DD and whole numbers lose their '.0', ready for parse_date /
      parse_amount.
    """
    name = (filename or "").lower()
    if not data:
        raise ImportFileError("The file is empty.")
    if name.endswith(".xls"):
        raise ImportFileError("This is an old Excel file (.xls). Save it as .xlsx and try again.")
    known = {normalise_header(c) for c in known_columns} if known_columns is not None else None
    if name.endswith(".xlsx"):
        if data.startswith(_OLE_SIGNATURE):
            raise ImportFileError("This file is password-protected. Remove the password in Excel and try again.")
        raw_rows = _xlsx_rows(data, sheet_names, known)
    elif name.endswith(".csv"):
        raw_rows = _csv_rows(data)
    else:
        raise ImportFileError("Upload an Excel (.xlsx) or CSV file.")

    numbered = [(i + 1, row) for i, row in enumerate(raw_rows) if any(cell for cell in row)]
    if not numbered:
        raise ImportFileError("The file has no rows.")

    header_index = None
    for index, (_, row) in enumerate(numbered[:_HEADER_SEARCH_ROWS]):
        if known is None or sum(normalise_header(cell) in known for cell in row) >= 2:
            header_index = index
            break
    if header_index is None:
        raise ImportFileError("Couldn't find the header row. Check the column names match the template.")

    # Header text by column position; blank header cells are dropped, repeated names get " (2)", " (3)"
    headers: dict[int, str] = {}
    seen: dict[str, int] = {}
    for position, cell in enumerate(numbered[header_index][1]):
        if not cell:
            continue
        key = normalise_header(cell)
        seen[key] = seen.get(key, 0) + 1
        headers[position] = cell if seen[key] == 1 else f"{cell} ({seen[key]})"

    data_rows = numbered[header_index + 1:]
    if len(data_rows) > MAX_ROWS:
        raise ImportFileError(f"The file has {len(data_rows):,} rows, more than the {MAX_ROWS:,} limit. Split it into smaller files.")

    table = []
    for row_number, row in data_rows:
        record: dict[str, str | int] = {header: (row[position] if position < len(row) else "") for position, header in headers.items()}
        if not any(record.values()):
            continue  # only filled in under columns we don't read
        record[ROW_NUMBER] = row_number
        table.append(record)
    return table


class ColumnMatch(NamedTuple):
    """Which sheet header feeds each of our fields, and the headers nothing uses."""

    fields: dict[str, str]
    ignored: list[str]


def match_columns(headers: Iterable[str], aliases: dict[str, list[str]]) -> ColumnMatch:
    """Map sheet headers to our fields. A field matches its own name or any alias (normalised).

    `ignored` lists the headers no field uses, for "Ignored columns: Notes 2, Internal" in the preview.
    """
    lookup: dict[str, str] = {}
    for field, names in aliases.items():
        for name in [field, *names]:
            lookup.setdefault(normalise_header(name), field)

    fields: dict[str, str] = {}
    ignored: list[str] = []
    for header in headers:
        if header == ROW_NUMBER:
            continue
        field = lookup.get(normalise_header(header))
        if field and field not in fields:
            fields[field] = header
        else:
            ignored.append(header)
    return ColumnMatch(fields, ignored)


# --- Values ---

_EXCEL_ERRORS = {"#N/A", "#DIV/0!", "#VALUE!", "#REF!", "#NAME?", "#NUM!", "#NULL!"}
_BLANK_AMOUNTS = {"", ".", "-", "–", "—"}


def uses_decimal_comma(values: Iterable[object]) -> bool:
    """True when a column clearly writes decimals with a comma (1.200,50 or 12,5) and never with a dot."""
    comma = dot = False
    for value in values:
        text = str(value or "").strip()
        if re.search(r"\d,\d{1,2}\)?$", text) or re.search(r"\d\.\d{3},\d", text):
            comma = True
        elif re.search(r"\d\.\d{1,2}\)?$", text):
            dot = True
    return comma and not dot


def parse_amount(value: object, decimal_comma: bool = False) -> Decimal | None:
    """An amount as typed: '$1,200.50' → 1200.50, '£300', '1 200', '(150.00)' → -150.

    Blank, '.' and '-' mean no amount (None). Excel errors (#N/A, #DIV/0! …) and text raise InvalidValueError.
    With decimal_comma (see uses_decimal_comma), '1.200,50' → 1200.50 and '1,200' → 1.2.
    """
    if value is None:
        return None
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return Decimal(str(value))
    text = str(value).strip()
    if text.upper() in _EXCEL_ERRORS:
        raise InvalidValueError(f"{text} is a spreadsheet error, not an amount")
    if text in _BLANK_AMOUNTS:
        return None

    # Drop a currency code or prefix before or after the number (USD 1200, US$1200, A$ 50, 1200 GBP),
    # currency symbols, and thousands spaces / apostrophes (incl. non-breaking and thin spaces)
    text = re.sub(r"^[A-Za-z]{1,3}(?=\s*[$£€₩¥\d(\-])", "", text)
    text = re.sub(r"(?<=[\d)])\s*[A-Za-z]{3}$", "", text)
    text = re.sub(r"[$£€₩¥\s  ']", "", text)

    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative, text = True, text[1:-1]
    if text.startswith("-"):
        negative, text = not negative, text[1:]
    text = text.lstrip("+")
    if not re.fullmatch(r"[\d.,]*\d[\d.,]*", text):
        raise InvalidValueError(f"'{value}' isn't an amount")

    decimal_mark = "," if decimal_comma else "."
    if "," in text and "." in text:
        decimal_mark = "," if text.rfind(",") > text.rfind(".") else "."  # whichever comes last is the decimal
    elif not decimal_comma and re.fullmatch(r"\d+,\d{1,2}", text):
        decimal_mark = ","  # 12,5 can't be a thousands separator
    elif decimal_comma and re.fullmatch(r"\d{1,3}(\.\d{3})+", text):
        decimal_mark = ","  # 1.200 in a comma-decimal file is twelve hundred
    thousands = "." if decimal_mark == "," else ","
    text = text.replace(thousands, "").replace(decimal_mark, ".")

    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise InvalidValueError(f"'{value}' isn't an amount") from exc
    return -amount if negative else amount


# Excel counts days from 30 Dec 1899 (its 1900 leap-year bug makes this line up for modern dates)
_EXCEL_EPOCH = date(1899, 12, 30)
_DATE_FORMATS = ("%Y-%m-%d", "%d-%b-%Y", "%d %b %Y", "%d-%b-%y", "%d %b %y", "%d %B %Y", "%d-%B-%Y")


def parse_date(value: object) -> date | None:
    """A date as typed or stored in a cell. Blank → None; anything unreadable raises InvalidValueError.

    Accepts date / datetime cells, Excel serial numbers (e.g. 46330), ISO (2026-11-04, with or without a
    time), DD/MM/YYYY (also with - or .), and DD-Mon-YYYY / DD Mon YY (4-Nov-2026, 04 Nov 26).

    Ambiguous day/month dates such as 03/04/2026 are read as DD/MM (3 April), the UK and Australian way.
    A date that only works one way round, like 11/25/2026, is read as MM/DD.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None

    if re.fullmatch(r"\d+(\.\d+)?", text):
        serial = float(text)
        if 1 <= serial < 100000:
            return _EXCEL_EPOCH + timedelta(days=int(serial))
        raise InvalidValueError(f"'{text}' isn't a date")

    iso = re.fullmatch(r"(\d{4}-\d{2}-\d{2})(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?", text)
    if iso:
        text = iso.group(1)

    numeric = re.fullmatch(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2}|\d{4})", text)
    if numeric:
        first, second, year = (int(part) for part in numeric.groups())
        if year < 100:
            year += 2000
        day, month = (second, first) if second > 12 >= first else (first, second)
        try:
            return date(year, month, day)
        except ValueError as exc:
            raise InvalidValueError(f"'{value}' isn't a date") from exc

    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise InvalidValueError(f"'{value}' isn't a date. Use DD/MM/YYYY or YYYY-MM-DD.")


# Longest first, so "US$" wins over "$"
_CURRENCY_SYMBOLS = {
    "US$": "USD", "AU$": "AUD", "CA$": "CAD", "A$": "AUD", "C$": "CAD",
    "$": "USD", "£": "GBP", "€": "EUR", "₩": "KRW",
}


def parse_currency(value: object, default: str) -> str:
    """A 3-letter currency code from a code (any case) or a symbol: $ → USD, £ → GBP, A$ → AUD, ₩ → KRW.

    Blank → `default`. Anything else raises InvalidValueError.
    """
    text = str(value or "").strip()
    if not text:
        return default
    upper = text.upper()
    if upper in _CURRENCY_SYMBOLS:
        return _CURRENCY_SYMBOLS[upper]
    if re.fullmatch(r"[A-Z]{3}", upper):
        return upper
    raise InvalidValueError(f"'{text}' isn't a currency. Use a code like USD or GBP.")
