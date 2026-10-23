import logging

import pytest
import yaml
from conftest import config_data

from cloudflare_dns_autoupdate.config.loader import ConfigError, load_config, parse_config


def test_token_sources(caplog):
    caplog.set_level(logging.INFO)
    document = config_data(token=" file-token ")
    config = parse_config(
        yaml.safe_dump(document).encode(), environ={"CLOUDFLARE_TOKEN": "env-token"}
    )
    assert config.token.get_secret_value() == "file-token"
    assert "takes precedence" in caplog.text
    assert "token source: config file" in caplog.text
    assert "file-token" not in caplog.text
    document["token"] = "   "
    config = parse_config(yaml.safe_dump(document).encode(), environ={"CLOUDFLARE_TOKEN": " env "})
    assert config.token.get_secret_value() == "env"
    assert "token source: environment (CLOUDFLARE_TOKEN)" in caplog.text


@pytest.mark.parametrize(
    "data", [b"", b"[]", b"null", b"token: [", b"token: !!python/object:bad {}"]
)
def test_bad_yaml(data):
    with pytest.raises(ConfigError):
        parse_config(data, environ={})


def test_missing_token():
    document = config_data(token=" ")
    with pytest.raises(ConfigError, match="CLOUDFLARE_TOKEN"):
        parse_config(yaml.safe_dump(document).encode(), environ={})


def test_readable_errors_and_secret_redaction():
    document = config_data()
    document["zones"][0]["dns"][0]["content"] = {"ip": "x", "shell": "y"}
    with pytest.raises(ConfigError, match=r"zones\[0\].dns\[0\].content: exactly one"):
        parse_config(yaml.safe_dump(document).encode(), environ={})
    document = config_data()
    document["interval"] = document["token"]
    with pytest.raises(ConfigError) as caught:
        parse_config(yaml.safe_dump(document).encode(), environ={})
    assert document["token"] not in str(caught.value)


async def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="cannot read config"):
        await load_config(tmp_path / "absent.yml")


async def test_permission_error(tmp_path, monkeypatch):
    from pathlib import Path

    def denied(self):
        raise PermissionError(13, "permission denied")

    monkeypatch.setattr(Path, "read_bytes", denied)
    with pytest.raises(ConfigError, match="permission denied"):
        await load_config(tmp_path / "config.yml")


def test_low_ttl_warning(caplog):
    document = config_data()
    document["zones"][0]["dns"][0]["ttl"] = 30
    parse_config(yaml.safe_dump(document).encode(), environ={})
    assert "Enterprise" in caplog.text
