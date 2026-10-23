import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from conftest import api_record, envelope

from cloudflare_dns_autoupdate import __version__
from cloudflare_dns_autoupdate.cloudflare import CloudflareAPIError, CloudflareClient

BASE = "https://api.cloudflare.com/client/v4"


async def test_headers_zone_cache_and_clear(respx_mock):
    route = respx_mock.get(f"{BASE}/zones", params={"name": "example.com"}).respond(
        200, json=envelope([{"id": "zone-1"}])
    )
    async with CloudflareClient("secret") as client:
        assert await client.get_zone_id("example.com") == "zone-1"
        assert await client.get_zone_id("example.com") == "zone-1"
        assert route.call_count == 1
        client.clear_zone_cache()
        await client.get_zone_id("example.com")
    assert route.call_count == 2
    headers = route.calls.last.request.headers
    assert headers["Authorization"] == "Bearer secret"
    assert headers["User-Agent"] == f"cloudflare-dns-autoupdate/{__version__}"
    assert headers["Content-Type"] == "application/json"


async def test_pagination(respx_mock):
    for page in (1, 2):
        respx_mock.get(
            f"{BASE}/zones/z/dns_records", params={"per_page": 100, "page": page}
        ).respond(200, json=envelope([api_record(id=f"r{page}")], result_info={"total_pages": 2}))
    async with CloudflareClient("secret") as client:
        assert [record.id for record in await client.list_dns_records("z")] == ["r1", "r2"]


@pytest.mark.parametrize("status", [400, 401, 403, 200])
async def test_envelope_errors_no_retry(status, respx_mock, caplog):
    route = respx_mock.get(f"{BASE}/zones").respond(
        status, json=envelope(None, success=False, errors=[{"code": 1000, "message": "broken"}])
    )
    async with CloudflareClient("secret") as client:
        with pytest.raises(CloudflareAPIError, match="1000: broken"):
            await client.get_zone_id("example.com")
    assert route.call_count == 1
    if status in (401, 403):
        assert "Zone:Read and DNS:Edit" in caplog.text


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_retries_and_retry_after(status, respx_mock, monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr("cloudflare_dns_autoupdate.cloudflare.client.asyncio.sleep", sleep)
    route = respx_mock.get(f"{BASE}/zones").mock(
        side_effect=[
            httpx.Response(status, headers={"Retry-After": "120"}),
            httpx.Response(status, headers={"Retry-After": "2"}),
            httpx.Response(200, json=envelope([{"id": "z"}])),
        ]
    )
    async with CloudflareClient("secret") as client:
        assert await client.get_zone_id("example.com") == "z"
    assert route.call_count == 3
    assert [call.args[0] for call in sleep.await_args_list] == [60, 2]


async def test_transport_retries_exhausted(respx_mock, monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr("cloudflare_dns_autoupdate.cloudflare.client.asyncio.sleep", sleep)
    route = respx_mock.get(f"{BASE}/zones").mock(side_effect=httpx.ConnectError("offline"))
    async with CloudflareClient("secret") as client:
        with pytest.raises(CloudflareAPIError, match="offline"):
            await client.get_zone_id("example.com")
    assert route.call_count == 3
    assert 0.75 <= sleep.await_args_list[0].args[0] <= 1.25
    assert 1.5 <= sleep.await_args_list[1].args[0] <= 2.5


async def test_final_5xx_error(respx_mock, monkeypatch):
    monkeypatch.setattr("cloudflare_dns_autoupdate.cloudflare.client.asyncio.sleep", AsyncMock())
    route = respx_mock.get(f"{BASE}/zones").respond(
        503, json=envelope(None, success=False, errors=[{"code": 9, "message": "unavailable"}])
    )
    async with CloudflareClient("secret") as client:
        with pytest.raises(CloudflareAPIError, match="9: unavailable"):
            await client.get_zone_id("example.com")
    assert route.call_count == 3


@pytest.mark.parametrize(
    "response",
    [httpx.Response(200, text="oops"), httpx.Response(200, json=envelope([{"not": "a zone"}]))],
)
async def test_malformed_response(response, respx_mock):
    respx_mock.get(f"{BASE}/zones").mock(return_value=response)
    async with CloudflareClient("secret") as client:
        with pytest.raises(CloudflareAPIError, match="invalid Cloudflare"):
            await client.get_zone_id("example.com")


async def test_patch_clear_fields_preserves_null_and_empty_list(respx_mock):
    route = respx_mock.patch(f"{BASE}/zones/z/dns_records/r").respond(
        200, json=envelope(api_record(comment=None, tags=[]))
    )
    async with CloudflareClient("secret") as client:
        await client.patch_dns_record("z", "r", {"comment": None, "tags": []})
    assert json.loads(route.calls.last.request.content) == {"comment": None, "tags": []}


async def test_verify_failure_nonfatal(respx_mock, caplog):
    respx_mock.get(f"{BASE}/user/tokens/verify").respond(
        403, json=envelope(None, success=False, errors=[{"code": 1, "message": "account token"}])
    )
    async with CloudflareClient("secret") as client:
        await client.verify_token()
    assert "account-owned tokens may fail" in caplog.text


async def test_unmanaged_fields_rejected():
    async with CloudflareClient("secret") as client:
        with pytest.raises(ValueError, match="unmanaged"):
            await client.patch_dns_record("z", "r", {"proxied": True})
        with pytest.raises(ValueError, match="unmanaged"):
            await client.create_dns_record("z", {"proxied": False})


async def test_client_concurrency_limit(respx_mock):
    active = 0
    maximum = 0

    async def respond(request):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(200, json=envelope([{"id": "z"}]))

    respx_mock.get(f"{BASE}/zones").mock(side_effect=respond)
    async with CloudflareClient("secret") as client:
        await asyncio.gather(*(client.get_zone_id(f"zone{i}.com") for i in range(12)))
    assert maximum == 4
