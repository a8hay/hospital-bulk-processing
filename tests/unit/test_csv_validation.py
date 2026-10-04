import pytest

from app.domain.csv_validation import MAX_BYTES, MAX_ROWS, validate_csv
from app.domain.models import RowInput


def csv_bytes(*lines: str) -> bytes:
    return ("\n".join(lines) + "\n").encode()


def messages(result) -> list[str]:
    return [e.message for e in result.errors]


class TestValidFiles:
    def test_parses_rows_with_1_based_row_numbers(self):
        result = validate_csv(csv_bytes("name,address,phone", "General,1 Main St,555-1234", "City,2 Oak Ave,"))

        assert result.ok
        assert result.rows == [
            RowInput(1, "General", "1 Main St", "555-1234"),
            RowInput(2, "City", "2 Oak Ave", None),
        ]

    def test_phone_column_may_be_absent(self):
        result = validate_csv(csv_bytes("name,address", "General,1 Main St"))

        assert result.rows == [RowInput(1, "General", "1 Main St", None)]

    def test_accepts_phone_in_any_format(self):
        result = validate_csv(csv_bytes("name,address,phone", "A,B,+91 (98) 7654-3210 ext. 4"))

        assert result.rows[0].phone == "+91 (98) 7654-3210 ext. 4"

    def test_strips_excel_bom(self):
        result = validate_csv(b"\xef\xbb\xbf" + csv_bytes("name,address", "A,B"))

        assert result.ok

    def test_header_is_case_and_whitespace_insensitive_and_order_free(self):
        result = validate_csv(csv_bytes(" Address , NAME ", "1 Main St,General"))

        assert result.rows == [RowInput(1, "General", "1 Main St", None)]

    def test_trims_cell_whitespace(self):
        result = validate_csv(csv_bytes("name,address,phone", "  General ,  1 Main St ,  "))

        assert result.rows == [RowInput(1, "General", "1 Main St", None)]

    def test_quoted_fields_may_contain_commas_and_newlines(self):
        result = validate_csv(csv_bytes("name,address", '"General, East","1 Main St\nSuite 4"'))

        assert result.rows == [RowInput(1, "General, East", "1 Main St\nSuite 4", None)]

    def test_skips_blank_lines(self):
        result = validate_csv(csv_bytes("name,address", "", "A,B", "  ,  ", "C,D"))

        assert [r.name for r in result.rows] == ["A", "C"]
        assert [r.row_no for r in result.rows] == [1, 2]

    def test_identical_rows_are_allowed(self):
        result = validate_csv(csv_bytes("name,address", "A,B", "A,B"))

        assert [r.row_no for r in result.rows] == [1, 2]

    def test_accepts_exactly_max_rows(self):
        result = validate_csv(csv_bytes("name,address", *[f"H{i},Addr" for i in range(MAX_ROWS)]))

        assert len(result.rows) == MAX_ROWS


class TestFileLevelErrors:
    @pytest.mark.parametrize(
        "data, expected",
        [
            (b"", "file is empty"),
            (b"\n\n  \n", "file is empty"),
            (csv_bytes("name,address"), "file has a header but no data rows"),
            (b"\xff\xfe\x00n\x00a", "file is not valid UTF-8"),
            (b"x" * (MAX_BYTES + 1), f"file exceeds {MAX_BYTES} bytes"),
        ],
    )
    def test_rejects(self, data, expected):
        assert messages(validate_csv(data)) == [expected]

    def test_rejects_more_than_max_rows(self):
        result = validate_csv(csv_bytes("name,address", *[f"H{i},Addr" for i in range(MAX_ROWS + 1)]))

        assert messages(result) == [f"file has {MAX_ROWS + 1} data rows; maximum is {MAX_ROWS}"]

    def test_rejects_unterminated_quote(self):
        result = validate_csv(csv_bytes("name,address", '"General,1 Main St'))

        assert messages(result)[0].startswith("malformed CSV")

    def test_reports_all_header_problems_at_once(self):
        result = validate_csv(csv_bytes("name,name,email", "A,B,C"))

        assert set(messages(result)) == {
            "missing required column 'address'",
            "unknown column 'email'",
            "duplicate column 'name'",
        }


class TestRowLevelErrors:
    def test_collects_errors_across_rows_and_returns_no_rows(self):
        result = validate_csv(csv_bytes("name,address,phone", ",1 Main St,", "Ok,Fine,", "Short,row", "City,,"))

        assert result.rows == []
        assert [(e.row, e.column, e.message) for e in result.errors] == [
            (1, "name", "must not be empty"),
            (3, None, "expected 3 columns, got 2"),
            (4, "address", "must not be empty"),
        ]
