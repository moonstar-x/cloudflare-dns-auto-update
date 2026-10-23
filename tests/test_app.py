import asyncio
import os
import signal
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
import yaml
from conftest import api_record, config_data, envelope

from cloudflare_dns_autoupdate import app as app_module
from cloudflare_dns_autoupdate.app import Application
from cloudflare_dns_autoupdate.cli import build_parser
from cloudflare_dns_autoupdate.config.models import AppConfig
from cloudflare_dns_autoupdate.reconciler import CycleReport

BASE = "https://api.cloudflare.com/client/v4"


def write_config(path, **changes):
    path.write_text(yaml.safe_dump(config_data(**changes)))


def verify_route(respx_mock):
    return respx_mock.get(f"{BASE}/user/tokens/verify").respond(
        200, json=envelope({"status": "active"})
    )


@pytest.mark.parametrize("failed", [False, True])
async def test_once_exit_status(tmp_path, respx_mock, failed):
    path = tmp_path / "config.yml"
    write_config(path)
    verify_route(respx_mock)
    respx_mock.get(f"{BASE}/zones").respond(200, json=envelope([{"id": "z"}]))
    respx_mock.get(f"{BASE}/zones/z/dns_records").respond(
        200, json=envelope([] if failed else [api_record()])
    )
    if failed:
        respx_mock.post(f"{BASE}/zones/z/dns_records").respond(
            400, json=envelope(None, success=False, errors=[{"code": 1, "message": "bad"}])
        )
    application = Application(path, once=True)
    assert await application.run() == int(failed)
    assert application._watcher_task is None
    assert application.client._http.is_closed


async def test_invalid_startup_is_fatal(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text("token: [broken")
    application = Application(path, once=True)
    assert await application.run() == 1
    assert application.client is None


async def test_reload_waits_for_cycle_and_runs_early(tmp_path, respx_mock, monkeypatch):
    path = tmp_path / "config.yml"
    write_config(path)
    verify_route(respx_mock)
    first_started, release_first, second_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    snapshots = []

    async def cycle(config, client, **kwargs):
        snapshots.append(config)
        if len(snapshots) == 1:
            first_started.set()
            await release_first.wait()
        else:
            second_started.set()
        return CycleReport([], 0)

    monkeypatch.setattr(app_module, "reconcile", cycle)
    application = Application(path)
    running = asyncio.create_task(application.run())
    reload_task = None
    try:
        await asyncio.wait_for(first_started.wait(), 2)
        original = application.config
        old_client = application.client
        write_config(path, interval="10s")
        reload_task = asyncio.create_task(application.watcher.check())
        await asyncio.sleep(0.03)
        assert application.config is original
        assert not old_client._http.is_closed
        release_first.set()
        assert await asyncio.wait_for(reload_task, 2)
        await asyncio.wait_for(second_started.wait(), 2)
        assert snapshots[0] is original
        assert snapshots[1].interval.total_seconds() == 10
        assert application.client is old_client
    finally:
        release_first.set()
        application.request_shutdown()
        if reload_task is not None:
            await asyncio.gather(reload_task, return_exceptions=True)
        assert await asyncio.wait_for(running, 2) == 0


async def test_token_change_recreates_client(tmp_path, respx_mock, monkeypatch):
    path = tmp_path / "config.yml"
    write_config(path)
    verify = verify_route(respx_mock)
    started = asyncio.Event()

    async def cycle(*args, **kwargs):
        started.set()
        return CycleReport([], 0)

    monkeypatch.setattr(app_module, "reconcile", cycle)
    application = Application(path)
    running = asyncio.create_task(application.run())
    try:
        await asyncio.wait_for(started.wait(), 2)
        old_client = application.client
        old_client._zone_ids["example.com"] = "cached"
        write_config(path, token="new-secret")
        assert await application.watcher.check()
        assert application.client is not old_client
        assert old_client._http.is_closed
        assert application.client._zone_ids == {}
        assert verify.call_count == 2
        assert verify.calls.last.request.headers["Authorization"] == "Bearer new-secret"
    finally:
        application.request_shutdown()
        assert await asyncio.wait_for(running, 2) == 0


async def test_invalid_reload_keeps_config(tmp_path, respx_mock, monkeypatch):
    path = tmp_path / "config.yml"
    write_config(path)
    verify_route(respx_mock)
    started = asyncio.Event()

    async def cycle(*args, **kwargs):
        started.set()
        return CycleReport([], 0)

    monkeypatch.setattr(app_module, "reconcile", cycle)
    application = Application(path)
    running = asyncio.create_task(application.run())
    try:
        await asyncio.wait_for(started.wait(), 2)
        original = application.config
        path.write_text("zones: []")
        assert not await application.watcher.check()
        assert application.config is original
        assert not application.wake_event.is_set()
    finally:
        application.request_shutdown(signal.SIGTERM)
        assert await asyncio.wait_for(running, 2) == 0


async def test_scheduler_recovers_from_exception_and_never_overlaps(tmp_path, monkeypatch):
    application = Application(tmp_path / "config.yml")
    application.config = AppConfig.model_validate(config_data()).model_copy(
        update={"interval": timedelta(seconds=0.01)}
    )
    application.client = AsyncMock()
    calls = 0
    active = 0

    async def cycle(*args, **kwargs):
        nonlocal calls, active
        calls += 1
        if calls == 1:
            raise RuntimeError("unexpected")
        active += 1
        assert active == 1
        await asyncio.sleep(0.02)
        active -= 1
        application.stop_event.set()
        return CycleReport([], 0)

    monkeypatch.setattr(app_module, "reconcile", cycle)
    assert await asyncio.wait_for(application.run_scheduler(), 1) == 0
    assert calls == 2


async def test_sigterm_gracefully_finishes_active_cycle(tmp_path, respx_mock, monkeypatch, caplog):
    path = tmp_path / "config.yml"
    write_config(path)
    verify_route(respx_mock)
    started, finish = asyncio.Event(), asyncio.Event()

    async def cycle(*args, **kwargs):
        started.set()
        await finish.wait()
        return CycleReport([], 0)

    monkeypatch.setattr(app_module, "reconcile", cycle)
    application = Application(path)
    running = asyncio.create_task(application.run())
    await asyncio.wait_for(started.wait(), 2)
    assert signal.SIGTERM in application._installed_signals
    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.sleep(0.02)
    assert not running.done()
    finish.set()
    assert await asyncio.wait_for(running, 2) == 0
    assert application.client._http.is_closed


async def test_shutdown_cancels_after_grace(tmp_path, respx_mock, monkeypatch):
    path = tmp_path / "config.yml"
    write_config(path)
    verify_route(respx_mock)
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def cycle(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(app_module, "reconcile", cycle)
    monkeypatch.setattr(app_module, "SHUTDOWN_GRACE_SECONDS", 0.02)
    application = Application(path)
    running = asyncio.create_task(application.run())
    await asyncio.wait_for(started.wait(), 2)
    application.request_shutdown()
    assert await asyncio.wait_for(running, 2) == 0
    assert cancelled.is_set()
    assert application.client._http.is_closed


def test_second_signal_forces_exit(tmp_path, monkeypatch):
    exit = []
    monkeypatch.setattr(app_module.os, "_exit", exit.append)
    application = Application(tmp_path / "config.yml")
    application.request_shutdown()
    application.request_shutdown(signal.SIGINT)
    assert exit == [0]


def test_cli_environment_and_overrides(monkeypatch):
    monkeypatch.setenv("CONFIG_PATH", "/tmp/environment.yml")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("LOG_LEVEL", "ERROR")
    args = build_parser().parse_args(["--config", "/tmp/cli.yml", "--log-level", "DEBUG", "--once"])
    assert str(args.config) == "/tmp/cli.yml"
    assert args.log_level == "DEBUG" and args.once and args.dry_run
