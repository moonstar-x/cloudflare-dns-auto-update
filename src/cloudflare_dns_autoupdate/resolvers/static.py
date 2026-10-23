"""Static scalar content resolver."""

from ..config.models import RecordConfig, ZoneConfig


async def resolve(record: RecordConfig, zone: ZoneConfig) -> str:
    """Return the configured static content unchanged."""
    assert record.content.ip is not None
    return record.content.ip
