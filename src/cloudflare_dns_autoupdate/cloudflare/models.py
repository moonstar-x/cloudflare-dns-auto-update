"""Typed Cloudflare envelopes and DNS records; unmanaged API fields are ignored."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class APIErrorDetail(BaseModel):
    """A Cloudflare error code and readable explanation."""

    code: int
    message: str


class PageInfo(BaseModel):
    """Pagination metadata returned by list endpoints."""

    total_pages: int = 1


class CloudflareEnvelope(BaseModel):
    """The common response envelope, including failure responses."""

    success: bool
    errors: list[APIErrorDetail] = Field(default_factory=list)
    messages: list[Any] = Field(default_factory=list)
    result: Any = None
    result_info: PageInfo | None = None


class DnsRecord(BaseModel):
    """The subset of Cloudflare record state needed for reconciliation."""

    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    type: str
    content: str
    ttl: int
    proxied: bool | None = None
    comment: str | None = None
    tags: list[str] | None = None


class ZoneResult(BaseModel):
    """The identity returned by a zone lookup."""

    id: str
