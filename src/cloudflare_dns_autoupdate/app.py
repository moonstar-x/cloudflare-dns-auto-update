"""Application ownership, sequential fixed-rate scheduling, signals, and reloads."""

import asyncio
import logging
import os
import platform
import signal
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from . import __version__
from .cloudflare import CloudflareClient
from .config.loader import ConfigError, load_config_snapshot
from .config.models import AppConfig
from .logging_setup import ContextRedactionFilter, log_context
from .reconciler import CycleReport, reconcile
from .watcher import ConfigWatcher, describe_changes

SHUTDOWN_GRACE_SECONDS = 10


class Application:
    """Own all mutable service state and swap generations only between cycles."""

    def __init__(
        self,
        config_path: Path,
        *,
        dry_run: bool = False,
        once: bool = False,
        redaction: ContextRedactionFilter | None = None,
    ) -> None:
        self.config_path = config_path.absolute()
        self.dry_run = dry_run
        self.once = once
        self.redaction = redaction if redaction is not None else ContextRedactionFilter()
        self.config: AppConfig | None = None
        self.client: CloudflareClient | None = None
        self.watcher: ConfigWatcher | None = None
        self.wake_event = asyncio.Event()
        self.stop_event = asyncio.Event()
        self._generation_lock = asyncio.Lock()
        self._watcher_task: asyncio.Task[None] | None = None
        self._logger = logging.getLogger(__name__)
        self._installed_signals: list[signal.Signals] = []

    def request_shutdown(self, signum: signal.Signals = signal.SIGTERM) -> None:
        """Allow one graceful shutdown; a second signal exits immediately."""
        if self.stop_event.is_set():
            self._logger.warning("received second signal, forcing immediate exit")
            os._exit(0)
        self._logger.info("received %s, shutting down", signum.name)
        self.stop_event.set()
        self.wake_event.set()
        if self.watcher is not None:
            self.watcher.stop_event.set()
        if self._watcher_task is not None:
            self._watcher_task.cancel()

    def _install_signals(self) -> None:
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(signum, self.request_shutdown, signum)
                self._installed_signals.append(signum)
            except (NotImplementedError, RuntimeError):
                self._logger.debug("signal handlers unavailable", exc_info=True)

    def _log_config(self, config: AppConfig) -> None:
        self._logger.info(
            "config: interval=%s (%ss) zones=%s records=%s",
            config.interval_original,
            int(config.interval.total_seconds()),
            len(config.zones),
            config.record_count,
        )
        for zone in config.zones:
            for record in zone.dns:
                self._logger.info(
                    "%s / %s / %s / %s / %s",
                    zone.domain,
                    record.name,
                    record.type,
                    record.content.kind,
                    "Auto" if record.ttl == 1 else record.ttl,
                )

    async def apply_config(self, config: AppConfig) -> None:
        """Wait for the snapshot's cycle, rotate clients safely, and wake the scheduler."""
        self.redaction.add_token(config.token.get_secret_value())
        async with self._generation_lock:
            if self.stop_event.is_set():
                return
            assert self.config is not None and self.client is not None
            previous = self.config
            if previous.token != config.token:
                replacement = CloudflareClient(config.token.get_secret_value())
                try:
                    await replacement.verify_token()
                except BaseException:
                    await replacement.aclose()
                    raise
                old_client = self.client
                self.client = replacement
                await old_client.aclose()
            self.client.clear_zone_cache()
            self.config = config
            self._logger.info("configuration reloaded: %s", describe_changes(previous, config))
            self._log_config(config)
            self.wake_event.set()

    async def _cycle(self) -> CycleReport | None:
        async with self._generation_lock:
            assert self.config is not None and self.client is not None
            # No await can swap the generation while this snapshot is in use.
            config, client = self.config, self.client
            identifier = uuid4().hex[:8]
            with log_context(cycle=identifier):
                try:
                    return await reconcile(
                        config, client, dry_run=self.dry_run, cycle_id=identifier
                    )
                except Exception:
                    self._logger.exception("unexpected cycle failure; scheduler will continue")
                    return None

    async def run_scheduler(self) -> int:
        """Run immediately and at a fixed rate, with no overlapping cycles."""
        loop = asyncio.get_running_loop()
        while not self.stop_event.is_set():
            self.wake_event.clear()
            started = loop.time()
            await self._cycle()
            if self.stop_event.is_set():
                break
            assert self.config is not None
            delay = max(0.0, started + self.config.interval.total_seconds() - loop.time())
            if delay == 0:
                self._logger.warning("cycle exceeded interval; starting next cycle immediately")
            next_time = datetime.now(UTC) + timedelta(seconds=delay)
            self._logger.info(
                "next cycle at %s (in %s)",
                next_time.isoformat(timespec="seconds").replace("+00:00", "Z"),
                self.config.interval_original if delay else "0s",
            )
            # Even overrun cycles yield so a queued reload can acquire the generation lock.
            await asyncio.sleep(0)
            with suppress(TimeoutError):
                await asyncio.wait_for(self.wake_event.wait(), timeout=delay)
        return 0

    async def _serve(self) -> int:
        assert self.client is not None
        await self.client.verify_token()
        if self.stop_event.is_set():
            return 0
        if self.once:
            report = await self._cycle()
            return int(report is None or report.failed > 0)
        assert self.watcher is not None
        self._watcher_task = asyncio.create_task(self.watcher.run())
        return await self.run_scheduler()

    async def run(self) -> int:
        """Load initial state, run until completion or a signal, and always close resources."""
        self._logger.info(
            "cloudflare-dns-autoupdate %s Python %s config=%s dry_run=%s once=%s",
            __version__,
            platform.python_version(),
            self.config_path,
            self.dry_run,
            self.once,
        )
        try:
            self.config, digest = await load_config_snapshot(self.config_path)
        except ConfigError as exc:
            self._logger.error("%s", exc)
            return 1
        self.redaction.add_token(self.config.token.get_secret_value())
        self._log_config(self.config)
        self.client = CloudflareClient(self.config.token.get_secret_value())
        self.watcher = ConfigWatcher(self.config_path, digest, self.apply_config)
        self._install_signals()
        work = asyncio.create_task(self._serve())
        stop = asyncio.create_task(self.stop_event.wait())
        try:
            await asyncio.wait({work, stop}, return_when=asyncio.FIRST_COMPLETED)
            if self.stop_event.is_set():
                try:
                    await asyncio.wait_for(asyncio.shield(work), timeout=SHUTDOWN_GRACE_SECONDS)
                except TimeoutError:
                    self._logger.warning("shutdown grace period expired; cancelling active cycle")
                    work.cancel()
                    await asyncio.gather(work, return_exceptions=True)
                return 0
            return await work
        finally:
            stop.cancel()
            work.cancel()
            if self.watcher is not None:
                self.watcher.stop_event.set()
            if self._watcher_task is not None:
                self._watcher_task.cancel()
            await asyncio.gather(
                work,
                stop,
                *([self._watcher_task] if self._watcher_task is not None else []),
                return_exceptions=True,
            )
            await self.client.aclose()
            for signum in self._installed_signals:
                asyncio.get_running_loop().remove_signal_handler(signum)
            self._logger.info("shutdown complete")
