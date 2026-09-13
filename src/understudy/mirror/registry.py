"""HTTP client implementation of the MirrorRegistry protocol.

Implements build-plan step A3.4:
- Interacts with the mirror-gateway REST API
  (POST /twins, DELETE /twins/{id}, GET /twins/{id}/stats)
- Maps twin handles to in-cluster edge-gateway destinations
- Conforms to MirrorRegistry protocol for injection into orchestrator Deps
- Provides typed domain error handling with MirrorError and TwinNotFoundError
"""

from typing import Any, Self
from urllib.parse import quote

import httpx

from understudy.common.config import Settings, get_settings
from understudy.common.errors import MirrorError, TwinNotFoundError
from understudy.common.logging import get_logger
from understudy.contracts.twin import MirrorStats, TwinHandle
from understudy.mirror.api import MirrorRegistry

logger = get_logger(__name__)

DEFAULT_TWIN_BASE_URL_TEMPLATE = "http://edge-gateway.{namespace}:8080"
DEFAULT_HTTP_TIMEOUT_SECONDS = 5.0


class HttpMirrorRegistry(MirrorRegistry):
    """HTTP client communicating with the mirror-gateway service."""

    def __init__(
        self,
        base_url: str | None = None,
        timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
        settings: Settings | None = None,
        twin_base_url_template: str = DEFAULT_TWIN_BASE_URL_TEMPLATE,
    ) -> None:
        resolved_settings = settings or get_settings()
        raw_base = base_url or resolved_settings.endpoints.mirror_gateway
        self.base_url = raw_base.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.twin_base_url_template = twin_base_url_template
        self._external_client = client is not None
        self._client: httpx.AsyncClient | None = client

    async def _get_client(self) -> httpx.AsyncClient:
        """Return existing client or lazily instantiate internal client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout_seconds)
            self._external_client = False
        return self._client

    async def aclose(self) -> None:
        """Close internally managed HTTP client."""
        if not self._external_client and self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> Self:
        await self._get_client()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        await self.aclose()

    def build_twin_base_url(self, twin_handle: TwinHandle) -> str:
        """Construct the cluster-internal base URL for a twin edge-gateway."""
        return self.twin_base_url_template.format(
            namespace=twin_handle.namespace,
            twin_id=twin_handle.twin_id,
            candidate_index=twin_handle.candidate_index,
            incident_id=twin_handle.incident_id,
        )

    async def register(
        self,
        twin_id: str,
        base_url: str,
        incident_id: str = "",
    ) -> None:
        """Directly register a twin by explicit ID and target URL."""
        clean_twin_id = twin_id.strip()
        clean_base_url = base_url.strip().rstrip("/")
        if not clean_twin_id:
            raise MirrorError("twin_id cannot be empty or whitespace")
        if not clean_base_url:
            raise MirrorError("base_url cannot be empty or whitespace")

        payload = {
            "twin_id": clean_twin_id,
            "base_url": clean_base_url,
            "incident_id": incident_id.strip(),
        }
        client = await self._get_client()
        url = f"{self.base_url}/twins"

        try:
            resp = await client.post(url, json=payload)
        except httpx.RequestError as exc:
            raise MirrorError(
                f"Failed to connect to mirror gateway at {url}: {exc}",
                details={"twin_id": clean_twin_id, "url": url},
            ) from exc

        if resp.status_code != 201:
            raise MirrorError(
                f"Mirror gateway rejected registration for twin {clean_twin_id!r} "
                f"with status {resp.status_code}: {resp.text}",
                details={
                    "twin_id": clean_twin_id,
                    "status_code": resp.status_code,
                    "response_body": resp.text,
                },
            )

        logger.info(
            "mirror_twin_registered",
            twin_id=clean_twin_id,
            base_url=clean_base_url,
            incident_id=incident_id,
        )

    async def register_twin(
        self,
        twin_handle: TwinHandle,
        base_url: str | None = None,
    ) -> None:
        """Register an active twin to begin receiving mirrored production traffic.

        Fulfills MirrorRegistry Protocol. If base_url is omitted, derives the
        destination URL from twin_base_url_template and twin_handle.namespace.
        """
        destination_url = base_url or self.build_twin_base_url(twin_handle)
        await self.register(
            twin_id=twin_handle.twin_id,
            base_url=destination_url,
            incident_id=twin_handle.incident_id,
        )

    async def unregister_twin(
        self,
        twin_id: str,
        *,
        raise_if_not_found: bool = False,
    ) -> None:
        """Unregister a twin from mirrored traffic fan-out.

        Fulfills MirrorRegistry Protocol. Defaults to idempotent behavior
        where a 404 response is treated as a clean completion.
        """
        clean_twin_id = twin_id.strip()
        if not clean_twin_id:
            raise MirrorError("twin_id cannot be empty or whitespace")

        client = await self._get_client()
        encoded_id = quote(clean_twin_id, safe="")
        url = f"{self.base_url}/twins/{encoded_id}"

        try:
            resp = await client.delete(url)
        except httpx.RequestError as exc:
            raise MirrorError(
                f"Failed to connect to mirror gateway at {url}: {exc}",
                details={"twin_id": clean_twin_id, "url": url},
            ) from exc

        if resp.status_code == 404:
            if raise_if_not_found:
                raise TwinNotFoundError(
                    f"Twin {clean_twin_id!r} is not registered with mirror gateway",
                    details={"twin_id": clean_twin_id},
                )
            logger.debug("mirror_twin_already_unregistered", twin_id=clean_twin_id)
            return

        if resp.status_code != 200:
            raise MirrorError(
                f"Mirror gateway unregistration failed for twin {clean_twin_id!r} "
                f"with status {resp.status_code}: {resp.text}",
                details={
                    "twin_id": clean_twin_id,
                    "status_code": resp.status_code,
                    "response_body": resp.text,
                },
            )

        logger.info("mirror_twin_unregistered", twin_id=clean_twin_id)

    async def get_stats(self, twin_id: str) -> MirrorStats:
        """Fetch delivery and drop metrics for a registered twin.

        Fulfills MirrorRegistry Protocol. Raises TwinNotFoundError if the twin
        is not currently registered with the gateway.
        """
        clean_twin_id = twin_id.strip()
        if not clean_twin_id:
            raise MirrorError("twin_id cannot be empty or whitespace")

        client = await self._get_client()
        encoded_id = quote(clean_twin_id, safe="")
        url = f"{self.base_url}/twins/{encoded_id}/stats"

        try:
            resp = await client.get(url)
        except httpx.RequestError as exc:
            raise MirrorError(
                f"Failed to connect to mirror gateway at {url}: {exc}",
                details={"twin_id": clean_twin_id, "url": url},
            ) from exc

        if resp.status_code == 404:
            raise TwinNotFoundError(
                f"Twin {clean_twin_id!r} is not registered with mirror gateway",
                details={"twin_id": clean_twin_id},
            )

        if resp.status_code != 200:
            raise MirrorError(
                f"Failed to fetch stats for twin {clean_twin_id!r} "
                f"with status {resp.status_code}: {resp.text}",
                details={
                    "twin_id": clean_twin_id,
                    "status_code": resp.status_code,
                    "response_body": resp.text,
                },
            )

        try:
            data = resp.json()
            return MirrorStats(
                twin_id=str(data.get("twin_id", clean_twin_id)),
                delivered=int(data.get("delivered", 0)),
                dropped=int(data.get("dropped", 0)),
            )
        except (ValueError, TypeError) as exc:
            raise MirrorError(
                f"Malformed statistics payload returned from mirror gateway: {resp.text}",
                details={"twin_id": clean_twin_id, "error": str(exc)},
            ) from exc

    async def get_all_stats(self) -> dict[str, MirrorStats]:
        """Fetch delivery and drop metrics for all registered twins."""
        client = await self._get_client()
        url = f"{self.base_url}/twins"

        try:
            resp = await client.get(url)
        except httpx.RequestError as exc:
            raise MirrorError(
                f"Failed to connect to mirror gateway at {url}: {exc}",
                details={"url": url},
            ) from exc

        if resp.status_code != 200:
            raise MirrorError(
                f"Failed to fetch all twin statistics with status {resp.status_code}: {resp.text}",
                details={"status_code": resp.status_code, "response_body": resp.text},
            )

        try:
            data = resp.json()
            if not isinstance(data, dict):
                raise ValueError(f"Expected JSON object mapping twin_id to stats, got {type(data)}")

            result: dict[str, MirrorStats] = {}
            for tid, raw_item in data.items():
                if isinstance(raw_item, dict):
                    result[tid] = MirrorStats(
                        twin_id=str(raw_item.get("twin_id", tid)),
                        delivered=int(raw_item.get("delivered", 0)),
                        dropped=int(raw_item.get("dropped", 0)),
                    )
            return result
        except (ValueError, TypeError) as exc:
            raise MirrorError(
                f"Malformed statistics response returned from mirror gateway: {resp.text}",
                details={"error": str(exc)},
            ) from exc

    async def health(self) -> bool:
        """Check mirror gateway liveness probe."""
        client = await self._get_client()
        url = f"{self.base_url}/healthz"
        try:
            resp = await client.get(url)
            return resp.status_code == 200
        except httpx.RequestError:
            return False

    async def ready(self) -> bool:
        """Check mirror gateway readiness probe (connectivity to production target)."""
        client = await self._get_client()
        url = f"{self.base_url}/readyz"
        try:
            resp = await client.get(url)
            return resp.status_code == 200
        except httpx.RequestError:
            return False


__all__ = ["DEFAULT_HTTP_TIMEOUT_SECONDS", "DEFAULT_TWIN_BASE_URL_TEMPLATE", "HttpMirrorRegistry"]
