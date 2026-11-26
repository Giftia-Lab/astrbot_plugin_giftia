from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from core.database.data_cache import DataCache
from core.llm.call_llm import CallLLM
from core.llm.xml_parse import XmlParse
from core.utils.schemas import FeatureKey

from astrbot.core.provider.entities import LLMResponse


@pytest.fixture
def runtime():
    model = AsyncMock(
        return_value=LLMResponse("assistant", completion_text="<message>ok</message>")
    )
    event = SimpleNamespace(
        platform_meta=SimpleNamespace(id="adapter"),
        get_platform_name=Mock(return_value="aiocqhttp"),
        get_group_id=Mock(return_value="123"),
        get_self_id=Mock(return_value="456"),
        is_admin=Mock(return_value=True),
        bot=SimpleNamespace(call_action=AsyncMock()),
    )
    bot_config = {}
    cache = DataCache(None, None, None)
    plugin = SimpleNamespace(
        bot_map={"bot": bot_config},
        adapter_id_map={"adapter": "bot"},
        data_cache=cache,
    )
    caller = CallLLM(
        SimpleNamespace(tool_loop_agent=model),
        XmlParse(None, None),
        {"reply_retry_times": 1},
        {},
        plugin=plugin,
    )
    caller._log_token_usage_safely = AsyncMock()

    async def reply():
        result = await caller.call_llm_reply(
            event, "session", ["provider"], "persona", "input"
        )
        assert result.msg_texts == ["ok"]
        return model.await_args.kwargs["system_prompt"]

    return SimpleNamespace(event=event, bot_config=bot_config, cache=cache, reply=reply)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "snapshot", [{"owner_id": "456"}, {"owner_id": "789", "admin_ids": [456]}]
)
async def test_onebot_group_admin_tools_use_the_current_bots_cached_role(
    runtime, snapshot
):
    event = runtime.event
    runtime.cache.group_metadata = {
        ("bot", "123"): {"onebot_snapshot": snapshot},
        ("bot", "321"): {"onebot_snapshot": {"owner_id": "789", "admin_ids": []}},
        ("other-bot", "321"): {"onebot_snapshot": snapshot},
    }
    for group_id, allowed in [("123", True), ("321", False)]:
        event.get_group_id.return_value = group_id
        prompt = await runtime.reply()
        assert ("<ban " in prompt) is allowed
        assert ("<kick " in prompt) is allowed
        assert "<delete " in prompt

    event.bot.call_action.assert_not_awaited()
    event.is_admin.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("group_id", "features", "snapshot"),
    [
        ("123", [FeatureKey.DELETE], {"owner_id": "456"}),
        ("", None, {"owner_id": "456"}),
        ("123", None, None),
        ("123", None, {"admin_ids": "4567"}),
    ],
)
async def test_onebot_group_admin_tools_require_enabled_feature_and_known_role(
    runtime, group_id, features, snapshot
):
    event = runtime.event
    event.get_group_id.return_value = group_id
    runtime.bot_config["enabled_interactive_features"] = features
    runtime.cache.group_metadata[("bot", group_id)] = {"onebot_snapshot": snapshot}

    prompt = await runtime.reply()
    assert "<ban " not in prompt and "<kick " not in prompt
    assert "<delete " in prompt
    event.bot.call_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_official_qq_group_ban_follows_feature_config(runtime):
    runtime.bot_config["enabled_interactive_features"] = [FeatureKey.GROUP_ADMIN]
    event = runtime.event
    event.get_platform_name.return_value = "qq_official_webhook"

    prompt = await runtime.reply()
    assert "<ban " in prompt
    assert "<kick " not in prompt
    event.get_group_id.assert_not_called()
    event.bot.call_action.assert_not_awaited()
