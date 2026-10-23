"""Parse human-readable reconciliation intervals."""

import re
from datetime import timedelta


def parse_interval(value: str | int) -> timedelta:
    """Parse whole seconds or unit segments, enforcing a ten-second minimum."""
    message = f"invalid interval {value!r}: expected whole units and at least 10 seconds"
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(message)
    text = str(value).strip()
    if re.fullmatch(r"[0-9]+", text):
        seconds = int(text)
    else:
        units = {
            "s": 1,
            "sec": 1,
            "secs": 1,
            "second": 1,
            "seconds": 1,
            "m": 60,
            "min": 60,
            "mins": 60,
            "minute": 60,
            "minutes": 60,
            "h": 3600,
            "hr": 3600,
            "hrs": 3600,
            "hour": 3600,
            "hours": 3600,
            "d": 86400,
            "day": 86400,
            "days": 86400,
        }
        seen: set[int] = set()
        seconds = 0
        position = 0
        while position < len(text):
            match = re.match(r"([0-9]+)\s*([a-zA-Z]+)\s*", text[position:])
            if match is None:
                raise ValueError(message)
            amount, unit = match.groups()
            multiplier = units.get(unit.lower())
            if multiplier is None or multiplier in seen or int(amount) == 0:
                raise ValueError(message)
            seen.add(multiplier)
            seconds += int(amount) * multiplier
            position += match.end()
    if seconds < 10:
        raise ValueError(message)
    try:
        return timedelta(seconds=seconds)
    except OverflowError as exc:
        raise ValueError(message) from exc
