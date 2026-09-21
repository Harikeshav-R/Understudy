"""Unit tests for OpenRouter embeddings client and vector normalization."""

import math
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from understudy.common.config import SecretSettings, Settings, TimeoutSettings
from understudy.common.errors import PlaybookEmbeddingError
from understudy.playbook.embeddings import (
    OpenRouterEmbeddingClient,
    normalize_vector,
)


def test_normalize_vector() -> None:
    # 1. Exact length (already 1024)
    exact = [1.0] + [0.0] * 1023
    norm_exact = normalize_vector(exact, target_dim=1024)
    assert len(norm_exact) == 1024
    assert abs(math.sqrt(sum(x * x for x in norm_exact)) - 1.0) < 1e-6

    # 2. Longer than 1024 (e.g. 1536)
    long_vec = [1.0] * 1536
    norm_long = normalize_vector(long_vec, target_dim=1024)
    assert len(norm_long) == 1024
    assert abs(math.sqrt(sum(x * x for x in norm_long)) - 1.0) < 1e-6

    # 3. Shorter than 1024 (e.g. 512)
    short_vec = [2.0] * 512
    norm_short = normalize_vector(short_vec, target_dim=1024)
    assert len(norm_short) == 1024
    assert abs(math.sqrt(sum(x * x for x in norm_short)) - 1.0) < 1e-6

    # 4. Zero vector fallback
    zero_vec = [0.0] * 1024
    norm_zero = normalize_vector(zero_vec, target_dim=1024)
    assert len(norm_zero) == 1024
    assert norm_zero == [0.0] * 1024


@pytest.mark.asyncio
async def test_openrouter_embedding_client_custom_caller() -> None:
    caller = AsyncMock(return_value=[0.5] * 1024)
    client = OpenRouterEmbeddingClient(embed_caller=caller)

    res = await client.embed("test incident signature")
    assert len(res) == 1024
    assert abs(math.sqrt(sum(x * x for x in res)) - 1.0) < 1e-6
    caller.assert_awaited_once_with("test incident signature")


@pytest.mark.asyncio
async def test_openrouter_embedding_client_missing_api_key() -> None:
    settings = Settings(
        secrets=SecretSettings(openrouter_api_key=None),
        timeouts=TimeoutSettings(),
    )
    client = OpenRouterEmbeddingClient(settings=settings)

    with pytest.raises(PlaybookEmbeddingError) as exc:
        await client.embed("test text")
    assert "OPENROUTER_API_KEY is not configured" in str(exc.value)


@pytest.mark.asyncio
async def test_openrouter_embedding_client_success() -> None:
    settings = Settings(
        secrets=SecretSettings(openrouter_api_key="sk-or-test-embed-key"),
        openrouter_base_url="https://openrouter.ai/api/v1",
        embedding_model="text-embedding-3-small",
        timeouts=TimeoutSettings(incident_seconds=30),
    )
    mock_resp_data = {
        "object": "list",
        "data": [
            {
                "object": "embedding",
                "index": 0,
                "embedding": [0.1] * 1024,
            }
        ],
    }
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post = AsyncMock(return_value=httpx.Response(200, json=mock_resp_data))

    client = OpenRouterEmbeddingClient(settings=settings, client=mock_client)
    res = await client.embed("incident signature text")

    assert len(res) == 1024
    assert abs(math.sqrt(sum(x * x for x in res)) - 1.0) < 1e-6

    # Verify call parameters
    mock_client.post.assert_awaited_once()
    args, kwargs = mock_client.post.call_args
    assert args[0] == "https://openrouter.ai/api/v1/embeddings"
    assert kwargs["headers"]["Authorization"] == "Bearer sk-or-test-embed-key"
    assert kwargs["json"]["model"] == "text-embedding-3-small"
    assert kwargs["json"]["dimensions"] == 1024


@pytest.mark.asyncio
async def test_openrouter_embedding_client_http_errors() -> None:
    settings = Settings(
        secrets=SecretSettings(openrouter_api_key="sk-or-test-embed-key"),
        timeouts=TimeoutSettings(incident_seconds=30),
    )
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    client = OpenRouterEmbeddingClient(settings=settings, client=mock_client)

    # 1. Non-200 status
    mock_client.post = AsyncMock(return_value=httpx.Response(500, text="Internal Server Error"))
    with pytest.raises(PlaybookEmbeddingError) as exc1:
        await client.embed("test")
    assert "OpenRouter embeddings API returned error status 500" in str(exc1.value)

    # 2. Missing data array
    mock_client.post = AsyncMock(return_value=httpx.Response(200, json={"error": "no data"}))
    with pytest.raises(PlaybookEmbeddingError) as exc2:
        await client.embed("test")
    assert "missing data array" in str(exc2.value)

    # 3. Data item is not dict
    mock_client.post = AsyncMock(return_value=httpx.Response(200, json={"data": ["not_a_dict"]}))
    with pytest.raises(PlaybookEmbeddingError) as exc3:
        await client.embed("test")
    assert "data item is not an object" in str(exc3.value)

    # 4. Embedding is not list
    mock_client.post = AsyncMock(
        return_value=httpx.Response(200, json={"data": [{"embedding": "not_a_list"}]})
    )
    with pytest.raises(PlaybookEmbeddingError) as exc4:
        await client.embed("test")
    assert "embedding value is not a list" in str(exc4.value)


@pytest.mark.asyncio
async def test_openrouter_embedding_client_default_context_manager() -> None:
    settings = Settings(
        secrets=SecretSettings(openrouter_api_key="sk-or-test-key"),
        timeouts=TimeoutSettings(incident_seconds=30),
    )
    client = OpenRouterEmbeddingClient(settings=settings)
    mock_resp = httpx.Response(200, json={"data": [{"embedding": [0.2] * 1024}]})

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        res = await client.embed("test context manager")
        assert len(res) == 1024


@pytest.mark.asyncio
async def test_openrouter_embedding_client_omits_dimensions_for_non_openai_models() -> None:
    """Verify models without text-embedding-3 omit dimensions parameter."""
    settings = Settings(
        secrets=SecretSettings(openrouter_api_key="sk-or-test-key"),
        embedding_model="nvidia/nemotron-3-embed-1b:free",
        timeouts=TimeoutSettings(incident_seconds=30),
    )
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_resp_data = {"data": [{"embedding": [0.5] * 2048}]}
    mock_client.post = AsyncMock(return_value=httpx.Response(200, json=mock_resp_data))

    client = OpenRouterEmbeddingClient(settings=settings, client=mock_client)
    res = await client.embed("test non-openai model")

    assert len(res) == 1024
    mock_client.post.assert_awaited_once()
    _, kwargs = mock_client.post.call_args
    assert kwargs["json"]["model"] == "nvidia/nemotron-3-embed-1b:free"
    assert "dimensions" not in kwargs["json"]
