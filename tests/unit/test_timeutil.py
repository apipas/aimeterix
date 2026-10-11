from datetime import datetime, timedelta, timezone

import pytest

from ai_meterix.core.errors import ValidationError
from ai_meterix.core.timeutil import ensure_utc, from_epoch_ms, to_epoch_ms, utc_now


def test_utc_now_is_aware_utc() -> None:
    assert utc_now().utcoffset() == timedelta(0)


def test_naive_datetime_is_refused() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        ensure_utc(datetime(2026, 1, 1))


def test_other_zone_is_converted_to_utc() -> None:
    cet = timezone(timedelta(hours=2))
    ts = ensure_utc(datetime(2026, 10, 11, 12, 0, tzinfo=cet))
    assert ts == datetime(2026, 10, 11, 10, 0, tzinfo=timezone.utc)
    assert ts.tzinfo == timezone.utc


@pytest.mark.parametrize("ms", [0, 1, 999, 1_760_000_000_123, -1, -1_500])
def test_epoch_ms_round_trip(ms: int) -> None:
    assert to_epoch_ms(from_epoch_ms(ms)) == ms


def test_epoch_ms_truncates_sub_millisecond() -> None:
    ts = datetime(2026, 1, 1, 0, 0, 0, 1_999, tzinfo=timezone.utc)
    assert to_epoch_ms(ts) == to_epoch_ms(ts.replace(microsecond=1_000))
