import pytest

from app.domain.backoff import backoff_delay


@pytest.mark.parametrize("attempt, ceiling", [(1, 1.0), (2, 2.0), (3, 4.0), (4, 8.0), (5, 8.0), (10, 8.0)])
def test_ceiling_doubles_then_caps(attempt, ceiling):
    assert backoff_delay(attempt, base=1.0, cap=8.0, rand=lambda: 1.0) == ceiling


def test_full_jitter_scales_the_ceiling():
    assert backoff_delay(3, base=1.0, cap=8.0, rand=lambda: 0.25) == 1.0


def test_retry_after_is_a_floor():
    assert backoff_delay(1, base=1.0, cap=8.0, retry_after=7.0, rand=lambda: 0.5) == 7.0
    assert backoff_delay(4, base=1.0, cap=8.0, retry_after=2.0, rand=lambda: 1.0) == 8.0
