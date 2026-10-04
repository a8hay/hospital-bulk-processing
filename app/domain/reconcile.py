"""Work out what actually happened upstream when our own records can't tell us.

Pure functions over what the upstream reports for a batch; no I/O.
"""

from collections import defaultdict
from collections.abc import Iterable, Sequence
from enum import Enum
from typing import Protocol

from app.domain.models import RowRecord


class UpstreamHospitalLike(Protocol):
    id: int
    name: str
    address: str
    phone: str | None
    active: bool


def match_unknown_rows(
    unknown_rows: Sequence[RowRecord], upstream: Iterable[UpstreamHospitalLike], owned_ids: set[int]
) -> dict[int, int | None]:
    """Map each unknown row to the upstream hospital it created, or None if it created nothing.

    Matching is by exact (name, address, phone): we sent those exact values, so no normalisation
    is needed. Rows with identical content are allowed, so matching is done as a multiset: ids
    already owned by other rows are excluded, and each candidate is adopted by at most one row.
    Identical rows are interchangeable, so which of them gets which id does not matter.
    """
    candidates: dict[tuple[str, str, str | None], list[int]] = defaultdict(list)
    for hospital in sorted(upstream, key=lambda h: h.id):
        if hospital.id not in owned_ids:
            candidates[(hospital.name, hospital.address, hospital.phone)].append(hospital.id)

    matches: dict[int, int | None] = {}
    for row in sorted(unknown_rows, key=lambda r: r.row_no):
        ids = candidates.get((row.name, row.address, row.phone))
        matches[row.row_no] = ids.pop(0) if ids else None
    return matches


class ActivationState(Enum):
    ACTIVE = "active"  # every hospital in the upstream batch is active
    INACTIVE = "inactive"  # none are active; activation has not happened
    MIXED = "mixed"  # some active, some not; upstream offers no way to fix this
    MISSING = "missing"  # upstream no longer has hospitals we created (e.g. it restarted and lost data)


def activation_state(owned_ids: set[int], upstream: Sequence[UpstreamHospitalLike]) -> ActivationState:
    if owned_ids - {h.id for h in upstream}:
        return ActivationState.MISSING
    active = [h.active for h in upstream]
    if all(active):
        return ActivationState.ACTIVE
    if not any(active):
        return ActivationState.INACTIVE
    return ActivationState.MIXED
