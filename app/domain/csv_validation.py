"""Turn an uploaded CSV into validated rows, or a complete list of what is wrong with it.

Validation is all-or-nothing: if any error is found, no rows are returned, so a batch is
never created from a partially valid file. Errors are collected across the whole file
rather than stopping at the first one, so the caller can fix everything in one pass.
"""

import csv
import io
from dataclasses import dataclass, field

from app.domain.models import RowInput

MAX_ROWS = 20  # spec limit; upstream also caps a batch at 20 but enforces it racily
MAX_BYTES = 1_000_000
REQUIRED_COLUMNS = ("name", "address")
OPTIONAL_COLUMNS = ("phone",)
KNOWN_COLUMNS = REQUIRED_COLUMNS + OPTIONAL_COLUMNS


@dataclass(frozen=True)
class ValidationError:
    message: str
    row: int | None = None  # None means the error is about the file, not a specific row
    column: str | None = None


@dataclass(frozen=True)
class ValidationResult:
    rows: list[RowInput] = field(default_factory=list)
    errors: list[ValidationError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _fail(message: str) -> ValidationResult:
    return ValidationResult(errors=[ValidationError(message)])


def validate_csv(data: bytes) -> ValidationResult:
    if len(data) > MAX_BYTES:
        return _fail(f"file exceeds {MAX_BYTES} bytes")

    try:
        # utf-8-sig strips the BOM that Excel prepends when saving as CSV
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return _fail("file is not valid UTF-8")

    try:
        records = [
            record
            for record in csv.reader(io.StringIO(text, newline=""), strict=True)
            if any(cell.strip() for cell in record)
        ]
    except csv.Error as exc:
        return _fail(f"malformed CSV: {exc}")

    if not records:
        return _fail("file is empty")

    header = [cell.strip().lower() for cell in records[0]]
    header_errors = [
        ValidationError(f"missing required column '{col}'") for col in REQUIRED_COLUMNS if col not in header
    ]
    header_errors += [
        ValidationError(f"unknown column '{col}'", column=col) for col in header if col not in KNOWN_COLUMNS
    ]
    header_errors += [
        ValidationError(f"duplicate column '{col}'", column=col)
        for col in sorted(set(header))
        if header.count(col) > 1
    ]
    if header_errors:
        return ValidationResult(errors=header_errors)

    data_rows = records[1:]
    if not data_rows:
        return _fail("file has a header but no data rows")
    if len(data_rows) > MAX_ROWS:
        return _fail(f"file has {len(data_rows)} data rows; maximum is {MAX_ROWS}")

    column_index = {col: i for i, col in enumerate(header)}
    rows: list[RowInput] = []
    errors: list[ValidationError] = []

    for row_no, record in enumerate(data_rows, start=1):
        if len(record) != len(header):
            errors.append(ValidationError(f"expected {len(header)} columns, got {len(record)}", row=row_no))
            continue

        values = {col: record[i].strip() for col, i in column_index.items()}
        missing = [col for col in REQUIRED_COLUMNS if not values[col]]
        errors += [ValidationError("must not be empty", row=row_no, column=col) for col in missing]
        if missing:
            continue

        rows.append(
            RowInput(
                row_no=row_no,
                name=values["name"],
                address=values["address"],
                phone=values.get("phone") or None,
            )
        )

    if errors:
        return ValidationResult(errors=errors)
    return ValidationResult(rows=rows)
