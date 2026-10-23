import pytest
from conftest import config_data, record_data
from pydantic import ValidationError

from cloudflare_dns_autoupdate.config.models import AppConfig, ContentConfig, RecordConfig


@pytest.mark.parametrize("content", [{}, {"ip": "x", "shell": "y"}, {"eval": None}])
def test_exactly_one(content):
    with pytest.raises(ValidationError, match="exactly one"):
        ContentConfig.model_validate(content)


def test_normalization_and_wildcards():
    config = AppConfig.model_validate(
        config_data(
            zones=[
                {
                    "domain": "EXAMPLE.COM.",
                    "dns": [record_data(name="*.Example.Com.", type="a")],
                }
            ],
            interval="1H30m",
        )
    )
    assert config.zones[0].domain == "example.com"
    assert config.zones[0].dns[0].name == "*.example.com"
    assert config.zones[0].dns[0].type == "A"
    assert config.interval_original == "1H30m"
    assert config.interval.total_seconds() == 5400
    assert "test-secret-token" not in repr(config)
    with pytest.raises(ValidationError, match="frozen"):
        config.zones = []


@pytest.mark.parametrize("ttl,expected", [("Auto", 1), ("aUtO", 1), (30, 30), (86400, 86400)])
def test_ttl(ttl, expected):
    assert RecordConfig.model_validate(record_data(ttl=ttl)).ttl == expected


@pytest.mark.parametrize("ttl", [1, 29, 86401, 300.0, "300", True, None])
def test_bad_ttl(ttl):
    with pytest.raises(ValidationError, match="ttl"):
        RecordConfig.model_validate(record_data(ttl=ttl))


@pytest.mark.parametrize(
    "changes",
    [
        {"type": "MX"},
        {"comment": "x" * 101},
        {"tags": ["a", "a"]},
        {"tags": [""]},
        {"content": {"ip": "not-an-ip"}},
        {"content": {"ip": 123}},
        {"proxied": True},
        {"name": "bad..example.com"},
        {"name": "a.*.example.com"},
    ],
)
def test_invalid_records(changes):
    with pytest.raises(ValidationError):
        RecordConfig.model_validate(record_data(**changes))


@pytest.mark.parametrize(
    "record_type,value",
    [
        ("A", "10.0.0.1"),
        ("AAAA", "::1"),
        ("CNAME", "TARGET.Example.Com."),
        ("NS", "_ns.example.com"),
        ("PTR", "host.example.com"),
        ("TXT", "free text"),
    ],
)
def test_static_content(record_type, value):
    RecordConfig.model_validate(record_data(type=record_type, content={"ip": value}))


@pytest.mark.parametrize(
    "document",
    [
        config_data(zones=[]),
        config_data(zones=[{"domain": "example.com", "dns": []}]),
        config_data(
            zones=[{"domain": "example.com", "dns": [record_data(name="evil-example.com")]}]
        ),
        config_data(zones=[{"domain": "example.com", "dns": [record_data(), record_data()]}]),
        config_data(zones=config_data()["zones"] * 2),
        config_data(unknown=True),
        config_data(zones=[{"domain": "example.com", "dns": [record_data()], "typo": True}]),
    ],
)
def test_invalid_zones_and_extra_keys(document):
    with pytest.raises(ValidationError):
        AppConfig.model_validate(document)
