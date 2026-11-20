"""Verify model inventory and successful manual recovery through real routes."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from core.llm.provider_health import (
    ProviderHealth,
    ProviderUnavailableError,
    get_provider_health,
)
from core.web.model_health_api import ModelHealthApi
from fastapi import FastAPI, Request

from astrbot.api.web import PluginRequest, bind_request_context


class ServiceError(Exception):
    status_code = 503


@pytest.fixture
def runtime():
    provider_ids = [
        "shared",
        "reply",
        "backup",
        "review",
        "completion",
        "subagent",
        "media",
        "inspect",
        "summary",
        "unused",
    ]
    context = SimpleNamespace(
        llm_generate=AsyncMock(
            side_effect=lambda **kwargs: SimpleNamespace(
                completion_text=kwargs["prompt"].rsplit("\n", 1)[-1]
            )
        ),
        provider_manager=SimpleNamespace(
            inst_map={
                provider_id: SimpleNamespace(
                    provider_config={
                        "name": f"Name {provider_id}",
                        "api_key": "sk-secret-never-publish",
                    },
                    model_name=f"model-{provider_id}",
                )
                for provider_id in provider_ids
            }
        ),
    )
    plugin = SimpleNamespace(
        context=context,
        bot_map={
            "bot": {
                "decision_conf": {
                    "provider_ids": ["shared"],
                    "session_completion_provider_id": "completion",
                },
                "llm_reply_conf": {
                    "provider_ids": ["reply", "shared", "missing"],
                    "invalid_reply_fallback_provider_ids": ["backup"],
                    "review_provider_id": "review",
                    "subagent_provider_ids": ["subagent", "shared"],
                },
            }
        },
        caption_config={
            "media_caption_provider_ids": ["media", "shared"],
            "inspect_media_provider_ids": ["inspect"],
        },
        passive_memory_provider_ids=["summary", "shared"],
        db=SimpleNamespace(
            log_token_usage=AsyncMock(),
            get_kv_data=AsyncMock(return_value=None),
            upsert_kv_data=AsyncMock(),
        ),
    )
    api = ModelHealthApi(plugin)
    app = FastAPI()

    @app.api_route("/models/{action}", methods=["GET", "POST"])
    async def dispatch(action: str, request: Request):
        with bind_request_context(PluginRequest(request)):
            handlers = {
                "health": api.get_model_health,
                "test": api.test_model_connection,
                "settings": api.update_model_health_settings,
                "prompt-suffix": api.update_model_prompt_suffix,
            }
            return await handlers[action]()

    return SimpleNamespace(plugin=plugin, app=app, health=get_provider_health(plugin))


@pytest.mark.asyncio
async def test_inventory_deduplicates_ids_covers_all_roles_and_omits_credentials(
    runtime,
):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(runtime.app), base_url="http://test"
    ) as client:
        response = await client.get("/models/health")
    assert response.json()["data"]["enabled"] is True
    assert response.json()["data"]["prompt_suffix"] == {
        "text": "",
        "provider_ids": [],
    }
    rows = {row["provider_id"]: row for row in response.json()["data"]["models"]}
    assert set(rows) == {
        "shared",
        "reply",
        "backup",
        "review",
        "completion",
        "subagent",
        "media",
        "inspect",
        "summary",
        "missing",
    }
    assert set(rows["shared"]["uses"]) == {
        "bot · 前置决策",
        "bot · 正式回复/转发转述",
        "bot · 子 Agent",
        "媒体转述",
        "记忆/好感度归纳",
    }
    assert rows["missing"]["registered"] is False
    assert rows["reply"]["model"] == "model-reply"
    assert rows["reply"]["uses"] == ["bot · 正式回复/转发转述"]
    assert rows["completion"]["uses"] == ["bot · 会话防抖判断"]
    assert rows["subagent"]["uses"] == ["bot · 子 Agent"]
    assert "sk-secret" not in response.text
    runtime.plugin.context.llm_generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_successful_connectivity_test_bypasses_cooldown_and_restores_shared_state(
    runtime,
):
    await runtime.health.update_prompt_suffix(
        runtime.plugin.db,
        text="Explain the conversation context.",
        provider_ids={"shared"},
    )
    for _ in range(2):
        with pytest.raises(ServiceError):
            await runtime.health.call(
                AsyncMock(side_effect=ServiceError()), chat_provider_id="shared"
            )
    assert runtime.health.snapshot("shared")["status"] == "cooldown"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(runtime.app), base_url="http://test"
    ) as client:
        result = (
            await client.post("/models/test", json={"provider_id": "shared"})
        ).json()
        rows = (await client.get("/models/health")).json()["data"]["models"]
    assert result["status"] == "success"
    assert result["data"]["status"] == "healthy"
    assert result["data"]["failures"] == 0
    assert (
        next(row for row in rows if row["provider_id"] == "shared")["status"]
        == "healthy"
    )
    runtime.plugin.context.llm_generate.assert_awaited_once()
    assert (
        "Explain the conversation context."
        not in (runtime.plugin.context.llm_generate.await_args.kwargs["prompt"])
    )
    usage = runtime.plugin.db.log_token_usage.await_args.kwargs
    assert usage["type"] == "provider_test"
    assert usage["extra_info"]["status"] == "success"


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", [False, True])
async def test_failed_manual_test_never_restores_health(runtime, empty):
    for _ in range(2):
        with pytest.raises(ServiceError):
            await runtime.health.call(
                AsyncMock(side_effect=ServiceError()), chat_provider_id="shared"
            )
    if empty:
        runtime.plugin.context.llm_generate.side_effect = None
        runtime.plugin.context.llm_generate.return_value = SimpleNamespace(
            completion_text=""
        )
    else:
        runtime.plugin.context.llm_generate.side_effect = ServiceError(
            "sk-secret-never-publish"
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(runtime.app), base_url="http://test"
    ) as client:
        response = await client.post("/models/test", json={"provider_id": "shared"})
    assert response.json()["status"] == "error"
    assert response.json()["data"]["status"] == "cooldown"
    assert "sk-secret" not in response.text
    assert (
        runtime.plugin.db.log_token_usage.await_args.kwargs["extra_info"]["status"]
        == "api_failed"
    )


@pytest.mark.asyncio
async def test_concurrent_tests_allow_only_one_provider_request(runtime):
    entered, release = asyncio.Event(), asyncio.Event()

    async def generate(**kwargs):
        entered.set()
        await release.wait()
        return SimpleNamespace(completion_text=kwargs["prompt"].rsplit("\n", 1)[-1])

    runtime.plugin.context.llm_generate.side_effect = generate
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(runtime.app), base_url="http://test"
    ) as client:
        first = asyncio.create_task(
            client.post("/models/test", json={"provider_id": "shared"})
        )
        await entered.wait()
        second = await client.post("/models/test", json={"provider_id": "shared"})
        assert second.status_code == 400
        runtime.plugin.context.llm_generate.assert_awaited_once()
        release.set()
        assert (await first).json()["data"]["status"] == "healthy"
    runtime.plugin.db.log_token_usage.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload", [{}, {"provider_id": "unused"}, {"provider_id": ["shared"]}, []]
)
async def test_unknown_or_malformed_test_targets_are_rejected_without_requests(
    runtime, payload
):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(runtime.app), base_url="http://test"
    ) as client:
        response = await client.post("/models/test", json=payload)
    assert response.status_code == 400
    runtime.plugin.context.llm_generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_inventory_includes_models_configured_for_disabled_bots(runtime):
    runtime.plugin.bot_config_manager = SimpleNamespace(
        bots=[
            {
                "name": "disabled",
                "enabled": False,
                "llm_reply_conf": {
                    "provider_ids": ["reply"],
                    "subagent_provider_ids": ["subagent"],
                },
            }
        ]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(runtime.app), base_url="http://test"
    ) as client:
        rows = {
            row["provider_id"]: row
            for row in (await client.get("/models/health")).json()["data"]["models"]
        }
    assert rows["reply"]["uses"] == ["disabled（已停用） · 正式回复/转发转述"]
    assert rows["subagent"]["uses"] == ["disabled（已停用） · 子 Agent"]


@pytest.mark.asyncio
async def test_inventory_marks_inherited_subagent_providers_only_when_delegation_enabled(
    runtime,
):
    runtime.plugin.bot_config_manager = SimpleNamespace(
        bots=[
            {
                "name": "inherited",
                "enabled": True,
                "llm_reply_conf": {
                    "provider_ids": ["reply", "shared"],
                    "subagent_provider_ids": [],
                    "subagent_tools": ["lookup"],
                },
            },
            {
                "name": "disabled-delegation",
                "enabled": True,
                "llm_reply_conf": {
                    "provider_ids": ["backup"],
                    "subagent_provider_ids": [],
                    "subagent_tools": [],
                },
            },
        ]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(runtime.app), base_url="http://test"
    ) as client:
        rows = {
            row["provider_id"]: row
            for row in (await client.get("/models/health")).json()["data"]["models"]
        }
    assert rows["reply"]["uses"] == [
        "inherited · 正式回复/转发转述",
        "inherited · 子 Agent",
    ]
    assert set(rows["shared"]["uses"]) >= {
        "inherited · 正式回复/转发转述",
        "inherited · 子 Agent",
    }
    assert rows["backup"]["uses"] == ["disabled-delegation · 正式回复/转发转述"]


@pytest.mark.asyncio
async def test_subagent_only_provider_supports_manual_test_and_toggle(runtime):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(runtime.app), base_url="http://test"
    ) as client:
        tested = await client.post("/models/test", json={"provider_id": "subagent"})
        disabled = await client.post(
            "/models/settings", json={"provider_id": "subagent", "enabled": False}
        )
    assert tested.status_code == 200
    assert tested.json()["status"] == "success"
    assert tested.json()["data"]["status"] == "healthy"
    assert disabled.status_code == 200
    subagent = next(
        row
        for row in disabled.json()["data"]["models"]
        if row["provider_id"] == "subagent"
    )
    assert subagent["status"] == "disabled" and subagent["enabled"] is False


@pytest.mark.asyncio
async def test_background_probe_and_manual_test_share_one_request_slot(runtime):
    health = runtime.health
    ticks = [100.0]
    health.clock = lambda: ticks[0]
    health.CHECK_INTERVAL = 0.01
    for _ in range(2):
        with pytest.raises(ServiceError):
            await health.call(
                AsyncMock(side_effect=ServiceError()), chat_provider_id="shared"
            )
    entered, release, logged = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def generate(**kwargs):
        entered.set()
        await release.wait()
        return SimpleNamespace(completion_text=kwargs["prompt"].rsplit("\n", 1)[-1])

    runtime.plugin.context.llm_generate.side_effect = generate
    runtime.plugin.db.log_token_usage.side_effect = lambda **_: logged.set()
    ticks[0] += 60
    health.start(runtime.plugin.db.log_token_usage)
    try:
        await asyncio.wait_for(entered.wait(), 1)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(runtime.app), base_url="http://test"
        ) as client:
            response = await client.post("/models/test", json={"provider_id": "shared"})
        assert response.status_code == 400
        assert "正在进行" in response.json()["message"]
        runtime.plugin.context.llm_generate.assert_awaited_once()
        runtime.plugin.db.log_token_usage.assert_not_awaited()
        release.set()
        await asyncio.wait_for(logged.wait(), 1)
        assert health.snapshot("shared")["status"] == "healthy"
        assert runtime.plugin.db.log_token_usage.await_args.kwargs["extra_info"] == {
            "status": "success",
            "manual_test": False,
        }
    finally:
        await health.close()


@pytest.mark.asyncio
async def test_controls_persist_across_database_reopen_and_block_all_tests(
    runtime, tmp_path
):
    import aiosqlite
    from core.database.database import Database
    from core.database.schema import initialize_database

    path = tmp_path / "health.db"
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    try:
        await initialize_database(conn)
        runtime.plugin.db = Database(conn)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(runtime.app), base_url="http://test"
        ) as client:
            suffix_text = "补充群聊语境。\n沿用当前输出格式。"
            suffix_settings = await client.post(
                "/models/prompt-suffix",
                json={
                    "text": f" {suffix_text}\n",
                    "provider_ids": ["shared", "reply", "reply"],
                },
            )
            assert suffix_settings.json()["data"]["prompt_suffix"] == {
                "text": suffix_text,
                "provider_ids": ["reply", "shared"],
            }
            disabled = await client.post(
                "/models/settings", json={"provider_id": "shared", "enabled": False}
            )
            assert disabled.json()["status"] == "success"
            row = next(
                row
                for row in disabled.json()["data"]["models"]
                if row["provider_id"] == "shared"
            )
            assert row["status"] == "disabled" and row["enabled"] is False
            response = await client.post("/models/test", json={"provider_id": "shared"})
            assert (
                response.status_code == 400 and "手动停用" in response.json()["message"]
            )
            off = await client.post("/models/settings", json={"enabled": False})
            assert off.json()["data"]["enabled"] is False
            response = await client.post("/models/test", json={"provider_id": "reply"})
            assert (
                response.status_code == 400 and "已关闭" in response.json()["message"]
            )
        runtime.plugin.context.llm_generate.assert_not_awaited()
    finally:
        await conn.close()
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    try:
        restarted = ProviderHealth(runtime.plugin.context)
        await restarted.load_settings(Database(conn))
        assert restarted.enabled is False
        assert restarted.disabled_provider_ids == {"shared"}
        assert restarted.prompt_suffix_provider_ids == {"reply", "shared"}
        blocked = AsyncMock()
        with pytest.raises(ProviderUnavailableError):
            await restarted.call(blocked, chat_provider_id="shared")
        blocked.assert_not_awaited()
        business = AsyncMock(return_value="reply")
        await restarted.call(business, chat_provider_id="reply", prompt="User input")
        business.assert_awaited_once_with(
            chat_provider_id="reply",
            prompt=f"User input\n\n{suffix_text}",
            request_max_retries=1,
        )
    finally:
        await conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"enabled": "false"},
        {"enabled": 0},
        {"enabled": False, "provider_id": "unused"},
        {"enabled": True, "provider_id": None},
        {"enabled": False, "provider_id": ["shared"]},
    ],
)
async def test_invalid_control_updates_are_rejected(runtime, payload):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(runtime.app), base_url="http://test"
    ) as client:
        response = await client.post("/models/settings", json=payload)
    assert response.status_code == 400
    runtime.plugin.db.upsert_kv_data.assert_not_awaited()
    assert runtime.health.enabled is True
    assert runtime.health.disabled_provider_ids == set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"text": None, "provider_ids": ["shared"]},
        {"text": "context", "provider_ids": [["shared"]]},
        {"text": "context", "provider_ids": ["unused"]},
    ],
)
async def test_invalid_prompt_suffix_settings_are_rejected(runtime, payload):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(runtime.app), base_url="http://test"
    ) as client:
        response = await client.post("/models/prompt-suffix", json=payload)
    assert response.status_code == 400
    runtime.plugin.db.upsert_kv_data.assert_not_awaited()
    assert runtime.health.prompt_suffix == ""


@pytest.mark.asyncio
async def test_failed_persistence_does_not_change_runtime_controls(runtime):
    runtime.plugin.db.upsert_kv_data.side_effect = RuntimeError("Database unavailable")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(runtime.app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/models/settings", json={"provider_id": "shared", "enabled": False}
        )
    assert response.status_code == 400
    assert "保存" in response.json()["message"]
    assert runtime.health.snapshot("shared")["enabled"] is True


@pytest.mark.asyncio
async def test_concurrent_updates_keep_both_disabled_providers(runtime):
    async def save(*args):
        await asyncio.sleep(0.01)

    runtime.plugin.db.upsert_kv_data.side_effect = save
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(runtime.app), base_url="http://test"
    ) as client:
        results = await asyncio.gather(
            *[
                client.post(
                    "/models/settings",
                    json={"provider_id": provider_id, "enabled": False},
                )
                for provider_id in ("shared", "reply")
            ]
        )
    assert all(response.json()["status"] == "success" for response in results)
    assert runtime.health.disabled_provider_ids == {"shared", "reply"}
    saved = json.loads(runtime.plugin.db.upsert_kv_data.await_args.args[1])
    assert saved["disabled_provider_ids"] == ["reply", "shared"]


@pytest.mark.asyncio
async def test_invalid_manual_probe_reports_safe_reason_and_enters_cooldown(runtime):
    runtime.plugin.context.llm_generate.side_effect = None
    runtime.plugin.context.llm_generate.return_value = SimpleNamespace(
        completion_text="\x00sk-secret-never-publish�"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(runtime.app), base_url="http://test"
    ) as client:
        response = await client.post("/models/test", json={"provider_id": "shared"})
    body = response.json()
    assert body["status"] == "error"
    assert "严格格式校验" in body["message"]
    assert body["data"]["status"] == "cooldown"
    assert body["data"]["reason"] == "invalid_probe_response"
    assert "sk-secret" not in response.text


@pytest.mark.asyncio
async def test_disabling_during_manual_probe_cancels_request_and_keeps_disabled(
    runtime,
):
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def generate(**kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    runtime.plugin.context.llm_generate.side_effect = generate
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(runtime.app), base_url="http://test"
    ) as client:
        pending = asyncio.create_task(
            client.post("/models/test", json={"provider_id": "shared"})
        )
        await entered.wait()
        response = await client.post(
            "/models/settings", json={"provider_id": "shared", "enabled": False}
        )
        assert response.json()["status"] == "success"
        assert (await pending).status_code == 400
    assert cancelled.is_set()
    assert runtime.health.snapshot("shared")["status"] == "disabled"
