from datetime import timedelta

import pytest

from cloudflare_dns_autoupdate.config.interval import parse_interval


@pytest.mark.parametrize(
    "value,seconds",
    [
        (300, 300),
        ("300", 300),
        ("  300  ", 300),
        ("1h30m", 5400),
        ("1d 12h", 129600),
        ("90 minutes", 5400),
        ("  2H 3M 4S  ", 7384),
        ("10s", 10),
    ],
)
def test_intervals(value, seconds):
    assert parse_interval(value) == timedelta(seconds=seconds)


@pytest.mark.parametrize(
    "units,multiplier",
    [
        (["s", "sec", "secs", "second", "seconds"], 1),
        (["m", "min", "mins", "minute", "minutes"], 60),
        (["h", "hr", "hrs", "hour", "hours"], 3600),
        (["d", "day", "days"], 86400),
    ],
)
def test_all_spellings(units, multiplier):
    for unit in units:
        assert parse_interval(f"10 {unit.upper()}").total_seconds() == 10 * multiplier


@pytest.mark.parametrize(
    "value",
    [
        0,
        -1,
        9,
        True,
        1.5,
        "",
        "0",
        "-1h",
        "1.5h",
        "1h 2hrs",
        "3m5minutes",
        "1w",
        "1h junk",
        "10s!",
        "1h0s",
        "s10",
        "1 0s",
        "999999999999999999999999999d",
    ],
)
def test_rejected(value):
    with pytest.raises(ValueError, match="invalid interval"):
        parse_interval(value)
