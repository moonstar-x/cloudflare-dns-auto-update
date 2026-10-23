"""Pure DNS value validation shared by configuration, resolvers, and diffing."""

import ipaddress
import re


def normalize_hostname(value: str, *, wildcard: bool = False) -> str:
    """Validate a DNS hostname and return its lowercase, absolute-name-free form."""
    normalized = value.removesuffix(".").lower()
    candidate = normalized.removeprefix("*.") if wildcard else normalized
    if not candidate or len(normalized) > 253:
        raise ValueError("hostname must contain 1 to 253 characters")
    for label in candidate.split("."):
        if (
            not re.fullmatch(r"[a-z0-9_\-]{1,63}", label)
            or label.startswith("-")
            or label.endswith("-")
        ):
            raise ValueError(f"invalid hostname label {label!r}")
    return normalized


def validate_content(record_type: str, value: str) -> str:
    """Validate and normalize scalar DNS content, including non-global IP addresses."""
    match record_type.upper():
        case "A" | "AAAA":
            address = (
                ipaddress.IPv4Address(value)
                if record_type.upper() == "A"
                else ipaddress.IPv6Address(value)
            )
            return str(address)
        case "CNAME" | "NS" | "PTR":
            return normalize_hostname(value)
        case "TXT":
            if not value or len(value) > 2048:
                raise ValueError("TXT content must contain 1 to 2048 characters")
            return value
        case _:
            raise ValueError("supported record types: A, AAAA, CNAME, TXT, NS, PTR")
