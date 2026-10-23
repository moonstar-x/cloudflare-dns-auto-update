"""Concurrent, isolated, and idempotent DNS reconciliation."""

import asyncio
import logging
from dataclasses import dataclass
from time import monotonic
from typing import Any, Literal

from .cloudflare import CloudflareAPIError, CloudflareClient, DnsRecord
from .config.models import AppConfig, RecordConfig, ZoneConfig
from .diff import create_payload, diff_record
from .logging_setup import log_context
from .resolvers import ResolutionCache, resolve_content

type Outcome = Literal["created", "updated", "unchanged", "skipped", "failed"]


@dataclass(frozen=True)
class RecordOutcome:
    """Exactly one terminal result for a configured record."""

    zone: str
    name: str
    type: str
    outcome: Outcome
    reason: str = ""


@dataclass(frozen=True)
class CycleReport:
    """Per-record results, aggregate counters, and elapsed seconds."""

    outcomes: list[RecordOutcome]
    duration: float

    @property
    def created(self) -> int:
        """Count actual or dry-run creates."""
        return sum(item.outcome == "created" for item in self.outcomes)

    @property
    def updated(self) -> int:
        """Count actual or dry-run updates."""
        return sum(item.outcome == "updated" for item in self.outcomes)

    @property
    def unchanged(self) -> int:
        """Count already matching records."""
        return sum(item.outcome == "unchanged" for item in self.outcomes)

    @property
    def skipped(self) -> int:
        """Count intentionally unmanaged ambiguous or inaccessible records."""
        return sum(item.outcome == "skipped" for item in self.outcomes)

    @property
    def failed(self) -> int:
        """Count records that could not be processed."""
        return sum(item.outcome == "failed" for item in self.outcomes)


async def reconcile(
    config: AppConfig, client: CloudflareClient, *, dry_run: bool, cycle_id: str
) -> CycleReport:
    """Plan and apply a snapshot without allowing one zone or record to cancel another."""
    logger = logging.getLogger(__name__)
    started = monotonic()
    cache: ResolutionCache = {}
    semaphore = asyncio.Semaphore(8)
    results: dict[tuple[str, str, str], RecordOutcome] = {}

    def finish(zone: ZoneConfig, record: RecordConfig, outcome: Outcome, reason: str = "") -> None:
        results[(zone.domain, record.name, record.type)] = RecordOutcome(
            zone.domain, record.name, record.type, outcome, reason
        )

    async def resolve_one(
        zone: ZoneConfig, record: RecordConfig, zone_index: int, record_index: int
    ) -> str | None:
        with log_context(record=f"{record.name}/{record.type}"):
            tick = monotonic()
            try:
                content = await resolve_content(
                    record,
                    zone,
                    cache=cache,
                    semaphore=semaphore,
                    filename=f"<config:zones[{zone_index}].dns[{record_index}].content.eval>",
                )
                logger.info(
                    "%s/%s → %s (via %s, %.0fms)",
                    record.name,
                    record.type,
                    content,
                    record.content.kind,
                    (monotonic() - tick) * 1000,
                )
                return content
            except Exception as exc:
                logger.error("resolution failed: %s", exc)
                logger.debug("resolver traceback", exc_info=True)
                finish(zone, record, "failed", str(exc))
                return None

    async def apply(
        zone: ZoneConfig,
        zone_id: str,
        record: RecordConfig,
        existing: DnsRecord | None,
        payload: dict[str, Any],
    ) -> None:
        with log_context(record=f"{record.name}/{record.type}"):
            outcome: Literal["created", "updated"] = "created" if existing is None else "updated"
            action = "create" if existing is None else "update"
            logger.info("%s%s payload=%s", "[dry-run] " if dry_run else "", action, payload)
            try:
                if not dry_run:
                    saved = (
                        await client.create_dns_record(zone_id, payload)
                        if existing is None
                        else await client.patch_dns_record(zone_id, existing.id, payload)
                    )
                    logger.info("%s succeeded record ID=%s", action, saved.id)
                finish(zone, record, outcome)
            except Exception as exc:
                logger.error("%s failed: %s", action, exc)
                finish(zone, record, "failed", str(exc))

    async def process_zone(zone: ZoneConfig, zone_index: int) -> None:
        with log_context(zone=zone.domain):
            try:
                try:
                    zone_id = await client.get_zone_id(zone.domain)
                except CloudflareAPIError as exc:
                    if exc.status_code not in (401, 403, 404):
                        raise
                    zone_id = None
                if zone_id is None:
                    logger.error("zone not found or not accessible: %s", zone.domain)
                    for record in zone.dns:
                        finish(zone, record, "skipped", "zone not found or not accessible")
                    return
                current = await client.list_dns_records(zone_id)
                index: dict[tuple[str, str], list[DnsRecord]] = {}
                for item in current:
                    index.setdefault(
                        (item.name.lower().removesuffix("."), item.type.upper()), []
                    ).append(item)
                contents = await asyncio.gather(
                    *(
                        resolve_one(zone, record, zone_index, position)
                        for position, record in enumerate(zone.dns)
                    )
                )
                plans: list[tuple[RecordConfig, DnsRecord | None, dict[str, Any]]] = []
                for record, content in zip(zone.dns, contents, strict=True):
                    if content is None:
                        continue
                    with log_context(record=f"{record.name}/{record.type}"):
                        matches = index.get((record.name, record.type), [])
                        if len(matches) > 1:
                            reason = f"ambiguous matches: {[(x.id, x.content) for x in matches]}"
                            logger.warning(reason)
                            finish(zone, record, "skipped", reason)
                        elif not matches:
                            logger.info("diff: record missing; create planned")
                            plans.append((record, None, create_payload(record, content)))
                        else:
                            difference = diff_record(record, content, matches[0])
                            if difference.ttl_ignored:
                                logger.warning("TTL ignored for proxied record")
                            if difference.payload:
                                logger.info("diff: %s", difference.description)
                                plans.append((record, matches[0], difference.payload))
                            else:
                                logger.info("unchanged")
                                finish(zone, record, "unchanged")
                async with asyncio.TaskGroup() as tasks:
                    for record, existing, payload in plans:
                        tasks.create_task(apply(zone, zone_id, record, existing, payload))
            except Exception as exc:
                logger.exception("zone failed: %s", exc)
                for record in zone.dns:
                    if (zone.domain, record.name, record.type) not in results:
                        finish(zone, record, "failed", str(exc))

    with log_context(cycle=cycle_id):
        logger.info("cycle started: zones=%s records=%s", len(config.zones), config.record_count)
        try:
            async with asyncio.TaskGroup() as tasks:
                for position, zone in enumerate(config.zones):
                    tasks.create_task(process_zone(zone, position))
        finally:
            # A cancelled cycle must also clean up shared resolver tasks and their subprocesses.
            pending = [task for task in cache.values() if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        report = CycleReport(
            [results[(z.domain, r.name, r.type)] for z in config.zones for r in z.dns],
            monotonic() - started,
        )
        logger.info(
            "cycle finished in %.2fs: created=%s updated=%s unchanged=%s skipped=%s failed=%s",
            report.duration,
            report.created,
            report.updated,
            report.unchanged,
            report.skipped,
            report.failed,
        )
        return report
