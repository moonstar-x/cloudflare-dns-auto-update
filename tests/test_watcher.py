import asyncio
import hashlib
from unittest.mock import AsyncMock

import yaml
from conftest import config_data, record_data

from cloudflare_dns_autoupdate.config.models import AppConfig
from cloudflare_dns_autoupdate.watcher import ConfigWatcher, describe_changes


def write_config(path, **changes):
    data = yaml.safe_dump(config_data(**changes)).encode()
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


async def test_valid_invalid_identical_and_deletion(tmp_path, caplog):
    path = tmp_path / "config.yml"
    digest = write_config(path)
    reload = AsyncMock()
    watcher = ConfigWatcher(path, digest, reload)
    assert not await watcher.check()
    reload.assert_not_awaited()
    path.touch()
    assert not await watcher.check()
    path.write_text("token: [broken")
    assert not await watcher.check()
    assert watcher.loaded_hash == digest
    assert "keeping previous configuration" in caplog.text
    path.unlink()
    assert not await watcher.check()
    assert "deleted" in caplog.text
    write_config(path, interval="10s")
    assert await watcher.check()
    assert reload.await_count == 1
    assert reload.await_args.args[0].interval.total_seconds() == 10
    assert not await watcher.check()


async def test_atomic_save_observed(tmp_path, monkeypatch):
    monkeypatch.setenv("WATCHFILES_FORCE_POLLING", "true")
    monkeypatch.setenv("WATCHFILES_POLL_DELAY_MS", "20")
    path = tmp_path / "config.yml"
    digest = write_config(path)
    changed = asyncio.Event()

    async def reloaded(config):
        changed.set()

    watcher = ConfigWatcher(path, digest, reloaded)
    task = asyncio.create_task(watcher.run())
    try:
        await asyncio.sleep(0.05)
        replacement = tmp_path / "temporary.yml"
        write_config(replacement, interval="10s")
        replacement.replace(path)
        await asyncio.wait_for(changed.wait(), timeout=4)
    finally:
        watcher.stop_event.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_kubernetes_symlink_swap(tmp_path):
    first = tmp_path / "version1"
    second = tmp_path / "version2"
    first.mkdir()
    second.mkdir()
    digest = write_config(first / "config.yml")
    write_config(second / "config.yml", interval="10s")
    data_link = tmp_path / "..data"
    data_link.symlink_to(first, target_is_directory=True)
    config_link = tmp_path / "config.yml"
    config_link.symlink_to("..data/config.yml")
    reload = AsyncMock()
    watcher = ConfigWatcher(config_link, digest, reload)
    replacement = tmp_path / "..data_tmp"
    replacement.symlink_to(second, target_is_directory=True)
    replacement.replace(data_link)
    assert await watcher.check()
    assert reload.await_args.args[0].interval.total_seconds() == 10


async def test_reload_callback_failure_preserves_hash(tmp_path):
    path = tmp_path / "config.yml"
    digest = write_config(path)
    watcher = ConfigWatcher(path, digest, AsyncMock(side_effect=RuntimeError("oops")))
    write_config(path, interval="10s")
    assert not await watcher.check()
    assert watcher.loaded_hash == digest


def test_change_summary_never_contains_token():
    old = AppConfig.model_validate(config_data())
    new = AppConfig.model_validate(
        config_data(
            token="new-secret",
            interval="10s",
            zones=[
                {"domain": "example.com", "dns": [record_data(comment="changed")]},
                {"domain": "new.com", "dns": [record_data(name="home.new.com")]},
            ],
        )
    )
    summary = describe_changes(old, new)
    assert "zones added=['new.com']" in summary
    assert "modified=['example.com/home.example.com/A']" in summary
    assert "token changed=yes" in summary
    assert "new-secret" not in summary
    assert "test-secret-token" not in summary
