"""UTC structured logging, task-local context, and credential redaction."""

import json
import logging
import sys
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

cycle_id: ContextVar[str | None] = ContextVar("cycle_id", default=None)
zone: ContextVar[str | None] = ContextVar("zone", default=None)
record: ContextVar[str | None] = ContextVar("record", default=None)


@contextmanager
def log_context(**values: str) -> Iterator[None]:
    """Scope cycle, zone, and record context to the current async task."""
    variables = {"cycle": cycle_id, "zone": zone, "record": record}
    tokens = [(variables[key], variables[key].set(value)) for key, value in values.items()]
    try:
        yield
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)


class ContextRedactionFilter(logging.Filter):
    """Inject task context and redact known credentials, including tracebacks."""

    def __init__(self) -> None:
        super().__init__()
        self._secrets: set[str] = set()

    def add_token(self, value: str) -> None:
        """Remember credentials across rotations so late logs stay redacted."""
        if value:
            self._secrets.add(value)

    def redact(self, value: str) -> str:
        """Remove every known token occurrence."""
        for secret in sorted(self._secrets, key=len, reverse=True):
            value = value.replace(secret, "***")
        return value

    def _redact_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, dict):
            return {self.redact(str(key)): self._redact_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._redact_value(item) for item in value]
        return value

    def filter(self, entry: logging.LogRecord) -> bool:
        """Render arguments once, then sanitize the message, context, and exception."""
        entry.msg = self.redact(entry.getMessage())
        entry.args = ()
        for key, value in (
            ("cycle", cycle_id.get()),
            ("zone", zone.get()),
            ("record", record.get()),
        ):
            if value is not None and not hasattr(entry, key):
                setattr(entry, key, value)
        for key, value in list(vars(entry).items()):
            if key not in {"exc_info", "args"}:
                setattr(entry, key, self._redact_value(value))
        if entry.exc_info:
            entry.exc_text = self.redact("".join(traceback.format_exception(*entry.exc_info)))
        return True


def _timestamp(entry: logging.LogRecord) -> str:
    return (
        datetime.fromtimestamp(entry.created, UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class TextFormatter(logging.Formatter):
    """Human-readable UTC log lines with task context."""

    def format(self, entry: logging.LogRecord) -> str:
        """Format a line and any previously redacted traceback."""
        context = " ".join(
            f"[{key}={getattr(entry, key)}]"
            for key in ("cycle", "zone", "record")
            if hasattr(entry, key)
        )
        text = (
            f"{_timestamp(entry)} {entry.levelname:<5} [{entry.name.rsplit('.', 1)[-1]}] "
            f"{context + ' ' if context else ''}{entry.getMessage()}"
        )
        if entry.exc_text:
            text += f"\n{entry.exc_text}"
        return text


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per line, including structured extras."""

    def format(self, entry: logging.LogRecord) -> str:
        """Serialize the message, task context, extras, and optional traceback."""
        standard = vars(logging.LogRecord("", 0, "", 0, "", (), None))
        result = {
            "timestamp": _timestamp(entry),
            "level": entry.levelname,
            "logger": entry.name,
            "message": entry.getMessage(),
        }
        result.update({key: value for key, value in vars(entry).items() if key not in standard})
        if entry.exc_text:
            result["exception"] = entry.exc_text
        return json.dumps(result, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO", log_format: str = "text") -> ContextRedactionFilter:
    """Configure stdout logging and return the application's redaction state."""
    handler = logging.StreamHandler(sys.stdout)
    redaction = ContextRedactionFilter()
    handler.addFilter(redaction)
    handler.setFormatter(JsonFormatter() if log_format == "json" else TextFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    logging.getLogger("httpx").setLevel(logging.INFO if level == "DEBUG" else logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    return redaction
