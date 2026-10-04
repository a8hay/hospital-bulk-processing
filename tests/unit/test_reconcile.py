from dataclasses import dataclass

from app.domain.models import RowRecord, RowStatus
from app.domain.reconcile import ActivationState, activation_state, match_unknown_rows


@dataclass
class Hospital:
    id: int
    name: str
    address: str
    phone: str | None = None
    active: bool = False


def unknown_row(row_no: int, name: str, address: str = "Addr", phone: str | None = None) -> RowRecord:
    return RowRecord(row_no, name, address, phone, RowStatus.UNKNOWN, None, 1, "ReadTimeout")


class TestMatchUnknownRows:
    def test_adopts_matching_hospital_and_reports_missing_ones(self):
        matches = match_unknown_rows(
            [unknown_row(1, "A"), unknown_row(2, "B")],
            [Hospital(10, "A", "Addr")],
            owned_ids=set(),
        )

        assert matches == {1: 10, 2: None}

    def test_matches_on_all_three_fields_exactly(self):
        upstream = [Hospital(10, "A", "Addr", "555"), Hospital(11, "a", "Addr"), Hospital(12, "A", "Other")]

        assert match_unknown_rows([unknown_row(1, "A", "Addr", None)], upstream, set()) == {1: None}

    def test_ignores_hospitals_already_owned_by_other_rows(self):
        # two identical CSV rows: row 1 was created as id 10, row 2's POST timed out
        upstream = [Hospital(10, "A", "Addr"), Hospital(11, "A", "Addr")]

        assert match_unknown_rows([unknown_row(2, "A")], upstream, owned_ids={10}) == {2: 11}

    def test_identical_unknown_rows_each_adopt_a_different_hospital(self):
        upstream = [Hospital(11, "A", "Addr")]

        # only one copy exists upstream, so exactly one of the two rows was committed
        assert match_unknown_rows([unknown_row(1, "A"), unknown_row(2, "A")], upstream, set()) == {1: 11, 2: None}


class TestActivationState:
    def test_all_active(self):
        assert activation_state({1, 2}, [Hospital(1, "A", "B", active=True), Hospital(2, "A", "B", active=True)]) is (
            ActivationState.ACTIVE
        )

    def test_none_active(self):
        assert activation_state({1}, [Hospital(1, "A", "B")]) is ActivationState.INACTIVE

    def test_mixed(self):
        assert activation_state({1, 2}, [Hospital(1, "A", "B", active=True), Hospital(2, "A", "B")]) is (
            ActivationState.MIXED
        )

    def test_missing_hospitals_win_over_everything(self):
        assert activation_state({1, 2}, [Hospital(1, "A", "B", active=True)]) is ActivationState.MISSING
