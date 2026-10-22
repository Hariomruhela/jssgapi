"""Wall-clock parsing and formatting for events.

An event carries a *date* and a *time* the way a person would write them
down: ``25/12/2026`` and ``18:30``. Those are local wall-clock values in the
platform timezone, not instants, so they are stored as a Postgres ``DATE`` and
``TIME`` pair. The instant (``starts_at``) is derived from them, which keeps
sorting, indexing and the "is it in the past" question consistent.

The parsing is deliberately forgiving about *input* (a client may send
``dd/mm/yyyy`` or ISO) and strict about *output* (always ``dd/mm/yyyy`` and
``24-hour HH:MM``), so every client sees the same shape.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.core.constants import EventTimeFrame

#: The number of days an event may start within and still count as upcoming.
UPCOMING_WINDOW_DAYS = 7

DATE_FORMAT = "%d/%m/%Y"
TIME_FORMAT = "%H:%M"

_DATE_FORMATS = ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%y")
_TIME_FORMATS = ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M:%S %p", "%I %p")


def get_app_tz() -> ZoneInfo:
    """The timezone every wall-clock event time is expressed in."""
    return ZoneInfo(get_settings().app_timezone)


def parse_event_date(value: object) -> date:
    """Turn a client-supplied date into a ``date``.

    Accepts a ``date``, a ``datetime`` (its date part is used) or a string in
    ``dd/mm/yyyy``, ``dd-mm-yyyy``, ``yyyy-mm-dd`` or ISO 8601 form.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            raise ValueError("Event date must not be empty")
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
        # An ISO 8601 timestamp carries the date in its leading segment.
        try:
            return datetime.fromisoformat(raw).date()
        except ValueError:
            pass
    raise ValueError(
        f"Invalid event date {value!r}; expected dd/mm/yyyy"
    )


def parse_event_time(value: object) -> time:
    """Turn a client-supplied time into a ``time``.

    Accepts a ``time``, a ``datetime`` (its time part is used) or a string in
    ``HH:MM``, ``HH:MM:SS``, ``hh:mm AM/PM`` or ISO 8601 form.
    """
    if isinstance(value, datetime):
        return value.time()
    if isinstance(value, time):
        return value
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            raise ValueError("Event time must not be empty")
        for fmt in _TIME_FORMATS:
            try:
                return datetime.strptime(raw.upper(), fmt).time()
            except ValueError:
                continue
        try:
            return time.fromisoformat(raw)
        except ValueError:
            pass
    raise ValueError(f"Invalid event time {value!r}; expected HH:MM")


def format_event_date(value: date | None) -> str | None:
    """Render a date the way the API always shows it."""
    return None if value is None else value.strftime(DATE_FORMAT)


def format_event_time(value: time | None) -> str | None:
    """Render a time the way the API always shows it (24-hour, no seconds)."""
    return None if value is None else value.strftime(TIME_FORMAT)


def combine_local(
    event_date: date, event_time: time, tz: ZoneInfo | None = None
) -> datetime:
    """The UTC instant a local wall-clock date and time refer to."""
    zone = tz or get_app_tz()
    local = datetime.combine(event_date, event_time, tzinfo=zone)
    return local.astimezone(UTC)


def local_now(tz: ZoneInfo | None = None) -> datetime:
    """The current wall-clock time in the platform timezone."""
    return datetime.now(tz or get_app_tz())


def classify_time_frame(
    event_date: date,
    event_time: time,
    *,
    now: datetime | None = None,
    tz: ZoneInfo | None = None,
) -> EventTimeFrame:
    """Bucket an event by how far away it is.

    ``PAST`` has already started, ``UPCOMING`` starts within the next seven
    days and ``FUTURE`` starts later than that. The window is measured from the
    current moment, not from midnight, so "in six days" means six days from
    now however late in the day that is.
    """
    zone = tz or get_app_tz()
    current = now or datetime.now(zone)
    if current.tzinfo is None:
        current = current.replace(tzinfo=zone)
    else:
        current = current.astimezone(zone)
    start = datetime.combine(event_date, event_time, tzinfo=zone)
    if start < current:
        return EventTimeFrame.PAST
    if start <= current + timedelta(days=UPCOMING_WINDOW_DAYS):
        return EventTimeFrame.UPCOMING
    return EventTimeFrame.FUTURE


def time_frame_bounds(
    *, now: datetime | None = None, tz: ZoneInfo | None = None
) -> tuple[datetime, datetime]:
    """The UTC instants that bound the past/upcoming/future split.

    Returns ``(now, now + 7 days)``. An event before the first is ``PAST``,
    between the two is ``UPCOMING`` and after the second is ``FUTURE``.
    """
    zone = tz or get_app_tz()
    current = now or datetime.now(zone)
    if current.tzinfo is None:
        current = current.replace(tzinfo=zone)
    else:
        current = current.astimezone(zone)
    upper = current + timedelta(days=UPCOMING_WINDOW_DAYS)
    return current.astimezone(UTC), upper.astimezone(UTC)
