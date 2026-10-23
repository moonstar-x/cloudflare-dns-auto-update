import asyncio
from unittest.mock import AsyncMock

import pytest
from conftest import record_data

from cloudflare_dns_autoupdate.config.models import RecordConfig
from cloudflare_dns_autoupdate.resolvers import ResolverError, evaluator, resolve_content


def eval_record(source):
    return RecordConfig.model_validate(record_data(type="TXT", content={"eval": source}))


@pytest.mark.parametrize(
    "source,expected",
    [
        ("1 + 2", "3"),
        ("result = '  hello  '", "hello"),
        ("f'{record_name}/{record_type}/{zone}'", "home.example.com/TXT/example.com"),
        ("json.dumps({'a': 1})", '{"a": 1}'),
        (
            "import asyncio\nasync def value():\n    await asyncio.sleep(0)\n    return 'ok'\n"
            "result = value()",
            "ok",
        ),
    ],
)
async def test_evaluation(zone, source, expected):
    assert await evaluator.resolve(eval_record(source), zone) == expected


@pytest.mark.parametrize(
    "source,message",
    [
        ("x = 1", "assign.*result"),
        ("None", "None"),
        ("'  '", "empty"),
        ("1 / 0", "ZeroDivisionError"),
        ("__import__('sys').exit(2)", "SystemExit: 2"),
        ("result = (", "syntax error at line 1"),
    ],
)
async def test_errors(zone, source, message):
    with pytest.raises(ResolverError, match=message):
        await evaluator.resolve(eval_record(source), zone)


async def test_timeout_does_not_block_loop(zone, monkeypatch, caplog):
    monkeypatch.setattr(evaluator, "RESOLVER_TIMEOUT_SECONDS", 0.02)
    with pytest.raises(ResolverError, match="timed out"):
        await evaluator.resolve(eval_record("import time\ntime.sleep(0.08)\nresult = 'ok'"), zone)
    assert "thread cannot be killed" in caplog.text
    await asyncio.sleep(0.09)


async def test_awaitable_timeout(zone, monkeypatch):
    monkeypatch.setattr(evaluator, "RESOLVER_TIMEOUT_SECONDS", 0.02)
    source = "import asyncio\nresult = asyncio.sleep(20)"
    with pytest.raises(ResolverError, match="timed out"):
        await evaluator.resolve(eval_record(source), zone)


async def test_fresh_namespace(zone):
    await evaluator.resolve(eval_record("secret = 1\nresult = 'ok'"), zone)
    assert await evaluator.resolve(eval_record("str('secret' in globals())"), zone) == "False"


async def test_cache_shares_concurrent_execution_and_resets(zone, monkeypatch):
    runner = AsyncMock(return_value="hello")
    monkeypatch.setattr(evaluator, "resolve", runner)
    cache = {}
    record = eval_record("'hello'")
    results = await asyncio.gather(*(resolve_content(record, zone, cache=cache) for _ in range(10)))
    assert results == ["hello"] * 10
    assert runner.await_count == 1
    await resolve_content(record, zone, cache={})
    assert runner.await_count == 2


async def test_cache_shares_failures(zone, monkeypatch):
    runner = AsyncMock(side_effect=ResolverError("broken"))
    monkeypatch.setattr(evaluator, "resolve", runner)
    cache = {}
    results = await asyncio.gather(
        *(resolve_content(eval_record("bad"), zone, cache=cache) for _ in range(10)),
        return_exceptions=True,
    )
    assert all(isinstance(result, ResolverError) for result in results)
    assert runner.await_count == 1
