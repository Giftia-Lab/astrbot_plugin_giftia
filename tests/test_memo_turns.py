"""Verify conversational memos and session context across tool turns."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from core.conversation.action_dispatcher import ActionDispatcher
from core.conversation.reply_pipeline import ReplyPipeline
from core.llm.preset_prompts import build_xml_instructions
from core.llm.xml_parse import XmlParse
from core.utils.scheduler import Scheduler
from core.utils.schemas import MessageData, Status

from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)


@pytest.fixture
def runtime():
    history = [
        MessageData(
            message_id="A", user_id="alice", nickname="Alice", content="Remind Bob"
        ),
        MessageData(
            message_id="B", user_id="bob", nickname="Bob", content="Unrelated reply"
        ),
    ]

    async def add_message(bot, session, message, **kwargs):
        history.append(message)

    bot_conf = {"llm_reply_conf": {"provider_ids": ["reply"]}}
    plugin = SimpleNamespace(
        bot_map={"bot": bot_conf},
        get_bot_config=lambda _: bot_conf,
        tools_config={"max_loop": 3},
        embedding_conf={"enabled": False},
        msg_number=20,
        data_cache=SimpleNamespace(
            get_recent_message=AsyncMock(side_effect=lambda *args: list(history)),
            get_bot_status=AsyncMock(return_value=Status()),
            set_bot_status=AsyncMock(),
            get_group_profile=AsyncMock(return_value=""),
            get_user_profile_record=AsyncMock(return_value={}),
            get_user_relation=AsyncMock(return_value=None),
            build_active_user_briefs=AsyncMock(return_value=[]),
            is_bot_muted=Mock(return_value=False),
            add_message=AsyncMock(side_effect=add_message),
        ),
        memos=SimpleNamespace(
            list_active=AsyncMock(return_value=[]),
            limit=3,
            apply=AsyncMock(return_value="memo42"),
        ),
        task_manager=SimpleNamespace(
            add_job=Mock(return_value="created"),
            get_prefix_jobs=Mock(return_value=[]),
            get_session_jobs_data=Mock(return_value=[]),
        ),
        emoji_manager=SimpleNamespace(get_random_stickers=AsyncMock(return_value=[])),
        context=SimpleNamespace(
            persona_manager=SimpleNamespace(
                get_persona_v3_by_id=lambda _: {"prompt": "persona"}
            )
        ),
        db=SimpleNamespace(
            get_user_common_groups=AsyncMock(return_value={}),
            get_relation_changes=AsyncMock(return_value=[]),
            get_relation_score=AsyncMock(return_value={}),
            slang_repo=SimpleNamespace(get_entries=AsyncMock(return_value=[])),
            update_message_reply_decision=AsyncMock(),
        ),
        call_llm=SimpleNamespace(call_llm_reply=AsyncMock()),
    )
    event = MagicMock(spec=AiocqhttpMessageEvent)
    event.platform_meta = SimpleNamespace(id="adapter")
    event.unified_msg_origin = "adapter:GroupMessage:group"
    event.get_platform_name.return_value = "aiocqhttp"
    event.get_group_id.return_value = "group"
    event.get_self_id.return_value = "bot-id"
    event.get_sender_id.return_value = "bob"
    event.get_sender_name.return_value = "Bob"
    event.get_group = AsyncMock(return_value="")
    pipeline = ReplyPipeline(plugin)
    pipeline.media_captioner.get_cached_media_captions = AsyncMock(return_value=[])
    dispatcher = ActionDispatcher(plugin)
    dispatcher._dispatch_aiocqhttp_outputs = AsyncMock()
    dispatcher._dispatch_generic_outputs = AsyncMock()
    parser = XmlParse(None, None)

    async def run(*xmls, **kwargs):
        results = [await parser.decode_llm_xml(xml, "group") for xml in xmls]
        assert all(result is not None for result in results)
        plugin.call_llm.call_llm_reply.side_effect = results
        chunks = []
        async for chunk in pipeline.dispatch_llm_reply_loop(
            event, "bot", "Bot", "group", **kwargs
        ):
            chunks.append(chunk)
            await dispatcher.dispatch_actions(event, "bot", "Bot", "group", chunk)
        return chunks

    return SimpleNamespace(
        plugin=plugin,
        event=event,
        history=history,
        run=run,
        pipeline=pipeline,
        dispatcher=dispatcher,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["aiocqhttp", "generic"])
async def test_model_manages_its_own_memos_without_creator(runtime, platform):
    runtime.event.get_platform_name.return_value = platform
    await runtime.run(
        '<memo action="add" duration="2d">A plan</memo>'
        '<memo action="renew" id="memo42" duration="30min"/>'
        '<memo action="delete" id="memo42"/>'
    )
    calls = runtime.plugin.memos.apply.await_args_list
    assert [c.kwargs["action"] for c in calls] == ["add", "renew", "delete"]
    assert all(c.args == ("bot", "group") for c in calls)
    assert calls[0].kwargs["content"] == "A plan"
    assert calls[1].kwargs["memo_id"] == "memo42"


@pytest.mark.asyncio
async def test_failed_memo_can_be_corrected_without_replaying_completed_actions(
    runtime,
):
    frozen = list(runtime.history)
    runtime.history.append(MessageData(user_id="late", nickname="Late"))
    runtime.plugin.memos.apply.side_effect = ["first", ValueError("时长错误"), "second"]
    chunks = await runtime.run(
        '<memo action="add" duration="1h">First</memo><memo action="add" duration="bad">Second</memo>',
        '<memo action="add" duration="2h">Second</memo>',
        recent_messages=frozen,
        reply_messages=frozen,
    )
    assert [
        c.kwargs["content"] for c in runtime.plugin.memos.apply.await_args_list
    ] == ["First", "Second", "Second"]
    assert len(chunks) == 2
    assert "时长错误" in chunks[0].xml_tool_results[0]["results"]
    first, second = runtime.plugin.call_llm.call_llm_reply.await_args_list
    assert "时长错误" in second.kwargs["user_prompt"]
    context = first.kwargs["tool_context"]
    assert context is second.kwargs["tool_context"] and context["request_id"]
    assert context["user_ids"] == ["alice", "bob"]
    assert "late" not in context["creator_names"]


def test_xml_instructions_show_optional_pin_and_memo_operations():
    prompt = build_xml_instructions(enabled_features=None)
    assert '<status pin="true">' in prompt and "数字是 4" in prompt
    for action in ("add", "renew", "delete"):
        assert f'<memo action="{action}"' in prompt
    assert "<task_board" not in prompt
    assert '<memo action="add"' not in build_xml_instructions(enabled_features=[])


def test_scheduled_query_displays_stored_creator_without_modifying_legacy_job():
    kwargs = {"user_id": "alice", "user_name": "Alice", "remind_message": "For Bob"}
    scheduler = Scheduler.__new__(Scheduler)
    scheduler.scheduler = SimpleNamespace(
        get_jobs=lambda: [
            SimpleNamespace(id="bot_group_123", name="remind", kwargs=kwargs)
        ]
    )
    assert "创建者：Alice(alice)" in scheduler.get_prefix_jobs("bot_group")[0]
    assert kwargs == {
        "user_id": "alice",
        "user_name": "Alice",
        "remind_message": "For Bob",
    }
