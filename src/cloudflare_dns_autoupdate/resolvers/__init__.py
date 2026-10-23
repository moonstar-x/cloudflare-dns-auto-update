"""Content dispatch and concurrency-safe, per-cycle source memoization."""

import asyncio
import logging

from ..config.models import RecordConfig, ZoneConfig
from ..validation import validate_content
from . import evaluator, shell, static
from .errors import RESOLVER_TIMEOUT_SECONDS, ResolverError

type ResolutionCache = dict[tuple[str, str], asyncio.Task[str]]

__all__ = [
    "RESOLVER_TIMEOUT_SECONDS",
    "ResolutionCache",
    "ResolverError",
    "resolve_content",
    "validate_content",
]


async def resolve_content(
    record: RecordConfig,
    zone: ZoneConfig,
    *,
    cache: ResolutionCache | None = None,
    semaphore: asyncio.Semaphore | None = None,
    filename: str = "<config:content.eval>",
) -> str:
    """Share raw source executions, then validate each record's type independently."""
    if cache is None:
        cache = {}
    if semaphore is None:
        semaphore = asyncio.Semaphore(8)
    key = (record.content.kind, record.content.source)
    logger = logging.getLogger(__name__)

    async def run() -> str:
        async with semaphore:
            logger.debug("resolver invocation: %s", record.content.kind)
            match record.content.kind:
                case "shell":
                    return await shell.resolve(record, zone)
                case "eval":
                    return await evaluator.resolve(record, zone, filename=filename)
                case "ip":
                    return await static.resolve(record, zone)

    if key not in cache:
        cache[key] = asyncio.create_task(run())
    else:
        logger.debug("resolver cache hit: %s", record.content.kind)
    try:
        raw = await cache[key]
        logger.debug("resolver raw output via %s: %r", record.content.kind, raw)
        return validate_content(record.type, raw, dynamic=record.content.kind != "ip")
    except ValueError as exc:
        raise ResolverError(f"invalid {record.type} content: {exc}") from exc
