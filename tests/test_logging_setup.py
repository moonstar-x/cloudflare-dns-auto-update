import io
import json
import logging

import pytest

from cloudflare_dns_autoupdate.logging_setup import (
    ContextRedactionFilter,
    JsonFormatter,
    TextFormatter,
    log_context,
)


@pytest.mark.parametrize("formatter", [TextFormatter(), JsonFormatter()])
def test_context_redaction_traceback_and_rotation(formatter):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    redaction = ContextRedactionFilter()
    redaction.add_token("old-secret")
    redaction.add_token("new-secret")
    handler.addFilter(redaction)
    handler.setFormatter(formatter)
    logger = logging.Logger("test", level=logging.DEBUG)
    logger.addHandler(handler)
    with log_context(cycle="abcd1234", zone="example.com", record="home.example.com/A"):
        try:
            raise ValueError("old-secret new-secret")
        except ValueError:
            logger.exception("credential %s", "new-secret", extra={"action": "old-secret"})
    text = stream.getvalue()
    assert "old-secret" not in text and "new-secret" not in text
    assert "***" in text and "abcd1234" in text
    if isinstance(formatter, JsonFormatter):
        item = json.loads(text)
        assert item["cycle"] == "abcd1234"
        assert item["zone"] == "example.com"
        assert item["record"] == "home.example.com/A"
        assert item["action"] == "***"
        assert "ValueError" in item["exception"]
    logger.info("outside context")
    assert "abcd1234" not in stream.getvalue().splitlines()[-1]
