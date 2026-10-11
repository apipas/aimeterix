"""UTC timestamps (record 0010, D1.7).

Timestamps are timezone-aware datetimes in UTC. SQLite stores them as integer
epoch milliseconds; these helpers convert both ways.
"""

from datetime import datetime, timedelta, timezone

from .errors import ValidationError

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(ts: datetime, name: str = "ts") -> datetime:
    """Return ``ts`` converted to UTC; refuse naive datetimes."""
    if not isinstance(ts, datetime):
        raise ValidationError(f"{name} must be a datetime, got {type(ts).__name__}")
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValidationError(f"{name} must be timezone-aware (UTC)")
    return ts.astimezone(timezone.utc)


def to_epoch_ms(ts: datetime) -> int:
    """Whole milliseconds since the epoch; sub-millisecond precision is dropped."""
    return (ensure_utc(ts) - _EPOCH) // timedelta(milliseconds=1)


def from_epoch_ms(ms: int) -> datetime:
    return _EPOCH + timedelta(milliseconds=ms)
