"""Read YAML configuration and resolve the Cloudflare token without exposing it."""

import asyncio
import hashlib
import logging
import os
from collections.abc import Mapping
from pathlib import Path

import yaml
from pydantic import ValidationError

from .models import AppConfig


class ConfigError(Exception):
    """A readable configuration failure safe to show to an operator."""


def _error_path(location: tuple[str | int, ...]) -> str:
    result = ""
    for part in location:
        result += f"[{part}]" if isinstance(part, int) else f".{part}" if result else part
    return result or "config"


def parse_config(data: bytes, *, environ: Mapping[str, str] | None = None) -> AppConfig:
    """Safely parse YAML bytes, resolve credentials, and validate every field."""
    logger = logging.getLogger(__name__)
    try:
        document = yaml.safe_load(data)
    except (yaml.YAMLError, UnicodeError) as exc:
        # PyYAML exceptions embed source lines, which can contain credentials.
        mark = getattr(exc, "problem_mark", None)
        position = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise ConfigError(f"invalid YAML{position}") from None
    if not isinstance(document, dict):
        raise ConfigError("config must be a non-empty YAML mapping")
    env = os.environ if environ is None else environ
    configured = document.get("token")
    environment = env.get("CLOUDFLARE_TOKEN", "").strip()
    if configured is not None and not isinstance(configured, str):
        raise ConfigError("token: must be a string")
    token = configured.strip() if isinstance(configured, str) else ""
    if token:
        source = "config file"
        if environment:
            logger.warning("config file token takes precedence over CLOUDFLARE_TOKEN")
    elif environment:
        token, source = environment, "environment (CLOUDFLARE_TOKEN)"
    else:
        raise ConfigError("token: set token in the config or CLOUDFLARE_TOKEN in the environment")
    document["token"] = token
    try:
        config = AppConfig.model_validate(document)
    except ValidationError as exc:
        errors = []
        for error in exc.errors(include_input=False, include_url=False):
            message = error["msg"].removeprefix("Value error, ")
            # A validation message or location may include user-controlled strings.
            line = f"{_error_path(error['loc'])}: {message}"
            for secret in (token, environment):
                if secret:
                    line = line.replace(secret, "***")
            errors.append(line)
        raise ConfigError("\n".join(errors)) from None
    logger.info("token source: %s", source)
    for zone in config.zones:
        for record in zone.dns:
            if 1 < record.ttl < 60:
                identity = f"{record.name}/{record.type}"
                for secret in (token, environment):
                    if secret:
                        identity = identity.replace(secret, "***")
                logger.warning("%s TTL %s requires an Enterprise zone", identity, record.ttl)
    return config


async def load_config(path: Path) -> AppConfig:
    """Read a small config file off the event loop and wrap all expected failures."""
    config, _ = await load_config_snapshot(path)
    return config


async def load_config_snapshot(path: Path) -> tuple[AppConfig, str]:
    """Load configuration and its hash from exactly the same bytes."""
    try:
        data = await asyncio.to_thread(path.read_bytes)
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc.strerror}") from exc
    try:
        return parse_config(data), hashlib.sha256(data).hexdigest()
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from None
