"""Shared small configurations and Cloudflare response helpers."""

from typing import Any

import pytest

from cloudflare_dns_autoupdate.config.models import AppConfig, RecordConfig, ZoneConfig


def record_data(**changes: Any) -> dict[str, Any]:
    """Build a valid record document with targeted overrides."""
    return {
        "name": "home.example.com",
        "type": "A",
        "content": {"ip": "203.0.113.10"},
        **changes,
    }


def config_data(**changes: Any) -> dict[str, Any]:
    """Build a valid complete document."""
    return {
        "token": "test-secret-token",
        "zones": [{"domain": "example.com", "dns": [record_data()]}],
        **changes,
    }


def api_record(**changes: Any) -> dict[str, Any]:
    """Build an API record, including optional provider metadata."""
    return {
        "id": "record-1",
        "name": "home.example.com",
        "type": "A",
        "content": "203.0.113.10",
        "ttl": 1,
        "proxied": False,
        "comment": None,
        "tags": [],
        **changes,
    }


def envelope(result: Any, **changes: Any) -> dict[str, Any]:
    """Build a successful Cloudflare response."""
    return {"success": True, "errors": [], "messages": [], "result": result, **changes}


@pytest.fixture
def config() -> AppConfig:
    return AppConfig.model_validate(config_data())


@pytest.fixture
def record(config: AppConfig) -> RecordConfig:
    return config.zones[0].dns[0]


@pytest.fixture
def zone(config: AppConfig) -> ZoneConfig:
    return config.zones[0]
