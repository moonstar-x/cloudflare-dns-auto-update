"""Async shell execution with process-group cleanup."""

import asyncio
import logging
import os
import signal
from contextlib import suppress
from time import monotonic

from ..config.models import RecordConfig, ZoneConfig
from .errors import RESOLVER_TIMEOUT_SECONDS, ResolverError


async def resolve(record: RecordConfig, zone: ZoneConfig) -> str:
    """Run a trusted shell script, capturing output and killing timed-out descendants."""
    script = record.content.source
    logger = logging.getLogger(__name__)
    logger.debug("running shell for %s: %.200s", record.name, script)
    started = monotonic()
    try:
        process = await asyncio.create_subprocess_exec(
            "/bin/sh",
            "-c",
            script,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            env={
                **os.environ,
                "CFDA_RECORD_NAME": record.name,
                "CFDA_RECORD_TYPE": record.type,
                "CFDA_ZONE": zone.domain,
            },
        )
    except OSError as exc:
        raise ResolverError(f"cannot start shell: {exc}") from exc
    try:
        async with asyncio.timeout(RESOLVER_TIMEOUT_SECONDS):
            stdout, stderr = await process.communicate()
    except (TimeoutError, asyncio.CancelledError) as exc:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.communicate()
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise ResolverError(f"shell timed out after {RESOLVER_TIMEOUT_SECONDS}s") from exc
    output = stdout.decode("utf-8", errors="replace").strip()
    error = stderr.decode("utf-8", errors="replace").strip()
    logger.debug(
        "shell exited code=%s duration=%.3fs stdout=%r stderr=%r",
        process.returncode,
        monotonic() - started,
        output,
        error,
    )
    if process.returncode != 0:
        raise ResolverError(f"shell exit code {process.returncode}: {error[:500]}")
    if error:
        logger.warning("shell succeeded with stderr: %.500s", error)
    if not output:
        raise ResolverError("shell returned empty output")
    return output
