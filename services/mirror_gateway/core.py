"""Core queuing, worker tasks, and stats tracking for the mirror gateway.

Conforms to ADR-012, ADR-013, and build-plan step A3.1:
- Synchronous proxy to ust-prod edge-gateway
- Per-twin bounded asyncio.Queue(maxsize=1000)
- Dedicated worker task per twin draining its queue with a 2s per-request timeout
- Drop-and-count on full queue and on worker timeout / delivery failure
- Shadow headers injected:
  X-Understudy-Shadow: 1, X-Understudy-Twin: <twin_id>, X-Understudy-Incident: <id>
"""

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

from services._common.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class MirroredRequest:
    """Captured HTTP request envelope dispatched to twins."""

    method: str
    path: str
    query: str
    headers: dict[str, str]
    body: bytes


class MirrorStats(BaseModel):
    """Mirror statistics for a registered twin."""

    model_config = ConfigDict(frozen=True)

    twin_id: str
    delivered: int = 0
    dropped: int = 0
    drop_ratio: float = 0.0


# Backward-compatible alias
MirrorStatsResponse = MirrorStats


@dataclass
class TwinRegistration:
    """In-memory state and drain worker for an active twin destination."""

    twin_id: str
    base_url: str
    incident_id: str
    queue: asyncio.Queue[MirroredRequest]
    worker_task: asyncio.Task[None] | None = None
    delivered: int = 0
    dropped: int = 0
    worker_timeout_seconds: float = 2.0


class MirrorGatewayManager:
    """Manages active twin registrations, bounded queues, and drain workers."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        queue_maxsize: int = 1000,
        worker_timeout_seconds: float = 2.0,
    ) -> None:
        self.client = client
        self.queue_maxsize = queue_maxsize
        self.worker_timeout_seconds = worker_timeout_seconds
        self._twins: dict[str, TwinRegistration] = {}

    @property
    def registered_twins(self) -> dict[str, TwinRegistration]:
        """Return view of registered twins."""
        return self._twins

    def register_twin(
        self,
        twin_id: str,
        base_url: str,
        incident_id: str = "",
    ) -> TwinRegistration:
        """Register an active twin, allocate a bounded queue, and start a drain worker."""
        clean_twin_id = twin_id.strip()
        clean_base_url = base_url.strip().rstrip("/")
        if not clean_twin_id:
            raise ValueError("twin_id cannot be empty")
        if not clean_base_url:
            raise ValueError("base_url cannot be empty")

        if clean_twin_id in self._twins:
            self.unregister_twin(clean_twin_id)

        queue: asyncio.Queue[MirroredRequest] = asyncio.Queue(maxsize=self.queue_maxsize)
        twin = TwinRegistration(
            twin_id=clean_twin_id,
            base_url=clean_base_url,
            incident_id=incident_id.strip(),
            queue=queue,
            worker_timeout_seconds=self.worker_timeout_seconds,
        )
        worker_task = asyncio.create_task(
            self._drain_worker(twin),
            name=f"mirror-worker-{clean_twin_id}",
        )
        twin.worker_task = worker_task
        self._twins[clean_twin_id] = twin
        logger.info("twin_registered", twin_id=clean_twin_id, base_url=clean_base_url)
        return twin

    def unregister_twin(self, twin_id: str) -> bool:
        """Unregister an active twin and cancel its drain worker task."""
        twin = self._twins.pop(twin_id, None)
        if twin is None:
            return False

        if twin.worker_task and not twin.worker_task.done():
            twin.worker_task.cancel()
        logger.info("twin_unregistered", twin_id=twin_id)
        return True

    def dispatch_to_twins(self, request: MirroredRequest) -> None:
        """Asynchronously fan out a request to all registered twins (fire-and-forget).

        Twin delivery never blocks the production path (ADR-013).
        If a twin's bounded queue is full, the request is dropped immediately and
        the drop counter for that twin is incremented.
        """
        for twin in list(self._twins.values()):
            try:
                twin.queue.put_nowait(request)
            except asyncio.QueueFull:
                twin.dropped += 1
                logger.warning(
                    "mirror_queue_overflow",
                    twin_id=twin.twin_id,
                    dropped=twin.dropped,
                )

    def _build_stats(self, twin: TwinRegistration) -> MirrorStats:
        """Calculate statistics for a twin registration."""
        total = twin.delivered + twin.dropped
        drop_ratio = (twin.dropped / total) if total > 0 else 0.0
        return MirrorStats(
            twin_id=twin.twin_id,
            delivered=twin.delivered,
            dropped=twin.dropped,
            drop_ratio=drop_ratio,
        )

    def get_stats(self, twin_id: str) -> MirrorStats | None:
        """Retrieve delivery and drop statistics for a registered twin."""
        twin = self._twins.get(twin_id)
        if twin is None:
            return None
        return self._build_stats(twin)

    def get_all_stats(self) -> dict[str, MirrorStats]:
        """Retrieve statistics for all registered twins."""
        return {twin_id: self._build_stats(twin) for twin_id, twin in self._twins.items()}

    async def close(self) -> None:
        """Cancel all drain workers and await shutdown."""
        tasks: list[asyncio.Task[Any]] = []
        for twin in self._twins.values():
            if twin.worker_task and not twin.worker_task.done():
                twin.worker_task.cancel()
                tasks.append(twin.worker_task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._twins.clear()

    async def _drain_worker(self, twin: TwinRegistration) -> None:
        """Worker task per twin draining its bounded queue with a timeout."""
        try:
            while True:
                req = await twin.queue.get()
                try:
                    headers = dict(req.headers)
                    headers["X-Understudy-Shadow"] = "1"
                    headers["X-Understudy-Twin"] = twin.twin_id
                    if twin.incident_id:
                        headers["X-Understudy-Incident"] = twin.incident_id
                    elif "x-understudy-incident" not in {k.lower() for k in headers}:
                        headers["X-Understudy-Incident"] = ""

                    # Filter hop-by-hop headers
                    for h in ["host", "content-length", "connection", "transfer-encoding"]:
                        for k in list(headers.keys()):
                            if k.lower() == h:
                                del headers[k]

                    url = f"{twin.base_url.rstrip('/')}/{req.path.lstrip('/')}"
                    if req.query:
                        url = f"{url}?{req.query}"

                    await self.client.request(
                        method=req.method,
                        url=url,
                        headers=headers,
                        content=req.body,
                        timeout=twin.worker_timeout_seconds,
                    )
                    twin.delivered += 1
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "mirror_drain_error",
                        twin_id=twin.twin_id,
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
                    twin.dropped += 1
                finally:
                    twin.queue.task_done()
        except asyncio.CancelledError:
            pass
