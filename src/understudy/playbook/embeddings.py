"""OpenRouter-compatible vector embedding client."""

import math
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from understudy.common.config import Settings, get_settings
from understudy.common.errors import PlaybookEmbeddingError
from understudy.common.logging import get_logger

logger = get_logger(__name__)


def normalize_vector(vec: list[float], target_dim: int = 1024) -> list[float]:
    """Truncate or pad vector to target dimension and normalize to unit length (L2 norm)."""
    if len(vec) > target_dim:
        adjusted = vec[:target_dim]
    elif len(vec) < target_dim:
        adjusted = list(vec) + [0.0] * (target_dim - len(vec))
    else:
        adjusted = list(vec)

    norm = math.sqrt(sum(x * x for x in adjusted)) or 1.0
    return [float(x) / norm for x in adjusted]


class OpenRouterEmbeddingClient:
    """Embeds incident text signatures into 1024-dimensional vectors via OpenRouter."""

    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.AsyncClient | None = None,
        embed_caller: Callable[[str], Awaitable[list[float]]] | None = None,
        target_dim: int = 1024,
    ) -> None:
        self.settings = settings or get_settings()
        self._client = client
        self._custom_caller = embed_caller
        self.target_dim = target_dim

    async def embed(self, text: str) -> list[float]:
        """Generate a normalized 1024-dimension float embedding for input text."""
        if self._custom_caller is not None:
            raw = await self._custom_caller(text)
            return normalize_vector(raw, target_dim=self.target_dim)

        api_key = self.settings.secrets.openrouter_api_key
        if not api_key:
            raise PlaybookEmbeddingError(
                "OPENROUTER_API_KEY is not configured in settings or environment",
                details={"setting": "secrets.openrouter_api_key"},
            )

        url = f"{self.settings.openrouter_base_url.rstrip('/')}/embeddings"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": self.settings.embedding_model,
            "input": text,
            "dimensions": self.target_dim,
        }
        timeout = float(self.settings.timeouts.incident_seconds)

        if self._client is not None:
            resp = await self._client.post(url, headers=headers, json=payload, timeout=timeout)
        else:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, headers=headers, json=payload, timeout=timeout)

        if resp.status_code != 200:
            raise PlaybookEmbeddingError(
                f"OpenRouter embeddings API returned error status {resp.status_code}: {resp.text}",
                details={"status_code": resp.status_code, "response": resp.text[:500]},
            )

        data = resp.json()
        data_items = data.get("data")
        if not data_items or not isinstance(data_items, list):
            raise PlaybookEmbeddingError(
                "OpenRouter embeddings response missing data array",
                details={"response": data},
            )

        first_item = data_items[0]
        if not isinstance(first_item, dict):
            raise PlaybookEmbeddingError(
                "OpenRouter embeddings data item is not an object",
                details={"item": first_item},
            )

        raw_embedding = first_item.get("embedding")
        if not isinstance(raw_embedding, list):
            raise PlaybookEmbeddingError(
                "OpenRouter embedding value is not a list of floats",
                details={"embedding": raw_embedding},
            )

        return normalize_vector(raw_embedding, target_dim=self.target_dim)


__all__ = [
    "OpenRouterEmbeddingClient",
    "normalize_vector",
]
