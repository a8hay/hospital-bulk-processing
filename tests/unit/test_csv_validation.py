import pytest

from app.domain.csv_validation import DEFAULT_MAX_ROWS, validate_csv
from app.domain.models import RowInput

HEADER = "name,address,phone"


def csv_bytes(*lines: str) -> bytes:
    return ("\n".join(lines) + "\n").encode()


def messages(result) -> list[str]:
    return [e.message for e in result.errors]


class TestValidFiles:
    def test_parses_rows_with_1_based_row_numbers(self):
        result = validate_csv(csv_bytes(HEADER, "General,1 Main St,555-1234", "City,2 Oak Ave,"))

        assert result.ok
        assert result.rows == [
            RowInput(1, "General", "1 Main St", "555-1234"),
            RowInput(2, "City", "2 Oak Ave", None),
        ]

    def test_accepts_phone_in_any_format(self):
        result = validate_csv(csv_bytes(HEADER, "A,B,+91 (98) 7654-3210 ext. 4"))

        assert result.rows[0].phone == "+91 (98) 7654-3210 ext. 4"

    def test_strips_excel_bom(self):
        result = validate_csv(b"\xef\xbb\xbf" + csv_bytes(HEADER, "A,B,"))

        assert result.ok

    def test_trims_cell_whitespace(self):
        result = validate_csv(csv_bytes(HEADER, "  General ,  1 Main St ,  "))

        assert result.rows == [RowInput(1, "General", "1 Main St", None)]

    def test_quoted_fields_may_contain_commas_and_newlines(self):
        result = validate_csv(csv_bytes(HEADER, '"General, East","1 Main St\nSuite 4",'))

        assert result.rows == [RowInput(1, "General, East", "1 Main St\nSuite 4", None)]

    def test_skips_blank_lines_without_counting_them_as_rows(self):
        result = validate_csv(csv_bytes(HEADER, "", "A,B,", "  ,  ,  ", "C,D,", "", ""))

        assert [(r.row_no, r.name) for r in result.rows] == [(1, "A"), (2, "C")]

    def test_identical_rows_are_allowed(self):
        result = validate_csv(csv_bytes(HEADER, "A,B,", "A,B,"))

        assert [r.row_no for r in result.rows] == [1, 2]

    def test_accepts_exactly_max_rows(self):
        result = validate_csv(csv_bytes(HEADER, *[f"H{i},Addr," for i in range(DEFAULT_MAX_ROWS)]))

        assert len(result.rows) == DEFAULT_MAX_ROWS

    def test_max_rows_is_configurable(self):
        result = validate_csv(csv_bytes(HEADER, "A,B,", "C,D,"), max_rows=1)

        assert messages(result) == ["file has 2 data rows; maximum is 1"]


class TestHeaderContract:
    @pytest.mark.parametrize(
        "header",
        [
            "name,address",  # phone column is required even though its values are optional
            "address,name,phone",  # order matters
            "Name,Address,Phone",  # case matters
            "name, address, phone",  # no surrounding whitespace
            "name,address,phone,email",  # no extra columns
        ],
    )
    def test_rejects_anything_but_the_exact_header(self, header):
        result = validate_csv(csv_bytes(header, "A,B,C"))

        assert messages(result) == [f"header must be exactly 'name,address,phone', got '{header}'"]
        assert result.errors[0].line == 1

    def test_header_line_accounts_for_leading_blank_lines(self):
        result = validate_csv(csv_bytes("", "", "bad,header"))

        assert result.errors[0].line == 3


class TestFileLevelErrors:
    @pytest.mark.parametrize(
        "data, expected",
        [
            (b"", "file is empty"),
            (b"\n\n  \n", "file is empty"),
            (csv_bytes(HEADER), "file has a header but no data rows"),
            (b"\xff\xfe\x00n\x00a", "file is not valid UTF-8"),
        ],
    )
    def test_rejects(self, data, expected):
        assert messages(validate_csv(data)) == [expected]

    def test_rejects_more_than_max_rows(self):
        result = validate_csv(csv_bytes(HEADER, *[f"H{i},Addr," for i in range(DEFAULT_MAX_ROWS + 1)]))

        assert messages(result) == [f"file has {DEFAULT_MAX_ROWS + 1} data rows; maximum is {DEFAULT_MAX_ROWS}"]

    def test_rejects_unterminated_quote(self):
        result = validate_csv(csv_bytes(HEADER, '"General,1 Main St,'))

        assert messages(result)[0].startswith("malformed CSV")


class TestRowLevelErrors:
    def test_collects_every_error_with_row_and_line_and_returns_no_rows(self):
        result = validate_csv(
            csv_bytes(
                HEADER,  # line 1
                ",1 Main St,",  # line 2, row 1: missing name
                "",  # line 3, blank
                "Ok,Fine,",  # line 4, row 2: valid
                '"Multi","line\naddress"',  # lines 5-6, row 3: 2 columns
                "City,,",  # line 7, row 4: missing address
                ",,555",  # line 8, row 5: missing both
            )
        )

        assert result.rows == []
        assert [(e.row, e.line, e.column, e.message) for e in result.errors] == [
            (1, 2, "name", "must not be empty"),
            (3, 5, None, "expected 3 columns, got 2"),
            (4, 7, "address", "must not be empty"),
            (5, 8, "name", "must not be empty"),
            (5, 8, "address", "must not be empty"),
        ]
