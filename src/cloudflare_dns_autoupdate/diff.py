"""Pure comparisons and write payloads for the four managed DNS fields."""

from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from .cloudflare.models import DnsRecord
from .config.models import RecordConfig
from .validation import validate_content


@dataclass(frozen=True)
class RecordDiff:
    """A minimal PATCH payload and its explanation, including TTL warnings."""

    payload: dict[str, Any]
    description: str
    ttl_ignored: bool = False


def diff_record(desired: RecordConfig, content: str, existing: DnsRecord) -> RecordDiff:
    """Compare normalized DNS values, comments, tag sets, and effective TTLs."""
    payload: dict[str, Any] = {}
    changes: list[str] = []
    current_content = existing.content
    if (
        desired.type == "TXT"
        and len(current_content) >= 2
        and current_content.startswith('"')
        and current_content.endswith('"')
    ):
        current_content = current_content[1:-1]
    # Malformed existing state should be repaired, rather than blocking the record.
    with suppress(ValueError):
        current_content = validate_content(desired.type, current_content)
    content = validate_content(desired.type, content)
    if current_content != content:
        payload["content"] = content
        changes.append(f"content: {existing.content} → {content}")
    ttl_ignored = bool(existing.proxied and desired.ttl != 1)
    if not ttl_ignored and existing.ttl != desired.ttl:
        payload["ttl"] = desired.ttl
        changes.append(
            f"ttl: {'Auto' if existing.ttl == 1 else existing.ttl} → "
            f"{'Auto' if desired.ttl == 1 else desired.ttl}"
        )
    if (existing.comment or "") != desired.comment:
        payload["comment"] = desired.comment or None
        changes.append(f"comment: {existing.comment or ''!r} → {desired.comment!r}")
    if set(existing.tags or []) != set(desired.tags):
        payload["tags"] = desired.tags
        changes.append(f"tags: {existing.tags or []} → {desired.tags}")
    return RecordDiff(payload, "; ".join(changes), ttl_ignored)


def create_payload(record: RecordConfig, content: str) -> dict[str, Any]:
    """Build a create payload, omitting empty optional fields and unmanaged fields."""
    payload: dict[str, Any] = {
        "type": record.type,
        "name": record.name,
        "content": content,
        "ttl": record.ttl,
    }
    if record.comment:
        payload["comment"] = record.comment
    if record.tags:
        payload["tags"] = record.tags
    return payload
