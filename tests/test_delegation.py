"""Exercise delegation with real SQLite, XML dispatch and the AstrBot tool executor."""

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from xml.etree.ElementTree import fromstring

import mcp.types
import pytest
import pytest_asyncio
from core.conversation.chat_manager import ChatManager, QueuedMessage, SessionState
from core.conversation.delegation import DelegationManager
from core.conversation.tool_executor import FunctionToolExecutor
from core.database.data_cache import DataCache
from core.database.database import Database
from core.llm.call_llm import CallLLM
from core.llm.tool_history import ReplyToolHistory
from core.llm.xml_parse import XmlParse
from core.utils.schemas import MessageData
from core.web.chat_history_api import ChatHistoryApi
from starlette.requests import Request

from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain
from astrbot.api.star import Context
from astrbot.api.web import PluginRequest, bind_request_context
from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.astr_agent_context import AgentContextWrapper, AstrAgentContext
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.provider.entities import LLMResponse
from astrbot.core.utils.session_lock import session_lock_manager

LONG_RESULT = "private raw tool output " * 400
IMAGE_DATA = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jWV8AAAAASUVORK5CYII="
TOOL_ERROR = "private lookup error"


class Lookup(FunctionTool):
    async def call(self, context, **kwargs):
        await context.context.event.send(MessageChain([Plain("private direct output")]))
        yield mcp.types.CallToolResult(
            content=[
                mcp.types.TextContent(type="text", text=LONG_RESULT),
                mcp.types.ImageContent(
                    type="image", mimeType="image/png", data=IMAGE_DATA
                ),
            ]
        )
        yield "evidence-42"
        if kwargs.get("fail"):
            raise RuntimeError(TOOL_ERROR)


def source(mid, content):
    return {
        "message_id": mid,
        "user_id": "alice",
        "nickname": "Alice",
        "content": content,
        "media_ids": [],
    }


@pytest_asyncio.fixture
async def rt(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "astrbot.api.star.StarTools.get_data_dir", lambda *args: tmp_path
    )
    db = await Database.connect()
    event = MagicMock(spec=AiocqhttpMessageEvent)
    event.platform_meta = SimpleNamespace(id="qq")
    event.unified_msg_origin = "qq:GroupMessage:7"
    event.get_group_id.return_value = "7"
    event.get_self_id.return_value = "bot-id"
    event.get_sender_id.return_value = "alice"
    event.get_sender_name.return_value = "Alice"
    event.get_platform_name.return_value = "aiocqhttp"
    context = Context.__new__(Context)
    lookup, direct = (
        Lookup(
            name=name, description=name, parameters={"type": "object", "properties": {}}
        )
        for name in ("lookup", "direct")
    )
    context.get_llm_tool_manager = lambda: SimpleNamespace(
        get_full_tool_set=lambda: ToolSet(tools=[lookup, direct]),
        is_builtin_tool=lambda _: False,
    )
    context.persona_manager = SimpleNamespace(
        get_persona_v3_by_id=lambda _: {"prompt": "Giftia", "tools": None}
    )
    context.llm_generate = AsyncMock()
    context.tool_loop_agent = AsyncMock()
    plugin = SimpleNamespace(
        context=context,
        db=db,
        tools_config={"max_loop": 3, "use_source_tools": True},
        adapter_id_map={"qq": "bot"},
        bot_map={
            "bot": {
                "nickname": "Bot",
                "llm_reply_conf": {
                    "persona_id": "default",
                    "provider_ids": ["model"],
                    "subagent_tools": ["lookup"],
                },
            }
        },
        embedding_conf={"enabled": False},
        msg_number=20,
        min_reply_interval=0,
        max_reply_interval=0,
        replying_status={},
        active_reply_counters={},
        passive_memory_manager=SimpleNamespace(reset_silence_counter=AsyncMock()),
        emoji_manager=SimpleNamespace(get_random_stickers=AsyncMock(return_value=[])),
        aiocqhttp=SimpleNamespace(send_message=AsyncMock(return_value=(True, "sent"))),
    )
    plugin.get_bot_config = plugin.bot_map.get
    plugin.data_cache = DataCache(db, None, None, plugin=plugin)
    plugin.data_cache.build_active_user_briefs = AsyncMock(return_value=[])
    plugin.data_cache.get_user_profile_record = AsyncMock(return_value={})
    plugin.call_llm = CallLLM(
        context, XmlParse(None, None), {"reply_retry_times": 1}, {}, plugin=plugin
    )
    plugin.chat_manager = ChatManager(plugin)
    plugin.delegations = DelegationManager(plugin)
    scope = {
        "bot_name": "bot",
        "session_id": "7",
        "request_id": "first",
        "user_ids": ["alice"],
        "creator_names": {"alice": "Alice"},
        "delegation_inputs": [source("a", "查版本")],
        "delegation_providers": ["model"],
    }
    try:
        yield SimpleNamespace(
            p=plugin, d=plugin.delegations, db=db, e=event, scope=scope, lookup=lookup
        )
    finally:
        await plugin.delegations.close()
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("force_xml", [False, True])
@pytest.mark.parametrize("dedicated_providers", [False, True])
async def test_real_reply_dispatches_ack_and_isolates_worker_until_wakeup(
    rt, force_xml, dedicated_providers
):
    p, event = rt.p, rt.e
    p.tools_config["force_xml_tools"] = force_xml
    if dedicated_providers:
        p.bot_map["bot"]["llm_reply_conf"].update(
            subagent_provider_ids=["worker-first", "worker-second"],
            provider_selection_mode="random",
        )
    original = MessageData(
        message_id="a",
        user_id="alice",
        nickname="Alice",
        group_or_user_id="7",
        content="查版本",
        time=datetime.now().isoformat(),
    )
    await p.data_cache.add_message("bot", "7", original)
    state = SessionState(current=[QueuedMessage(event, original, True, 0, 3)])
    p.context.tool_loop_agent.side_effect = [
        LLMResponse(
            "assistant",
            '<tool_call name="delegate_task">{"instruction":"查版本"}</tool_call><message>我去查。</message>',
        ),
        LLMResponse("assistant", "<message>支持版本 4，依据是 evidence-42。</message>"),
    ]
    worker_call = (
        LLMResponse("assistant", '<root><tool_call name="lookup">{}</tool_call></root>')
        if force_xml
        else LLMResponse(
            "assistant",
            tools_call_name=["lookup"],
            tools_call_args=[None],
            tools_call_ids=["1"],
        )
    )
    worker_report = LLMResponse("assistant", "支持版本 4，evidence-42")
    p.context.llm_generate.side_effect = (
        [
            RuntimeError("provider request failed"),
            worker_call,
            LLMResponse("assistant"),
            worker_report,
        ]
        if dedicated_providers
        else [worker_call, worker_report]
    )
    p.aiocqhttp.send_message.side_effect = [(True, "ack"), (True, "final")]
    async with session_lock_manager.acquire_lock(event.unified_msg_origin):
        assert await p.chat_manager._reply_batch(
            event, "bot", "7", state, [original], None, [], None
        )
        assert state.waiting_agent and p.context.tool_loop_agent.await_count == 1
        worker = next(iter(rt.d.workers.values()))
        await rt.db.chat_history_repo.update_processing_status(
            "bot", "7", ["a"], "waiting_agent"
        )
    await asyncio.wait_for(worker, 5)
    assert p.context.tool_loop_agent.await_count == 2
    assert p.aiocqhttp.send_message.await_count == 2
    event.send.assert_not_called()
    parent, wake = (c.kwargs for c in p.context.tool_loop_agent.await_args_list)
    assert parent["chat_provider_id"] == wake["chat_provider_id"] == "model"
    expected_order = (
        ["worker-first", "worker-second"] * 2 if dedicated_providers else ["model"] * 2
    )
    assert [
        call.kwargs["chat_provider_id"]
        for call in p.context.llm_generate.await_args_list
    ] == expected_order
    if force_xml:
        assert '<tool_call name="direct"' in parent["system_prompt"]
        assert '<tool_call name="lookup"' not in parent["system_prompt"]
    else:
        assert [t.name for t in parent["tools"].tools] == ["direct"]
    assert "delegate_task" in parent["system_prompt"] and "查版本" in wake["prompt"]
    assert "delegate_task" not in wake["system_prompt"]
    assert "evidence-42" in wake["prompt"] and LONG_RESULT not in wake["prompt"]
    worker_input = p.context.llm_generate.await_args.kwargs
    assert (
        LONG_RESULT in worker_input["prompt"]
        and "private direct output" in worker_input["prompt"]
    )
    assert IMAGE_DATA not in worker_input["prompt"] and IMAGE_DATA not in wake["prompt"]
    assert worker_input["image_urls"] == [f"data:image/png;base64,{IMAGE_DATA}"]
    async with rt.db.query(
        "SELECT payload FROM delegated_steps WHERE kind='output'"
    ) as cur:
        assert IMAGE_DATA in (await cur.fetchone())[0]
    async with rt.db.query("SELECT content FROM chat_history") as cur:
        history = [row[0] for row in await cur.fetchall()]
    assert not any(LONG_RESULT in item for item in history)
    async with rt.db.query("SELECT status FROM delegated_tasks") as cur:
        assert (await cur.fetchone())[0] == "delivered"
    async with rt.db.query(
        "SELECT processing_status FROM chat_history WHERE message_id='a'"
    ) as cur:
        assert (await cur.fetchone())[0] == "handled"


@pytest.mark.asyncio
@pytest.mark.parametrize("force_xml", [False, True])
async def test_provider_configuration_without_tools_omits_delegation_prompt(
    rt, force_xml
):
    p, event = rt.p, rt.e
    p.bot_map["bot"]["llm_reply_conf"].update(
        subagent_tools=[], subagent_provider_ids=["configured-model"]
    )
    p.tools_config["force_xml_tools"] = force_xml
    p.context.tool_loop_agent.return_value = LLMResponse(
        "assistant", "<message>收到。</message>"
    )
    rt.d.active = MagicMock(return_value=True)
    rt.d.context = AsyncMock(return_value="<subagent_tasks>old task</subagent_tasks>")
    message = MessageData(message_id="a", user_id="alice", content="你好")
    state = SessionState(current=[QueuedMessage(event, message, True, 0, 3)])
    async with session_lock_manager.acquire_lock(event.unified_msg_origin):
        assert await p.chat_manager._reply_batch(
            event, "bot", "7", state, [message], None, [], None
        )
    request = p.context.tool_loop_agent.await_args.kwargs
    assert "delegate_task" not in request["system_prompt"]
    assert "后台委派" not in request["system_prompt"]
    assert "subagent_tasks" not in request["prompt"]
    if force_xml:
        assert '<tool_call name="lookup"' in request["system_prompt"]
    else:
        assert request["tools"].names() == ["lookup", "direct"]
    rt.d.context.assert_not_awaited()
    p.context.llm_generate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_xml_call_only_keeps_same_turn_results_and_logs_every_call(rt, fail):
    p, event = rt.p, rt.e
    p.tools_config["force_xml_tools"] = True
    p.bot_map["bot"]["llm_reply_conf"]["call_only_tools"] = ["direct"]
    message = MessageData(message_id="a", user_id="alice", content="查资料")
    state = SessionState(current=[QueuedMessage(event, message, True, 0, 3)])
    # Distinct calls to one tool produce identical history entries in this mode.
    tool_calls = "".join(
        f'<tool_call name="alias:direct">{json.dumps({"query": query, "fail": fail})}</tool_call>'
        for query in ("private query 1", "private query 2")
    )
    p.context.tool_loop_agent.side_effect = [
        LLMResponse("assistant", tool_calls),
        LLMResponse("assistant", "<message>查到了。</message>"),
    ]
    async with session_lock_manager.acquire_lock(event.unified_msg_origin):
        assert await p.chat_manager._reply_batch(
            event, "bot", "7", state, [message], None, [], None
        )
    same_turn = p.context.tool_loop_agent.await_args.kwargs["prompt"]
    assert LONG_RESULT in same_turn and "evidence-42" in same_turn
    assert (TOOL_ERROR in same_turn) is fail
    async with rt.db.query(
        "SELECT content,created_at FROM chat_history WHERE role='operation_log' ORDER BY id"
    ) as cur:
        rows = await cur.fetchall()
    assert len(rows) == 2
    for content, created_at in rows:
        entry = fromstring(content)
        assert entry.attrib == {
            "name": "direct",
            "status": "failed" if fail else "finished",
        }
        assert entry.text is None
        assert created_at


@pytest.mark.asyncio
async def test_followup_while_completed_wakeup_is_blocked_by_parent_lock(
    rt, monkeypatch
):
    release_worker, queued = asyncio.Event(), asyncio.Event()
    deliver = rt.d._deliver

    async def notify(*args):
        queued.set()
        await deliver(*args)

    monkeypatch.setattr(rt.d, "_deliver", notify)
    wake = rt.p.chat_manager.deliver_delegation = AsyncMock(return_value=True)

    async def worker_reply(**kwargs):
        if "Linux works" in kwargs["prompt"]:
            return LLMResponse("assistant", "Linux and Windows work")
        await release_worker.wait()
        return LLMResponse("assistant", "Linux works")

    rt.p.context.llm_generate.side_effect = worker_reply
    followup = {**rt.scope, "delegation_inputs": [source("b", "也查 Windows")]}
    async with session_lock_manager.acquire_lock(rt.e.unified_msg_origin):
        tid, _ = await rt.d.submit(rt.e, "bot", {"instruction": "查版本"}, rt.scope)
        worker = rt.d.workers[tid]

    args = {"task_id": str(tid), "instruction": "也查 Windows"}
    target = MessageData(
        message_id="b",
        user_id="alice",
        nickname="Alice",
        group_or_user_id="7",
        content="也查 Windows",
        time=datetime.now().isoformat(),
    )
    await rt.p.data_cache.add_message("bot", "7", target)

    async def delayed_parent_reply(**kwargs):
        assert (
            "<subagent_tasks>" in kwargs["prompt"]
            and '"status": "running"' in kwargs["prompt"]
        )
        release_worker.set()
        await asyncio.wait_for(queued.wait(), 3)
        assert "ready" in await rt.d.context("bot", rt.e.unified_msg_origin)
        return LLMResponse(
            "assistant",
            f'<tool_call name="delegate_task">{json.dumps(args, ensure_ascii=False)}</tool_call>',
        )

    rt.p.context.tool_loop_agent.side_effect = delayed_parent_reply
    async with session_lock_manager.acquire_lock(rt.e.unified_msg_origin):
        state = SessionState(current=[QueuedMessage(rt.e, target, True, 0, 3)])
        assert await rt.p.chat_manager._reply_batch(
            rt.e, "bot", "7", state, [target], None, [], None
        )
        assert state.waiting_agent
        assert await rt.d.submit(rt.e, "bot", args, followup) == (tid, True)
        async with rt.db.query(
            "SELECT revision,remaining FROM delegated_tasks WHERE id=?", (tid,)
        ) as cur:
            assert tuple(await cur.fetchone()) == (2, 3)
        wake.assert_not_awaited()
    await asyncio.wait_for(worker, 3)
    wake.assert_awaited_once()
    assert wake.await_args.args[1]["result"] == "Linux and Windows work"
    assert wake.await_args.args[2] == ["a", "b"]
    assert "Linux works" in rt.p.context.llm_generate.await_args.kwargs["prompt"]


@pytest.mark.asyncio
async def test_followup_during_last_tool_preserves_result_and_replenishes_rounds(
    rt, monkeypatch
):
    rt.p.tools_config["max_loop"] = 1
    started, release = asyncio.Event(), asyncio.Event()
    execute = rt.d._execute

    async def delayed(*args):
        await execute(*args)
        started.set()
        await release.wait()

    monkeypatch.setattr(rt.d, "_execute", delayed)
    wake = rt.p.chat_manager.deliver_delegation = AsyncMock(return_value=True)
    rt.p.context.llm_generate.side_effect = [
        LLMResponse("assistant", tools_call_name=["lookup"], tools_call_args=[{}]),
        LLMResponse("assistant", "added answer"),
        LLMResponse("assistant", "later answer"),
    ]
    tid, _ = await rt.d.submit(rt.e, "bot", {"instruction": "first"}, rt.scope)
    await asyncio.wait_for(started.wait(), 3)
    before = await rt.d.context("bot", rt.e.unified_msg_origin)
    assert '"remaining": 0' in before and LONG_RESULT not in before
    await rt.d.submit(
        rt.e,
        "bot",
        {"task_id": tid, "instruction": "more"},
        {**rt.scope, "delegation_inputs": [source("b", "more")]},
    )
    release.set()
    await asyncio.wait_for(rt.d.workers[tid], 3)
    wake.assert_awaited_once()
    assert LONG_RESULT in rt.p.context.llm_generate.await_args.kwargs["prompt"]
    # Once the report has been claimed, a new user turn can reopen the same transcript.
    await rt.d.submit(
        rt.e,
        "bot",
        {"task_id": tid, "instruction": "later"},
        {**rt.scope, "delegation_inputs": [source("c", "later")]},
    )
    await asyncio.wait_for(rt.d.workers[tid], 3)
    assert wake.await_count == 2 and wake.await_args.args[1]["revision"] == 3


@pytest.mark.asyncio
async def test_scoped_tools_budget_and_private_transcript(rt):
    rt.p.tools_config["max_loop"] = 1
    wake = rt.p.chat_manager.deliver_delegation = AsyncMock(return_value=True)
    rt.p.context.llm_generate.return_value = LLMResponse(
        "assistant", tools_call_name=["direct"], tools_call_args=[{}]
    )
    tid, _ = await rt.d.submit(rt.e, "bot", {"instruction": "lookup"}, rt.scope)
    await asyncio.wait_for(rt.d.workers[tid], 3)
    assert "上限" in wake.await_args.args[1]["result"]
    async with rt.db.query(
        "SELECT payload FROM delegated_steps WHERE kind='output'"
    ) as cur:
        assert "可用范围" in json.loads((await cur.fetchone())[0])["error"]
    receipt = await rt.p.chat_manager.action_dispatcher.tool_executor.execute_tool(
        rt.e,
        "bot",
        "Bot",
        "7",
        "lookup",
        {},
        tool_context={**rt.scope, "xml_tools": ["direct"]},
    )
    assert "主人格当前可用范围" in receipt["results"]
    rt.e.unified_msg_origin = "qq:FriendMessage:7"
    with pytest.raises(ValueError, match="当前会话没有"):
        await rt.d.submit(
            rt.e, "bot", {"task_id": tid, "instruction": "different origin"}, rt.scope
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("call_only", [False, True])
async def test_native_history_preserves_all_yields_and_error(rt, call_only):
    if call_only:
        rt.p.bot_map["bot"]["llm_reply_conf"]["call_only_tools"] = ["lookup"]
    hooks = ReplyToolHistory(rt.p, rt.e, "bot", "7")
    tool = hooks.observe(ToolSet(tools=[rt.lookup])).tools[0]
    run = AgentContextWrapper(
        context=AstrAgentContext(context=rt.p.context, event=rt.e)
    )
    await hooks.on_tool_start(run, tool, {"query": "<abc>"})
    parts = [
        part async for part in FunctionToolExecutor.execute(tool, run, query="<abc>")
    ]
    assert parts[0].content[0].text == LONG_RESULT
    await hooks.on_tool_end(run, tool, {"query": "<abc>"}, parts[-1])
    await hooks.on_tool_start(run, tool, {"query": "second"})
    await hooks.finish(RuntimeError("failed after start"))
    async with rt.db.query("SELECT content FROM chat_history ORDER BY id") as cur:
        entries = [fromstring(row[0]) for row in await cur.fetchall()]
    assert [e.get("status") for e in entries] == [
        "started",
        "finished",
        "started",
        "failed",
    ]
    if call_only:
        assert all(e.get("name") == "lookup" and e.text is None for e in entries)
        assert all(set(e.attrib) == {"name", "status"} for e in entries)
    else:
        assert entries[0].get("id") == entries[1].get("id") != entries[2].get("id")
        assert json.loads(entries[0].text)["arguments"] == {"query": "<abc>"}
        assert LONG_RESULT in entries[1].text and "evidence-42" in entries[1].text
        assert "failed after start" in entries[-1].text


@pytest.mark.asyncio
async def test_private_transcript_reachable_only_from_receipt_and_removed_with_chat(rt):
    rt.p.context.llm_generate.return_value = LLMResponse(
        "assistant", "only the conclusion"
    )
    rt.p.chat_manager.deliver_delegation = AsyncMock(return_value=True)
    response = await rt.p.chat_manager.action_dispatcher.tool_executor.execute_tool(
        rt.e, "bot", "Bot", "7", "delegate_task", {"instruction": "first"}, rt.scope
    )
    await asyncio.wait_for(rt.d.workers[response["delegated"]], 3)
    assert await rt.d.submit(rt.e, "bot", {"instruction": "first"}, rt.scope) == (
        response["delegated"],
        False,
    )
    async with rt.db.query(
        "SELECT id FROM chat_history WHERE role='operation_log'"
    ) as cur:
        receipt = (await cur.fetchone())[0]
    api = ChatHistoryApi()
    api.giftia = rt.p
    with bind_request_context(
        PluginRequest(
            Request(
                {
                    "type": "http",
                    "method": "GET",
                    "path": "/chat_history/delegation",
                    "headers": [],
                    "query_string": f"receipt={receipt}".encode(),
                }
            )
        )
    ):
        result = json.loads((await api.get_delegated_task()).body)
        assert [step["kind"] for step in result["data"]["steps"]] == ["input", "report"]
        assert result["data"]["status"] == "delivered"
        await rt.db.delete_chat_history("bot", "7")
        assert (await api.get_delegated_task()).status_code == 400
    async with rt.db.query("SELECT count(*) FROM delegated_steps") as cur:
        assert (await cur.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_pending_tasks_stop_on_shutdown_and_never_send_late(rt):
    entered, held = asyncio.Event(), asyncio.Event()

    async def paused(**kwargs):
        entered.set()
        await held.wait()

    rt.p.context.llm_generate.side_effect = paused
    rt.p.chat_manager.deliver_delegation = AsyncMock()
    tid, _ = await rt.d.submit(rt.e, "bot", {"instruction": "long task"}, rt.scope)
    await asyncio.wait_for(entered.wait(), 3)
    await rt.d.close()
    rt.p.chat_manager.deliver_delegation.assert_not_awaited()
    async with rt.db.query(
        "SELECT status FROM delegated_tasks WHERE id=?", (tid,)
    ) as cur:
        assert (await cur.fetchone())[0] == "interrupted"


@pytest.mark.asyncio
async def test_budget_exhaustion_returns_evidence_without_running_more_tools(rt):
    rt.p.tools_config["max_loop"] = 1
    wake = rt.p.chat_manager.deliver_delegation = AsyncMock(return_value=True)
    rt.p.context.llm_generate.side_effect = [
        LLMResponse("assistant", tools_call_name=["lookup"], tools_call_args=[{}]),
        LLMResponse("assistant", "From evidence-42"),
    ]
    tid, _ = await rt.d.submit(rt.e, "bot", {"instruction": "first"}, rt.scope)
    await asyncio.wait_for(rt.d.workers[tid], 3)
    assert wake.await_args.args[1]["result"] == "From evidence-42"
    last = rt.p.context.llm_generate.await_args.kwargs
    assert last["tools"] is None and "evidence-42" in last["prompt"]
