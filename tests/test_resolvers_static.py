import pytest

from cloudflare_dns_autoupdate.resolvers import resolve_content, validate_content


async def test_static(record, zone):
    assert await resolve_content(record, zone) == "203.0.113.10"


@pytest.mark.parametrize(
    "kind,value,expected",
    [
        ("A", "8.8.8.8", "8.8.8.8"),
        ("AAAA", "2606:4700:4700:0:0:0:0:1111", "2606:4700:4700::1111"),
        ("CNAME", "Target.Example.Com.", "target.example.com"),
        ("NS", "_dns.example.com", "_dns.example.com"),
        ("PTR", "host-name.example.com", "host-name.example.com"),
        ("TXT", " some text ", " some text "),
    ],
)
def test_valid(kind, value, expected):
    assert validate_content(kind, value) == expected


@pytest.mark.parametrize(
    "kind,value",
    [
        ("A", "::1"),
        ("AAAA", "127.0.0.1"),
        ("A", "001.2.3.4"),
        ("CNAME", "-a.example.com"),
        ("NS", "bad-.com"),
        ("PTR", "a..com"),
        ("CNAME", "a" * 64 + ".com"),
        ("NS", "a." * 128),
        ("TXT", ""),
        ("TXT", "x" * 2049),
        ("MX", "hello"),
    ],
)
def test_invalid(kind, value):
    with pytest.raises(ValueError):
        validate_content(kind, value)


@pytest.mark.parametrize(
    "kind,value",
    [
        ("A", "10.0.0.1"),
        ("A", "127.0.0.1"),
        ("A", "169.254.1.1"),
        ("A", "0.0.0.0"),
        ("A", "224.0.0.1"),
        ("A", "203.0.113.10"),
        ("AAAA", "::1"),
        ("AAAA", "fe80::1"),
        ("AAAA", "fc00::1"),
        ("AAAA", "ff02::1"),
    ],
)
def test_private_only_static(kind, value):
    assert validate_content(kind, value)
    with pytest.raises(ValueError, match="globally routable"):
        validate_content(kind, value, dynamic=True)
