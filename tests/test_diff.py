import pytest
from conftest import api_record, record_data

from cloudflare_dns_autoupdate.cloudflare.models import DnsRecord
from cloudflare_dns_autoupdate.config.models import RecordConfig
from cloudflare_dns_autoupdate.diff import create_payload, diff_record


def test_no_difference(record):
    assert diff_record(record, "203.0.113.10", DnsRecord.model_validate(api_record())).payload == {}


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({"content": "8.8.8.8"}, {"content": "203.0.113.10"}),
        ({"ttl": 300}, {"ttl": 1}),
        ({"comment": "old"}, {"comment": None}),
        ({"tags": ["old"]}, {"tags": []}),
    ],
)
def test_minimal_diff(record, changes, expected):
    diff = diff_record(record, "203.0.113.10", DnsRecord.model_validate(api_record(**changes)))
    assert diff.payload == expected
    assert "→" in diff.description


def test_tag_order_and_null_comment():
    desired = RecordConfig.model_validate(record_data(tags=["a", "b"]))
    existing = DnsRecord.model_validate(api_record(tags=["b", "a"], comment=None))
    assert diff_record(desired, "203.0.113.10", existing).payload == {}


@pytest.mark.parametrize(
    "kind,desired,current",
    [
        ("TXT", "hello", '"hello"'),
        ("CNAME", "target.example.com", "TARGET.Example.Com."),
        ("AAAA", "2606:4700::1", "2606:4700:0:0:0:0:0:1"),
    ],
)
def test_content_normalization(kind, desired, current):
    record = RecordConfig.model_validate(record_data(type=kind, content={"ip": desired}))
    existing = DnsRecord.model_validate(api_record(type=kind, content=current))
    assert diff_record(record, desired, existing).payload == {}


def test_proxied_ttl():
    desired = RecordConfig.model_validate(record_data(ttl=300))
    existing = DnsRecord.model_validate(api_record(proxied=True))
    difference = diff_record(desired, "203.0.113.10", existing)
    assert difference.payload == {}
    assert difference.ttl_ignored


def test_create_payload_defaults_and_optional_fields(record):
    payload = create_payload(record, "203.0.113.10")
    assert set(payload) == {"name", "type", "content", "ttl"}
    record = RecordConfig.model_validate(record_data(comment="managed", tags=["owner:me"]))
    payload = create_payload(record, "203.0.113.10")
    assert payload["comment"] == "managed"
    assert payload["tags"] == ["owner:me"]
    assert "proxied" not in payload
