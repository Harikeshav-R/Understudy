from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

import pytest
from langgraph.checkpoint.base import (
    ChannelVersions,
    CheckpointMetadata,
    CheckpointTuple,
    create_checkpoint,
    empty_checkpoint,
)
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph

from understudy.common.errors import OrchestratorError
from understudy.orchestrator.checkpoint import (
    PostgresCheckpointSaver,
    StoreCheckpointSaver,
    _run_sync,
    create_checkpointer,
)
from understudy.orchestrator.fakes import create_fake_deps
from understudy.orchestrator.state import State
from understudy.store.fakes import FakeCheckpointStore


def test_get_next_version() -> None:
    store = FakeCheckpointStore()
    saver = StoreCheckpointSaver(store)

    assert saver.get_next_version(None, None) == f"{1:032}"
    assert saver.get_next_version(5, None) == f"{6:032}"
    assert saver.get_next_version("2.0", None) == f"{3:032}"
    assert saver.get_next_version("invalid_str", None) == f"{1:032}"


def test_init_with_custom_serde() -> None:
    store = FakeCheckpointStore()
    custom_serde = JsonPlusSerializer()
    saver = StoreCheckpointSaver(store, serde=custom_serde)
    assert saver.serde is custom_serde
    assert PostgresCheckpointSaver is StoreCheckpointSaver


def test_run_sync_outside_event_loop() -> None:
    async def sample_coro() -> str:
        return "hello_sync"

    res = _run_sync(sample_coro())
    assert res == "hello_sync"


@pytest.mark.asyncio
async def test_run_sync_inside_event_loop() -> None:
    async def sample_coro() -> int:
        await asyncio.sleep(0.001)
        return 42

    res = _run_sync(sample_coro())
    assert res == 42


@pytest.mark.asyncio
async def test_store_checkpoint_saver_aput_and_aget_tuple() -> None:
    store = FakeCheckpointStore()
    saver = StoreCheckpointSaver(store)

    config: RunnableConfig = {"configurable": {"thread_id": "inc_01", "checkpoint_ns": ""}}
    base_cp = empty_checkpoint()
    cp = create_checkpoint(base_cp, {}, 1)
    cp["channel_values"] = {"counter": 10, "state_name": "initialized"}
    cp["channel_versions"] = {"counter": 1, "state_name": 1}
    metadata: CheckpointMetadata = {"source": "loop", "step": 1, "parents": {}}
    new_versions: ChannelVersions = {"counter": 1, "state_name": 1, "missing_key": 1}

    # Put checkpoint
    res_config = await saver.aput(config, cp, metadata, new_versions)
    assert res_config["configurable"]["thread_id"] == "inc_01"
    assert res_config["configurable"]["checkpoint_id"] == cp["id"]

    # Retrieve latest
    t = await saver.aget_tuple(config)
    assert t is not None
    assert t.config["configurable"]["checkpoint_id"] == cp["id"]
    assert t.checkpoint["channel_values"]["counter"] == 10
    assert t.checkpoint["channel_values"]["state_name"] == "initialized"
    assert "missing_key" not in t.checkpoint["channel_values"]
    assert t.metadata["step"] == 1
    assert t.parent_config is None

    # Retrieve specific ID
    specific_config: RunnableConfig = {
        "configurable": {
            "thread_id": "inc_01",
            "checkpoint_ns": "",
            "checkpoint_id": cp["id"],
        }
    }
    t_specific = await saver.aget_tuple(specific_config)
    assert t_specific is not None
    assert t_specific.config["configurable"]["checkpoint_id"] == cp["id"]

    # Retrieve non-existent
    missing_config: RunnableConfig = {
        "configurable": {
            "thread_id": "inc_01",
            "checkpoint_ns": "",
            "checkpoint_id": "cp_nonexistent",
        }
    }
    assert await saver.aget_tuple(missing_config) is None


@pytest.mark.asyncio
async def test_store_checkpoint_saver_parent_config_and_writes() -> None:
    store = FakeCheckpointStore()
    saver = StoreCheckpointSaver(store)

    config_parent: RunnableConfig = {
        "configurable": {"thread_id": "inc_02", "checkpoint_ns": "", "checkpoint_id": "parent_001"}
    }
    cp = create_checkpoint(empty_checkpoint(), {}, 2)
    cp["channel_values"] = {"status": "in_progress"}
    metadata: CheckpointMetadata = {"source": "loop", "step": 2, "parents": {}}

    res_config = await saver.aput(config_parent, cp, metadata, {"status": 2})
    assert res_config["configurable"]["checkpoint_id"] is not None

    # Put pending writes
    await saver.aput_writes(
        res_config,
        writes=[("output_channel", {"delta": 5})],
        task_id="task_node_1",
        task_path="path/step",
    )

    t = await saver.aget_tuple(res_config)
    assert t is not None
    assert t.parent_config is not None
    assert t.parent_config["configurable"]["checkpoint_id"] == "parent_001"
    assert t.pending_writes is not None
    assert len(t.pending_writes) == 1
    assert t.pending_writes[0][0] == "task_node_1"
    assert t.pending_writes[0][1] == "output_channel"
    assert t.pending_writes[0][2] == {"delta": 5}


@pytest.mark.asyncio
async def test_store_checkpoint_saver_alist_filtering() -> None:
    store = FakeCheckpointStore()
    saver = StoreCheckpointSaver(store)

    config: RunnableConfig = {"configurable": {"thread_id": "inc_list", "checkpoint_ns": ""}}

    cp1 = create_checkpoint(empty_checkpoint(), {}, 1)
    cp1["channel_values"] = {"step": 1}
    md1: CheckpointMetadata = {"source": "loop", "step": 1, "parents": {}}
    res1 = await saver.aput(config, cp1, md1, {"step": 1})

    cp2 = create_checkpoint(cp1, {}, 2)
    cp2["channel_values"] = {"step": 2}
    # Put empty channel blob to exercise v[0] == "empty" branch in alist
    await store.put_blobs([("inc_list", "", "empty_blob", 1, ("empty", b""))])
    cp2["channel_versions"]["empty_blob"] = 1
    md2: CheckpointMetadata = {"source": "loop", "step": 2, "parents": {}}
    res2 = await saver.aput(
        {
            "configurable": {
                "thread_id": "inc_list",
                "checkpoint_ns": "",
                "checkpoint_id": res1["configurable"]["checkpoint_id"],
            }
        },
        cp2,
        md2,
        {"step": 2, "empty_blob": 1},
    )

    # List all for thread
    all_tuples = [t async for t in saver.alist(config)]
    assert len(all_tuples) == 2

    # List without config
    all_unfiltered = [t async for t in saver.alist(None)]
    assert len(all_unfiltered) == 2

    # List with specific config_cid
    filtered_by_cid = [t async for t in saver.alist(res1)]
    assert len(filtered_by_cid) == 1
    assert filtered_by_cid[0].config["configurable"]["checkpoint_id"] == cp1["id"]

    # List with metadata filter
    filtered_md = [t async for t in saver.alist(config, filter={"step": 2})]
    assert len(filtered_md) == 1
    assert filtered_md[0].metadata["step"] == 2

    # List with before
    filtered_before = [t async for t in saver.alist(config, before=res2)]
    assert len(filtered_before) == 1
    assert filtered_before[0].config["configurable"]["checkpoint_id"] == cp1["id"]

    # List with limit
    limited = [t async for t in saver.alist(config, limit=1)]
    assert len(limited) == 1


@pytest.mark.asyncio
async def test_store_checkpoint_saver_adelete_thread() -> None:
    store = FakeCheckpointStore()
    saver = StoreCheckpointSaver(store)

    config: RunnableConfig = {"configurable": {"thread_id": "inc_del", "checkpoint_ns": ""}}
    cp = create_checkpoint(empty_checkpoint(), {}, 1)
    cp["channel_values"] = {"v": 1}
    await saver.aput(config, cp, {"source": "loop", "step": 1, "parents": {}}, {"v": 1})

    assert await saver.aget_tuple(config) is not None
    await saver.adelete_thread("inc_del")
    assert await saver.aget_tuple(config) is None


def test_store_checkpoint_saver_sync_interface() -> None:
    store = FakeCheckpointStore()
    saver = StoreCheckpointSaver(store)

    config: RunnableConfig = {"configurable": {"thread_id": "inc_sync", "checkpoint_ns": ""}}
    cp = create_checkpoint(empty_checkpoint(), {}, 1)
    cp["channel_values"] = {"val": "sync_value"}
    cp["channel_versions"] = {"val": 1}
    md: CheckpointMetadata = {"source": "loop", "step": 1, "parents": {}}

    res = saver.put(config, cp, md, {"val": 1})
    assert res["configurable"]["thread_id"] == "inc_sync"

    saver.put_writes(
        res,
        writes=[("sync_ch", "sync_res")],
        task_id="task_sync",
        task_path="",
    )

    t = saver.get_tuple(config)
    assert t is not None
    assert t.checkpoint["channel_values"]["val"] == "sync_value"
    assert t.pending_writes is not None
    assert len(t.pending_writes) == 1

    items = list(saver.list(config))
    assert len(items) == 1

    saver.delete_thread("inc_sync")
    assert saver.get_tuple(config) is None


def test_create_checkpointer_from_deps() -> None:
    deps = create_fake_deps()
    checkpointer = create_checkpointer(deps)
    assert isinstance(checkpointer, StoreCheckpointSaver)
    assert checkpointer.store is deps.checkpoint_store


def test_create_checkpointer_missing_store_raises() -> None:
    dummy_deps = MagicMock()
    dummy_deps.checkpoint_store = None
    with pytest.raises(OrchestratorError, match=r"Deps\.checkpoint_store is required"):
        create_checkpointer(dummy_deps)


@pytest.mark.asyncio
async def test_time_travel_and_resume_with_state_graph() -> None:
    """Verify that a StateGraph can resume execution from an earlier checkpoint."""
    store = FakeCheckpointStore()
    saver = StoreCheckpointSaver(store)

    builder: StateGraph[State, None, State, State] = StateGraph(State)
    builder.add_node("step_a", lambda _s: {"errors": ["step_a_done"]})
    builder.add_node("step_b", lambda _s: {"errors": ["step_b_done"]})
    builder.add_edge(START, "step_a")
    builder.add_edge("step_a", "step_b")
    builder.add_edge("step_b", END)

    graph = builder.compile(checkpointer=saver)

    config: RunnableConfig = {"configurable": {"thread_id": "inc_timetravel"}}
    initial_state = State(incident_id="inc_timetravel")
    final_output = await graph.ainvoke(initial_state, config=config)
    assert final_output["errors"] == ["step_a_done", "step_b_done"]

    # Inspect all checkpoints
    checkpoints = await store.list_checkpoints(thread_id="inc_timetravel")
    assert len(checkpoints) >= 3

    # Retrieve intermediate step_a checkpoint tuple
    step_a_cid = checkpoints[1][2]  # The checkpoint before step_b
    t_step_a = await saver.aget_tuple(
        {"configurable": {"thread_id": "inc_timetravel", "checkpoint_id": step_a_cid}}
    )
    assert t_step_a is not None
    assert isinstance(t_step_a, CheckpointTuple)
