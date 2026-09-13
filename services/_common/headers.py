"""HTTP header utilities for service proxies."""

from collections.abc import Mapping

HOP_BY_HOP_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "connection",
        "transfer-encoding",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "upgrade",
    }
)


def strip_hop_by_hop_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Return a new dict with hop-by-hop HTTP headers stripped for proxy forwarding."""
    return {k: v for k, v in headers.items() if k.lower() not in HOP_BY_HOP_HEADERS}
