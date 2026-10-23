import pytest
from conftest import record_data

from cloudflare_dns_autoupdate.config.models import RecordConfig
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


@pytest.mark.parametrize("source_kind", ["ip", "shell", "eval"])
@pytest.mark.parametrize(
    "kind,value",
    [
        ("A", "10.0.0.1"),
        ("A", "172.16.0.1"),
        ("A", "192.168.100.192"),
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
async def test_non_global_addresses_from_all_sources(zone, source_kind, kind, value):
    source = {"ip": value, "shell": f"printf '%s' '{value}'", "eval": repr(value)}[source_kind]
    record = RecordConfig.model_validate(record_data(type=kind, content={source_kind: source}))
    assert await resolve_content(record, zone) == value
