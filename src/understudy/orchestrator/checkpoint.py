"""LangGraph checkpointer implementation backed by Understudy store.

Implements build-plan step B1.4:
- Checkpoints LangGraph StateGraph executions into store, keyed by incident_id.
- Implements BaseCheckpointSaver[str] over CheckpointStore protocol.
- Supports full async operations (aput, aget_tuple, alist, aput_writes, adelete_thread)
  and sync operations (put, get_tuple, list, put_writes, delete_thread).
- Integrates with JsonPlusSerializer for typed round-trip state serialization.
"""

import asyncio
import concurrent.futures
from collections.abc import AsyncIterator, Coroutine, Iterator, Sequence
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_id,
    get_checkpoint_metadata,
)
from langgraph.checkpoint.serde.base import SerializerProtocol
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from understudy.common.errors import OrchestratorError
from understudy.store.api import CheckpointStore


def _run_sync[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run an async coroutine synchronously, whether or not an event loop is active."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


class StoreCheckpointSaver(BaseCheckpointSaver[str]):
    """LangGraph checkpointer backed by Understudy CheckpointStore."""

    def __init__(
        self,
        store: CheckpointStore,
        serde: SerializerProtocol | None = None,
    ) -> None:
        super().__init__(serde=serde or JsonPlusSerializer())
        self.store = store

    def get_next_version(self, current: str | int | float | None, channel: None) -> str:
        """Generate a monotonically increasing string version identifier for a channel."""
        _ = channel
        if current is None:
            current_v = 0
        elif isinstance(current, int):
            current_v = current
        else:
            try:
                current_v = int(str(current).split(".")[0])
            except ValueError:
                current_v = 0
        next_v = current_v + 1
        return f"{next_v:032}"

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """Asynchronously save a checkpoint and channel version blobs to the store."""
        thread_id: str = config["configurable"]["thread_id"]
        checkpoint_ns: str = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = checkpoint["id"]
        values: dict[str, Any] = checkpoint.get("channel_values", {})
        c = {k: v for k, v in checkpoint.items() if k != "channel_values"}

        blobs_to_put: list[tuple[str, str, str, str | int | float, tuple[str, bytes]]] = []
        for k, v in new_versions.items():
            encoded = self.serde.dumps_typed(values[k]) if k in values else ("empty", b"")
            blobs_to_put.append((thread_id, checkpoint_ns, k, v, encoded))
        await self.store.put_blobs(blobs_to_put)

        cp_encoded = self.serde.dumps_typed(c)
        md_encoded = self.serde.dumps_typed(get_checkpoint_metadata(config, metadata))
        parent_id = config["configurable"].get("checkpoint_id")
        await self.store.put_checkpoint(
            thread_id,
            checkpoint_ns,
            checkpoint_id,
            parent_id,
            cp_encoded,
            md_encoded,
        )
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        }

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """Asynchronously save pending writes for a task execution step."""
        thread_id: str = config["configurable"]["thread_id"]
        checkpoint_ns: str = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id: str = config["configurable"]["checkpoint_id"]
        writes_to_put: list[tuple[str, str, str, str, int, str, tuple[str, bytes], str]] = []
        for idx, (c, v) in enumerate(writes):
            inner_idx = WRITES_IDX_MAP.get(c, idx)
            val_encoded = self.serde.dumps_typed(v)
            writes_to_put.append(
                (
                    thread_id,
                    checkpoint_ns,
                    checkpoint_id,
                    task_id,
                    inner_idx,
                    c,
                    val_encoded,
                    task_path,
                )
            )
        await self.store.put_writes(writes_to_put)

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """Asynchronously retrieve a CheckpointTuple matching the given config."""
        thread_id: str = config["configurable"]["thread_id"]
        checkpoint_ns: str = config["configurable"].get("checkpoint_ns", "")
        target_cid = get_checkpoint_id(config)
        found = await self.store.get_checkpoint(thread_id, checkpoint_ns, target_cid)
        if found is None:
            return None
        cid, cp_data, md_data, parent_cid = found
        checkpoint_: Checkpoint = self.serde.loads_typed(cp_data)
        metadata: CheckpointMetadata = self.serde.loads_typed(md_data)

        raw_blobs = await self.store.get_blobs(
            thread_id, checkpoint_ns, checkpoint_["channel_versions"]
        )
        channel_values: dict[str, Any] = {}
        for k, v in raw_blobs.items():
            if v[0] != "empty":
                channel_values[k] = self.serde.loads_typed(v)

        raw_writes = await self.store.get_writes(thread_id, checkpoint_ns, cid)
        pending_writes = [
            (task_id, ch, self.serde.loads_typed(blob)) for task_id, ch, blob, _ in raw_writes
        ]

        return CheckpointTuple(
            config={
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": cid,
                }
            },
            checkpoint={**checkpoint_, "channel_values": channel_values},
            metadata=metadata,
            parent_config=(
                {
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": checkpoint_ns,
                        "checkpoint_id": parent_cid,
                    }
                }
                if parent_cid
                else None
            ),
            pending_writes=pending_writes,
        )

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        """Asynchronously yield CheckpointTuples matching the search criteria."""
        thread_id: str | None = None
        checkpoint_ns: str | None = None
        if config and "configurable" in config:
            thread_id = config["configurable"].get("thread_id")
            checkpoint_ns = config["configurable"].get("checkpoint_ns")
        config_cid = get_checkpoint_id(config) if config else None
        before_cid = get_checkpoint_id(before) if before else None

        records = await self.store.list_checkpoints(
            thread_id=thread_id,
            checkpoint_ns=checkpoint_ns,
            before_checkpoint_id=before_cid,
        )

        count = 0
        for tid, ns, cid, cp_data, md_data, parent_cid in records:
            if config_cid is not None and cid != config_cid:
                continue

            metadata: CheckpointMetadata = self.serde.loads_typed(md_data)
            if filter and not all(metadata.get(k) == v for k, v in filter.items()):
                continue

            if limit is not None and count >= limit:
                break

            checkpoint_: Checkpoint = self.serde.loads_typed(cp_data)
            raw_blobs = await self.store.get_blobs(tid, ns, checkpoint_["channel_versions"])
            channel_values: dict[str, Any] = {}
            for k, v in raw_blobs.items():
                if v[0] != "empty":
                    channel_values[k] = self.serde.loads_typed(v)

            raw_writes = await self.store.get_writes(tid, ns, cid)
            pending_writes = [
                (task_id, ch, self.serde.loads_typed(blob)) for task_id, ch, blob, _ in raw_writes
            ]

            yield CheckpointTuple(
                config={
                    "configurable": {
                        "thread_id": tid,
                        "checkpoint_ns": ns,
                        "checkpoint_id": cid,
                    }
                },
                checkpoint={**checkpoint_, "channel_values": channel_values},
                metadata=metadata,
                parent_config=(
                    {
                        "configurable": {
                            "thread_id": tid,
                            "checkpoint_ns": ns,
                            "checkpoint_id": parent_cid,
                        }
                    }
                    if parent_cid
                    else None
                ),
                pending_writes=pending_writes,
            )
            count += 1

    async def adelete_thread(self, thread_id: str) -> None:
        """Asynchronously delete all checkpoints and writes associated with a thread ID."""
        await self.store.delete_thread(thread_id)

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """Synchronously save a checkpoint and channel version blobs to the store."""
        return _run_sync(self.aput(config, checkpoint, metadata, new_versions))

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """Synchronously save pending writes for a task execution step."""
        _run_sync(self.aput_writes(config, writes, task_id, task_path))

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """Synchronously retrieve a CheckpointTuple matching the given config."""
        return _run_sync(self.aget_tuple(config))

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        """Synchronously yield CheckpointTuples matching the search criteria."""

        async def _collect() -> list[CheckpointTuple]:
            items: list[CheckpointTuple] = []
            async for item in self.alist(config, filter=filter, before=before, limit=limit):
                items.append(item)
            return items

        yield from _run_sync(_collect())

    def delete_thread(self, thread_id: str) -> None:
        """Synchronously delete all checkpoints and writes associated with a thread ID."""
        _run_sync(self.adelete_thread(thread_id))


PostgresCheckpointSaver = StoreCheckpointSaver


def create_checkpointer(deps: Any) -> StoreCheckpointSaver:
    """Construct a LangGraph checkpoint saver using the checkpoint store from Deps."""
    checkpoint_store = getattr(deps, "checkpoint_store", None)
    if checkpoint_store is None:
        raise OrchestratorError("Deps.checkpoint_store is required for checkpointing")
    return StoreCheckpointSaver(checkpoint_store)


__all__ = [
    "PostgresCheckpointSaver",
    "StoreCheckpointSaver",
    "create_checkpointer",
]
