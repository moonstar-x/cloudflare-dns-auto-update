"""Immutable, validated service configuration."""

from datetime import timedelta
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ModelWrapValidatorHandler,
    PrivateAttr,
    SecretStr,
    field_validator,
    model_validator,
)

from ..validation import normalize_hostname, validate_content
from .interval import parse_interval

type RecordType = Literal["A", "AAAA", "CNAME", "TXT", "NS", "PTR"]


class ConfigModel(BaseModel):
    """Reject unknown configuration keys and accidental field assignment."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ContentConfig(ConfigModel):
    """Exactly one trusted content source."""

    ip: str | None = Field(default=None, strict=True)
    shell: str | None = Field(default=None, strict=True)
    eval: str | None = Field(default=None, strict=True)

    @model_validator(mode="after")
    def one_source(self) -> Self:
        """Reject missing, null, or multiple source keys."""
        keys = self.model_fields_set & {"ip", "shell", "eval"}
        if len(keys) != 1 or getattr(self, next(iter(keys), "ip")) is None:
            supplied = ", ".join(sorted(keys)) or "none"
            raise ValueError(f"exactly one of 'shell', 'ip', 'eval' must be set (got: {supplied})")
        return self

    @property
    def kind(self) -> Literal["ip", "shell", "eval"]:
        """Return the configured source kind."""
        if self.ip is not None:
            return "ip"
        return "shell" if self.shell is not None else "eval"

    @property
    def source(self) -> str:
        """Return the selected source text."""
        value = getattr(self, self.kind)
        assert isinstance(value, str)
        return value


class RecordConfig(ConfigModel):
    """One uniquely identified DNS record and its managed fields."""

    name: str = Field(strict=True)
    type: RecordType
    content: ContentConfig
    ttl: int = 1
    tags: list[Annotated[str, Field(strict=True)]] = Field(default_factory=list)
    comment: str = Field(default="", max_length=100, strict=True)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        """Normalize record names, allowing a leading wildcard label."""
        return normalize_hostname(value, wildcard=True)

    @field_validator("type", mode="before")
    @classmethod
    def normalize_type(cls, value: object) -> str:
        """Normalize the type and explain the supported subset on failure."""
        if not isinstance(value, str) or value.upper() not in (
            "A",
            "AAAA",
            "CNAME",
            "TXT",
            "NS",
            "PTR",
        ):
            raise ValueError("supported record types: A, AAAA, CNAME, TXT, NS, PTR")
        return value.upper()

    @field_validator("ttl", mode="before")
    @classmethod
    def normalize_ttl(cls, value: object) -> int:
        """Translate Auto to the API sentinel, with strict numeric bounds."""
        if isinstance(value, str) and value.lower() == "auto":
            return 1
        if type(value) is not int or not 30 <= value <= 86400:
            raise ValueError("ttl must be Auto or an integer between 30 and 86400")
        return value

    @field_validator("tags")
    @classmethod
    def unique_tags(cls, value: list[str]) -> list[str]:
        """Reject duplicates and empty tag names."""
        if len(value) != len(set(value)):
            raise ValueError("duplicate tags are not allowed")
        if any(not tag or not tag.split(":", 1)[0] for tag in value):
            raise ValueError("tags must have the form name or name:value")
        return value

    @model_validator(mode="after")
    def validate_static(self) -> Self:
        """Fail immediately for invalid static content."""
        if self.content.ip is not None:
            validate_content(self.type, self.content.ip)
        return self


class ZoneConfig(ConfigModel):
    """A zone and its nonempty set of unique records."""

    domain: str = Field(strict=True)
    dns: list[RecordConfig] = Field(min_length=1)

    @field_validator("domain")
    @classmethod
    def normalize_domain(cls, value: str) -> str:
        """Normalize the zone apex."""
        return normalize_hostname(value)

    @model_validator(mode="after")
    def validate_records(self) -> Self:
        """Keep records within the zone and reject duplicate identities."""
        seen: set[tuple[str, str]] = set()
        for record in self.dns:
            if record.name != self.domain and not record.name.endswith(f".{self.domain}"):
                raise ValueError(f"record {record.name!r} must belong to zone {self.domain!r}")
            key = (record.name, record.type)
            if key in seen:
                raise ValueError(f"duplicate record {record.name}/{record.type}")
            seen.add(key)
        return self


class AppConfig(ConfigModel):
    """One complete configuration generation."""

    token: SecretStr
    interval: Annotated[timedelta, BeforeValidator(parse_interval)] = timedelta(minutes=5)
    zones: list[ZoneConfig] = Field(min_length=1)
    _interval_original: str = PrivateAttr(default="5m")

    @model_validator(mode="wrap")
    @classmethod
    def keep_interval_label(cls, value: Any, handler: ModelWrapValidatorHandler[Self]) -> Self:
        """Keep the user's interval spelling for operator-facing logs."""
        model = handler(value)
        if isinstance(value, dict):
            model._interval_original = str(value.get("interval", "5m"))
        return model

    @model_validator(mode="after")
    def unique_zones(self) -> Self:
        """Reject duplicate normalized zone names."""
        domains = [zone.domain for zone in self.zones]
        if len(domains) != len(set(domains)):
            raise ValueError("duplicate zone domains are not allowed")
        return self

    @property
    def interval_original(self) -> str:
        """Return the original interval input."""
        return self._interval_original

    @property
    def record_count(self) -> int:
        """Return the total number of configured records."""
        return sum(len(zone.dns) for zone in self.zones)
