"""Unit tests for services/_common/headers.py."""

from services._common.headers import HOP_BY_HOP_HEADERS, strip_hop_by_hop_headers


def test_strip_hop_by_hop_headers() -> None:
    headers = {
        "Host": "edge-gateway.ust-prod:8080",
        "Content-Length": "123",
        "Connection": "keep-alive",
        "Transfer-Encoding": "chunked",
        "Keep-Alive": "timeout=5",
        "Proxy-Authenticate": "basic",
        "Proxy-Authorization": "basic xyz",
        "TE": "trailers",
        "Trailers": "X-Custom",
        "Upgrade": "websocket",
        "Authorization": "Bearer token",
        "X-Understudy-Incident": "inc_123",
        "X-Custom-Header": "custom-val",
    }
    stripped = strip_hop_by_hop_headers(headers)

    # All hop-by-hop headers should be gone
    for h in HOP_BY_HOP_HEADERS:
        assert h not in {k.lower() for k in stripped}

    # Preserved application headers
    assert stripped["Authorization"] == "Bearer token"
    assert stripped["X-Understudy-Incident"] == "inc_123"
    assert stripped["X-Custom-Header"] == "custom-val"


def test_strip_hop_by_hop_headers_empty() -> None:
    assert strip_hop_by_hop_headers({}) == {}
