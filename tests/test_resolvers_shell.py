import asyncio
import os
import signal
from unittest.mock import patch

import pytest
from conftest import record_data

from cloudflare_dns_autoupdate.config.models import RecordConfig
from cloudflare_dns_autoupdate.resolvers import ResolverError, resolve_content, shell


def shell_record(source):
    return RecordConfig.model_validate(record_data(type="TXT", content={"shell": source}))


async def test_success_and_whitespace(zone):
    assert await shell.resolve(shell_record("printf '  hello  \n'"), zone) == "hello"


async def test_environment(zone):
    value = await shell.resolve(
        shell_record('printf "%s/%s/%s" "$CFDA_RECORD_NAME" "$CFDA_RECORD_TYPE" "$CFDA_ZONE"'), zone
    )
    assert value == "home.example.com/TXT/example.com"


@pytest.mark.parametrize(
    "source,error",
    [
        ("printf broken >&2; exit 7", "exit code 7: broken"),
        ("printf '  '", "empty output"),
    ],
)
async def test_failures(zone, source, error):
    with pytest.raises(ResolverError, match=error):
        await shell.resolve(shell_record(source), zone)


async def test_stderr_warning(zone, caplog):
    assert await shell.resolve(shell_record("printf ok; printf warning >&2"), zone) == "ok"
    assert "succeeded with stderr: warning" in caplog.text


async def test_timeout_kills_group(zone, monkeypatch):
    monkeypatch.setattr(shell, "RESOLVER_TIMEOUT_SECONDS", 0.03)
    with patch.object(os, "killpg", wraps=os.killpg) as kill:
        with pytest.raises(ResolverError, match="timed out"):
            await shell.resolve(shell_record("sleep 30 & wait"), zone)
        assert kill.call_count == 1
        assert kill.call_args.args[1] == signal.SIGKILL


async def test_cancellation_kills_group(zone):
    with patch.object(os, "killpg", wraps=os.killpg) as kill:
        task = asyncio.create_task(shell.resolve(shell_record("sleep 30 & wait"), zone))
        await asyncio.sleep(0.03)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert kill.call_count == 1


@pytest.mark.parametrize(
    "record_type,value",
    [
        ("A", "not-an-ip"),
        ("A", "192.168.100.999"),
        ("A", "fd00::1"),
        ("AAAA", "192.168.100.192"),
        ("AAAA", "fd00::invalid"),
    ],
)
async def test_dynamic_invalid_never_accepted(zone, record_type, value):
    record = RecordConfig.model_validate(
        record_data(type=record_type, content={"shell": f"printf '%s' '{value}'"})
    )
    with pytest.raises(ResolverError, match=f"invalid {record_type} content"):
        await resolve_content(record, zone)
