"""Turn an uploaded CSV into validated rows, or a complete list of what is wrong with it.

Contract: the header must be exactly `name,address,phone`. `phone` values may be empty.

Validation is all-or-nothing: if any error is found, no rows are returned, so a batch is
never created from a partially valid file. Errors are collected across the whole file so
the client can fix everything in one pass. Blank lines are skipped.

The upload size limit is enforced by the caller with a bounded read, before this runs.
"""

import csv
import io
from dataclasses import dataclass, field

from app.domain.models import RowInput

EXPECTED_HEADER = ["name", "address", "phone"]
DEFAULT_MAX_ROWS = 20  # spec limit; upstream also caps a batch at 20 but enforces it racily


@dataclass(frozen=True)
class ValidationError:
    message: str
    row: int | None = None  # data-row index, matching `row` in the batch response
    line: int | None = None  # physical line where the record starts, for finding it in an editor
    column: str | None = None


@dataclass(frozen=True)
class ValidationResult:
    rows: list[RowInput] = field(default_factory=list)
    errors: list[ValidationError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _fail(message: str, line: int | None = None) -> ValidationResult:
    return ValidationResult(errors=[ValidationError(message, line=line)])


def _read_records(text: str) -> list[tuple[int, list[str]]]:
    """Return (start line, cells) for every non-blank record."""
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    records = []
    start_line = 1
    for record in reader:
        if any(cell.strip() for cell in record):
            records.append((start_line, record))
        # a quoted field may span several lines, so the next record starts after the last one read
        start_line = reader.line_num + 1
    return records


def validate_csv(data: bytes, max_rows: int = DEFAULT_MAX_ROWS) -> ValidationResult:
    try:
        # utf-8-sig strips the BOM that Excel prepends when saving as CSV
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return _fail("file is not valid UTF-8")

    try:
        records = _read_records(text)
    except csv.Error as exc:
        return _fail(f"malformed CSV: {exc}")

    if not records:
        return _fail("file is empty")

    header_line, header = records[0]
    if header != EXPECTED_HEADER:
        return _fail(f"header must be exactly '{','.join(EXPECTED_HEADER)}', got '{','.join(header)}'", header_line)

    data_records = records[1:]
    if not data_records:
        return _fail("file has a header but no data rows")
    if len(data_records) > max_rows:
        return _fail(f"file has {len(data_records)} data rows; maximum is {max_rows}")

    rows: list[RowInput] = []
    errors: list[ValidationError] = []

    for row_no, (line, record) in enumerate(data_records, start=1):
        if len(record) != len(EXPECTED_HEADER):
            errors.append(
                ValidationError(f"expected {len(EXPECTED_HEADER)} columns, got {len(record)}", row=row_no, line=line)
            )
            continue

        name, address, phone = (cell.strip() for cell in record)
        missing = [col for col, value in (("name", name), ("address", address)) if not value]
        errors += [ValidationError("must not be empty", row=row_no, line=line, column=col) for col in missing]
        if not missing:
            rows.append(RowInput(row_no=row_no, name=name, address=address, phone=phone or None))

    if errors:
        return ValidationResult(errors=errors)
    return ValidationResult(rows=rows)
