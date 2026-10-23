"""Bounded, retrying HTTPX client that only reads, creates, or patches DNS records."""

import asyncio
import logging
import os
import random
from contextlib import suppress
from time import monotonic
from typing import Any, Literal, Self

import httpx
from pydantic import TypeAdapter, ValidationError

from .. import __version__
from .errors import CloudflareAPIError
from .models import APIErrorDetail, CloudflareEnvelope, DnsRecord, PageInfo, ZoneResult


class CloudflareClient:
    """Own a single HTTP session, four-request limit, and generation-local zone cache."""

    def __init__(self, token: str, *, base_url: str | None = None) -> None:
        self._http = httpx.AsyncClient(
            base_url=(
                base_url
                or os.getenv("CLOUDFLARE_API_BASE_URL")
                or "https://api.cloudflare.com/client/v4"
            ).rstrip("/")
            + "/",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": f"cloudflare-dns-autoupdate/{__version__}",
            },
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
        self._semaphore = asyncio.Semaphore(4)
        self._zone_ids: dict[str, str | None] = {}
        self._logger = logging.getLogger(__name__)

    async def __aenter__(self) -> Self:
        """Return this already-created client."""
        return self

    async def __aexit__(self, *_: object) -> None:
        """Close the HTTP session."""
        await self.aclose()

    async def aclose(self) -> None:
        """Release all HTTP connections."""
        await self._http.aclose()

    def clear_zone_cache(self) -> None:
        """Invalidate zone identities after every successful config reload."""
        self._zone_ids.clear()

    async def _request[T](
        self,
        method: Literal["GET", "POST", "PATCH"],
        path: str,
        adapter: TypeAdapter[T],
        *,
        params: dict[str, str | int] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> tuple[T, PageInfo | None]:
        for attempt in range(1, 4):
            started = monotonic()
            response: httpx.Response | None = None
            try:
                async with self._semaphore:
                    response = await self._http.request(
                        method, path.lstrip("/"), params=params, json=payload
                    )
                self._logger.debug(
                    "HTTP %s %s query=%s status=%s duration=%.3fs attempt=%s",
                    method,
                    path,
                    params,
                    response.status_code,
                    monotonic() - started,
                    attempt,
                )
            except httpx.TransportError as exc:
                self._logger.debug(
                    "HTTP %s %s query=%s transport=%s duration=%.3fs attempt=%s",
                    method,
                    path,
                    params,
                    type(exc).__name__,
                    monotonic() - started,
                    attempt,
                )
                if attempt == 3:
                    raise CloudflareAPIError(
                        None, [APIErrorDetail(code=-1, message=str(exc))], method, path
                    ) from exc
            else:
                if response.status_code in (401, 403):
                    self._logger.error(
                        "HTTP %s: check token permissions: Zone:Read and DNS:Edit",
                        response.status_code,
                    )
                retryable = response.status_code == 429 or response.status_code >= 500
                if not retryable or attempt == 3:
                    try:
                        envelope = CloudflareEnvelope.model_validate(response.json())
                    except (ValidationError, ValueError) as exc:
                        raise CloudflareAPIError(
                            response.status_code,
                            [
                                APIErrorDetail(
                                    code=-1, message="invalid Cloudflare response envelope"
                                )
                            ],
                            method,
                            path,
                        ) from exc
                    if not response.is_success or not envelope.success:
                        raise CloudflareAPIError(
                            response.status_code, envelope.errors, method, path
                        )
                    try:
                        return adapter.validate_python(envelope.result), envelope.result_info
                    except ValidationError as exc:
                        raise CloudflareAPIError(
                            response.status_code,
                            [APIErrorDetail(code=-1, message="invalid Cloudflare result")],
                            method,
                            path,
                        ) from exc
            delay = (2 ** (attempt - 1)) * random.uniform(0.75, 1.25)
            if response is not None and (retry_after := response.headers.get("Retry-After")):
                with suppress(ValueError):
                    delay = min(60.0, max(0.0, float(retry_after)))
            self._logger.warning(
                "retrying %s %s after %s in %.2fs (attempt %s/3)",
                method,
                path,
                response.status_code if response is not None else "network error",
                delay,
                attempt + 1,
            )
            await asyncio.sleep(delay)
        raise AssertionError("request attempts exhausted")

    async def verify_token(self) -> None:
        """Log token verification without making it a startup prerequisite."""
        try:
            result, _ = await self._request(
                "GET", "/user/tokens/verify", TypeAdapter(dict[str, Any])
            )
            self._logger.info("token verification status: %s", result.get("status", "unknown"))
        except CloudflareAPIError as exc:
            self._logger.warning(
                "token verification failed (account-owned tokens may fail): %s", exc
            )

    async def get_zone_id(self, domain: str) -> str | None:
        """Look up an accessible zone, caching positive and negative results."""
        if domain in self._zone_ids:
            self._logger.debug("zone ID lookup (cached): %s → %s", domain, self._zone_ids[domain])
            return self._zone_ids[domain]
        result, _ = await self._request(
            "GET", "/zones", TypeAdapter(list[ZoneResult]), params={"name": domain}
        )
        zone_id = result[0].id if len(result) == 1 else None
        self._zone_ids[domain] = zone_id
        self._logger.debug("zone ID lookup (fetched): %s → %s", domain, zone_id)
        return zone_id

    async def list_dns_records(self, zone_id: str) -> list[DnsRecord]:
        """Fetch every page of DNS records from a zone."""
        started = monotonic()
        records: list[DnsRecord] = []
        page = 1
        while True:
            result, info = await self._request(
                "GET",
                f"/zones/{zone_id}/dns_records",
                TypeAdapter(list[DnsRecord]),
                params={"per_page": 100, "page": page},
            )
            records.extend(result)
            if info is None or page >= info.total_pages:
                break
            page += 1
        self._logger.info(
            "records fetched: count=%s pages=%s duration=%.3fs",
            len(records),
            page,
            monotonic() - started,
        )
        return records

    @staticmethod
    def _check_payload(payload: dict[str, Any], *, create: bool) -> None:
        allowed = {"content", "ttl", "comment", "tags"}
        if create:
            allowed |= {"type", "name"}
        if extra := payload.keys() - allowed:
            raise ValueError(f"unmanaged DNS fields are forbidden: {sorted(extra)}")

    async def create_dns_record(self, zone_id: str, payload: dict[str, Any]) -> DnsRecord:
        """Create a record without specifying any unmanaged fields."""
        self._check_payload(payload, create=True)
        result, _ = await self._request(
            "POST", f"/zones/{zone_id}/dns_records", TypeAdapter(DnsRecord), payload=payload
        )
        return result

    async def patch_dns_record(
        self, zone_id: str, record_id: str, payload: dict[str, Any]
    ) -> DnsRecord:
        """Patch only managed, changed fields; preserve explicit null and empty lists."""
        self._check_payload(payload, create=False)
        result, _ = await self._request(
            "PATCH",
            f"/zones/{zone_id}/dns_records/{record_id}",
            TypeAdapter(DnsRecord),
            payload=payload,
        )
        return result
