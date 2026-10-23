import asyncio
import json

import pytest
from conftest import api_record, config_data, envelope, record_data

from cloudflare_dns_autoupdate.cloudflare import CloudflareClient
from cloudflare_dns_autoupdate.config.models import AppConfig
from cloudflare_dns_autoupdate.reconciler import reconcile

BASE = "https://api.cloudflare.com/client/v4"


def mock_zone(respx_mock, records):
    respx_mock.get(f"{BASE}/zones", params={"name": "example.com"}).respond(
        200, json=envelope([{"id": "z"}])
    )
    return respx_mock.get(f"{BASE}/zones/z/dns_records").respond(200, json=envelope(records))


async def test_create_then_idempotent(config, respx_mock):
    listing = mock_zone(respx_mock, [])
    creation = respx_mock.post(f"{BASE}/zones/z/dns_records").respond(
        200, json=envelope(api_record())
    )
    async with CloudflareClient("secret") as client:
        first = await reconcile(config, client, dry_run=False, cycle_id="first")
        assert first.created == 1 and first.failed == 0
        listing.respond(200, json=envelope([api_record()]))
        second = await reconcile(config, client, dry_run=False, cycle_id="second")
    assert second.unchanged == 1
    assert creation.call_count == 1
    payload = json.loads(creation.calls.last.request.content)
    assert set(payload) == {"type", "name", "content", "ttl"}


async def test_update_only_changed_fields_then_idempotent(config, respx_mock):
    listing = mock_zone(respx_mock, [api_record(content="8.8.8.8")])
    patch = respx_mock.patch(f"{BASE}/zones/z/dns_records/record-1").respond(
        200, json=envelope(api_record())
    )
    async with CloudflareClient("secret") as client:
        first = await reconcile(config, client, dry_run=False, cycle_id="first")
        listing.respond(200, json=envelope([api_record()]))
        second = await reconcile(config, client, dry_run=False, cycle_id="second")
    assert first.updated == 1 and second.unchanged == 1
    assert json.loads(patch.calls.last.request.content) == {"content": "203.0.113.10"}
    assert patch.call_count == 1


@pytest.mark.parametrize("source_kind", ["shell", "eval"])
@pytest.mark.parametrize("record_type,value", [("A", "192.168.100.192"), ("AAAA", "fd00::1")])
async def test_dynamic_private_ip_create_update_and_idempotence(
    respx_mock, source_kind, record_type, value
):
    source = f"printf '%s' '{value}'" if source_kind == "shell" else repr(value)
    public_ip = "1.1.1.1" if record_type == "A" else "2606:4700:4700::1111"
    config = AppConfig.model_validate(
        config_data(
            zones=[
                {
                    "domain": "example.com",
                    "dns": [record_data(type=record_type, content={source_kind: source})],
                }
            ]
        )
    )
    desired = api_record(type=record_type, content=value)
    listing = mock_zone(respx_mock, [])
    creation = respx_mock.post(f"{BASE}/zones/z/dns_records").respond(200, json=envelope(desired))
    patch = respx_mock.patch(f"{BASE}/zones/z/dns_records/record-1").respond(
        200, json=envelope(desired)
    )
    async with CloudflareClient("secret") as client:
        created = await reconcile(config, client, dry_run=False, cycle_id="create-private")
        listing.respond(200, json=envelope([api_record(type=record_type, content=public_ip)]))
        updated = await reconcile(config, client, dry_run=False, cycle_id="update-private")
        listing.respond(200, json=envelope([desired]))
        unchanged = await reconcile(config, client, dry_run=False, cycle_id="unchanged-private")
    assert created.created == 1 and created.failed == 0
    assert updated.updated == 1 and updated.failed == 0
    assert unchanged.unchanged == 1 and unchanged.failed == 0
    assert creation.call_count == patch.call_count == 1
    assert json.loads(creation.calls.last.request.content)["content"] == value
    assert json.loads(patch.calls.last.request.content) == {"content": value}


async def test_ambiguous_skipped(config, respx_mock, caplog):
    mock_zone(respx_mock, [api_record(), api_record(id="other", content="8.8.8.8")])
    async with CloudflareClient("secret") as client:
        report = await reconcile(config, client, dry_run=False, cycle_id="ambiguous")
    assert report.skipped == 1
    assert "other" in caplog.text and "8.8.8.8" in caplog.text
    assert all(call.request.method == "GET" for call in respx_mock.calls)


async def test_zone_not_found(config, respx_mock):
    respx_mock.get(f"{BASE}/zones").respond(200, json=envelope([]))
    async with CloudflareClient("secret") as client:
        report = await reconcile(config, client, dry_run=False, cycle_id="missing")
    assert report.skipped == 1


async def test_dry_run_zero_writes(config, respx_mock):
    listing = mock_zone(respx_mock, [])
    async with CloudflareClient("secret") as client:
        first = await reconcile(config, client, dry_run=True, cycle_id="dry")
        listing.respond(200, json=envelope([api_record(content="8.8.8.8")]))
        second = await reconcile(config, client, dry_run=True, cycle_id="dry2")
    assert first.created == 1 and second.updated == 1
    assert all(call.request.method == "GET" for call in respx_mock.calls)


async def test_failed_record_does_not_affect_others(respx_mock):
    config = AppConfig.model_validate(
        config_data(
            zones=[
                {
                    "domain": "example.com",
                    "dns": [
                        record_data(content={"eval": "1/0"}),
                        record_data(name="other.example.com"),
                    ],
                }
            ]
        )
    )
    mock_zone(respx_mock, [api_record(name="other.example.com")])
    async with CloudflareClient("secret") as client:
        report = await reconcile(config, client, dry_run=False, cycle_id="isolated")
    assert report.failed == 1 and report.unchanged == 1
    assert len(report.outcomes) == 2


async def test_zone_failure_isolated(respx_mock):
    config = AppConfig.model_validate(
        config_data(
            zones=[
                {"domain": "example.com", "dns": [record_data()]},
                {"domain": "other.com", "dns": [record_data(name="home.other.com")]},
            ]
        )
    )
    mock_zone(respx_mock, [api_record()])
    respx_mock.get(f"{BASE}/zones", params={"name": "other.com"}).respond(
        400, json=envelope(None, success=False, errors=[{"code": 1, "message": "bad zone"}])
    )
    async with CloudflareClient("secret") as client:
        report = await reconcile(config, client, dry_run=False, cycle_id="zones")
    assert report.failed == 1 and report.unchanged == 1


async def test_fetch_failure_marks_whole_zone(config, respx_mock):
    route = mock_zone(respx_mock, [])
    route.respond(400, json=envelope(None, success=False, errors=[{"code": 2, "message": "fetch"}]))
    async with CloudflareClient("secret") as client:
        report = await reconcile(config, client, dry_run=False, cycle_id="fetch")
    assert report.failed == config.record_count


async def test_write_failure_isolated(respx_mock):
    config = AppConfig.model_validate(
        config_data(
            zones=[
                {
                    "domain": "example.com",
                    "dns": [
                        record_data(),
                        record_data(name="other.example.com"),
                    ],
                }
            ]
        )
    )
    mock_zone(respx_mock, [api_record(name="other.example.com")])
    respx_mock.post(f"{BASE}/zones/z/dns_records").respond(
        400, json=envelope(None, success=False, errors=[{"code": 3, "message": "write"}])
    )
    async with CloudflareClient("secret") as client:
        report = await reconcile(config, client, dry_run=False, cycle_id="write")
    assert report.failed == 1 and report.unchanged == 1


async def test_global_resolver_limit_across_zones(respx_mock, monkeypatch):
    from cloudflare_dns_autoupdate.resolvers import evaluator

    active = 0
    maximum = 0

    async def resolve(*args, **kwargs):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1
        return "hello"

    monkeypatch.setattr(evaluator, "resolve", resolve)
    zones = []
    for domain, zone_id in [("example.com", "z"), ("other.com", "z2")]:
        zones.append(
            {
                "domain": domain,
                "dns": [
                    record_data(
                        name=f"r{i}.{domain}", type="TXT", content={"eval": f"'{domain}-{i}'"}
                    )
                    for i in range(10)
                ],
            }
        )
        respx_mock.get(f"{BASE}/zones", params={"name": domain}).respond(
            200, json=envelope([{"id": zone_id}])
        )
        respx_mock.get(f"{BASE}/zones/{zone_id}/dns_records").respond(200, json=envelope([]))
    config = AppConfig.model_validate(config_data(zones=zones))
    async with CloudflareClient("secret") as client:
        report = await reconcile(config, client, dry_run=True, cycle_id="limit")
    assert report.created == 20 and report.failed == 0
    assert maximum == 8
