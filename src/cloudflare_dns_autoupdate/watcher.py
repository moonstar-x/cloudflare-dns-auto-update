"""Directory-based config watching, byte hashing, and safe reload validation."""

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable, Iterable
from contextlib import suppress
from pathlib import Path

from watchfiles import Change, awatch

from .config.loader import ConfigError, parse_config
from .config.models import AppConfig


class ConfigWatcher:
    """Keep the last accepted hash and handle atomic saves and ConfigMap symlink swaps."""

    def __init__(
        self, path: Path, loaded_hash: str, on_reload: Callable[[AppConfig], Awaitable[None]]
    ) -> None:
        # Keep the lexical path as well: resolving it once loses Kubernetes' new symlink target.
        self.path = path.absolute()
        self.loaded_hash = loaded_hash
        self.on_reload = on_reload
        self.stop_event = asyncio.Event()
        self._logger = logging.getLogger(__name__)

    def _relevant_event(self, change: Change, changed_path: str) -> bool:
        candidate = Path(changed_path)
        return (
            candidate == self.path
            or candidate.resolve() == self.path.resolve()
            or candidate.name.startswith("..data")
        )

    async def check(self) -> bool:
        """Validate changed bytes and call the reload hook only for a new valid generation."""
        try:
            data = await asyncio.to_thread(self.path.read_bytes)
        except FileNotFoundError:
            self._logger.warning("config file deleted; keeping previous configuration")
            return False
        except OSError as exc:
            self._logger.error(
                "cannot read config: %s; keeping previous configuration", exc.strerror
            )
            return False
        digest = hashlib.sha256(data).hexdigest()
        if digest == self.loaded_hash:
            self._logger.debug("config bytes unchanged; ignoring event")
            return False
        try:
            config = parse_config(data)
        except ConfigError as exc:
            self._logger.error("%s: %s\nkeeping previous configuration", self.path, exc)
            return False
        try:
            await self.on_reload(config)
        except Exception:
            self._logger.exception("reload failed; keeping previous configuration")
            return False
        self.loaded_hash = digest
        return True

    async def run(self) -> None:
        """Watch the parent directory and check once to close the startup read/watch race."""
        while not self.stop_event.is_set():
            try:
                async for changes in awatch(
                    self.path.parent,
                    watch_filter=self._relevant_event,
                    debounce=500,
                    step=100,
                    stop_event=self.stop_event,
                    yield_on_timeout=True,
                    rust_timeout=1000,
                    recursive=False,
                ):
                    # Check on timeout too: symlink targets can change without a config-file event.
                    # Hashing makes polling inexpensive and harmless.
                    if changes or not self.stop_event.is_set():
                        await self.check()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._logger.exception("config watcher failed; retrying")
                with suppress(TimeoutError):
                    await asyncio.wait_for(self.stop_event.wait(), timeout=1)


def describe_changes(old: AppConfig, new: AppConfig) -> str:
    """Summarize config identities, interval changes, and credential rotation safely."""
    old_zones, new_zones = {z.domain for z in old.zones}, {z.domain for z in new.zones}
    old_records = {(z.domain, r.name, r.type): r for z in old.zones for r in z.dns}
    new_records = {(z.domain, r.name, r.type): r for z in new.zones for r in z.dns}

    def names(keys: Iterable[tuple[str, str, str]]) -> list[str]:
        return ["/".join(key) for key in sorted(keys)]

    modified = {
        key
        for key in old_records.keys() & new_records.keys()
        if old_records[key] != new_records[key]
    }
    return (
        f"zones added={sorted(new_zones - old_zones)} removed={sorted(old_zones - new_zones)}; "
        f"records added={names(new_records.keys() - old_records.keys())} "
        f"removed={names(old_records.keys() - new_records.keys())} modified={names(modified)}; "
        f"interval {old.interval_original} → {new.interval_original}; "
        f"token changed={'yes' if old.token != new.token else 'no'}"
    )
