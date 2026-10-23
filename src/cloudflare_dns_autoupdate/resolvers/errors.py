"""Common resolver errors and limits."""

RESOLVER_TIMEOUT_SECONDS = 30


class ResolverError(Exception):
    """A content source could not produce a usable value."""
