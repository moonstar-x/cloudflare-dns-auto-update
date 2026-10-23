"""Trusted Python evaluation off the event loop, with optional coroutine results."""

import asyncio
import builtins
import inspect
import ipaddress
import json
import logging
import os
import re
import socket
from typing import Any

import httpx

from ..config.models import RecordConfig, ZoneConfig
from .errors import RESOLVER_TIMEOUT_SECONDS, ResolverError


def _evaluate(source: str, filename: str, record: RecordConfig, zone: ZoneConfig) -> Any:
    namespace: dict[str, Any] = {
        "__builtins__": builtins.__dict__,
        "os": os,
        "re": re,
        "json": json,
        "socket": socket,
        "ipaddress": ipaddress,
        "httpx": httpx,
        "record_name": record.name,
        "record_type": record.type,
        "zone": zone.domain,
    }
    try:
        expression = compile(source, filename, "eval")
    except SyntaxError:
        program = compile(source, filename, "exec")
        exec(program, namespace)
        if "result" not in namespace:
            raise ResolverError("eval code must assign a variable named result") from None
        return namespace["result"]
    return eval(expression, namespace)


async def resolve(
    record: RecordConfig, zone: ZoneConfig, *, filename: str = "<config:content.eval>"
) -> str:
    """Evaluate trusted code with a timeout spanning worker execution and awaiting."""
    logger = logging.getLogger(__name__)
    logger.debug("evaluating Python for %s: %.200s", record.name, record.content.source)
    try:
        async with asyncio.timeout(RESOLVER_TIMEOUT_SECONDS):
            result = await asyncio.to_thread(
                _evaluate, record.content.source, filename, record, zone
            )
            if inspect.isawaitable(result):
                result = await result
            if result is None:
                raise ResolverError("eval returned None")
            value = str(result).strip()
            logger.debug("eval raw output: %r", value)
            if not value:
                raise ResolverError("eval returned empty output")
            return value
    except TimeoutError as exc:
        logger.warning("eval timed out; a running worker thread cannot be killed and may continue")
        raise ResolverError(f"eval timed out after {RESOLVER_TIMEOUT_SECONDS}s") from exc
    except SyntaxError as exc:
        logger.debug("eval compilation failed", exc_info=True)
        raise ResolverError(f"eval syntax error at line {exc.lineno}: {exc.msg}") from exc
    except ResolverError:
        raise
    except asyncio.CancelledError:
        raise
    except BaseException as exc:
        # Trusted resolver code may call sys.exit(); that is still a record failure.
        logger.debug("eval failed", exc_info=True)
        raise ResolverError(f"eval {type(exc).__name__}: {exc}") from exc
