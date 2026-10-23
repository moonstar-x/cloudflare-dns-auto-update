"""Cloudflare request failures with method, path, and provider error codes."""

from .models import APIErrorDetail


class CloudflareAPIError(Exception):
    """A transport, protocol, HTTP, or Cloudflare envelope failure."""

    def __init__(
        self, status_code: int | None, errors: list[APIErrorDetail], method: str, path: str
    ) -> None:
        self.status_code = status_code
        self.errors = errors
        self.method = method
        self.path = path
        details = "; ".join(f"{error.code}: {error.message}" for error in errors)
        super().__init__(f"{method} {path} (HTTP {status_code}): {details}")
