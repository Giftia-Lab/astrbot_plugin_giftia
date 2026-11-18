import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import httpx
import pytest
import pytest_asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from core.database.database import Database
from core.database.schema import initialize_database
from core.utils.memos import MemoManager
from core.utils.scheduler import Scheduler
from core.utils.schemas import Memo
from core.web.webui_manager import WebUIManager
from fastapi import FastAPI, Request

from astrbot.api.web import PluginRequest, bind_request_context


@pytest_asyncio.fixture
async def task_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRBOT_ROOT", str(tmp_path / "runtime"))
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await initialize_database(conn)
        scheduler = Scheduler.__new__(Scheduler)
        scheduler.registered_funcs = {"remind": AsyncMock()}
        scheduler.scheduler = AsyncIOScheduler()
        scheduler.scheduler.start(paused=True)
        plugin = SimpleNamespace(
            db=Database(conn),
            tools_config={"task_board_max_active": 3},
            task_manager=scheduler,
            get_bot_config=lambda name=None: {"nickname": "Giftia"},
            context=MagicMock(),
        )
        plugin.memos = MemoManager(plugin)
        manager = WebUIManager(plugin)
        manager.register_routes()
        routes = {
            call.kwargs["route"]: call.kwargs
            for call in plugin.context.register_web_api.call_args_list
        }
        app = FastAPI()

        @app.api_route("/{endpoint:path}", methods=["GET", "POST"])
        async def dispatch(endpoint: str, request: Request):
            route = routes[f"/astrbot_plugin_giftia/{endpoint}"]
            assert request.method in route["methods"]
            with bind_request_context(PluginRequest(request)):
                return await route["view_handler"]()

        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://test"
            ) as client:
                yield plugin, client
        finally:
            scheduler.scheduler.shutdown(wait=False)


@pytest.mark.parametrize(
    ("seconds", "label"),
    [
        (172800, "2 天"),
        (86400, "1 天"),
        (86399, "23 h"),
        (3600, "1 h"),
        (3599, "59 min"),
        (60, "1 min"),
        (59, "不足 1 min"),
    ],
)
def test_memo_remaining_units(seconds, label):
    assert Memo("id", "text", "", seconds).remaining == label


@pytest.mark.asyncio
async def test_memo_lifecycle_is_scoped_and_renewals_add_to_deadline(task_runtime):
    plugin, client = task_runtime
    scope = {"bot_name": "Giftia", "group_or_user_id": "g1"}
    res = await client.post(
        "/memos/add", json={**scope, "content": "  A plan  ", "duration": "2d"}
    )
    assert res.status_code == 200
    memo_id = res.json()["data"]["memo_id"]
    target = {**scope, "memo_id": memo_id}
    for other in ({"bot_name": "Other"}, {"group_or_user_id": "elsewhere"}):
        for action in ("delete", "renew"):
            assert (
                await client.post(
                    f"/memos/{action}", json={**target, **other, "duration": "1h"}
                )
            ).status_code == 400

    [before] = (await client.get("/memos", params=scope)).json()["data"]["items"]
    assert before["content"] == "A plan"
    assert 172795 <= before["remaining_seconds"] <= 172800
    assert "creator_user_id" not in before and before["created_at"]
    responses = await asyncio.gather(
        *[
            client.post("/memos/renew", json={**target, "duration": "2h"})
            for _ in range(2)
        ]
    )
    assert all(response.status_code == 200 for response in responses)
    [after] = (await client.get("/memos", params=scope)).json()["data"]["items"]
    assert after["created_at"] == before["created_at"]
    assert abs(after["remaining_seconds"] - before["remaining_seconds"] - 14400) < 3
    assert (await client.post("/memos/delete", json=target)).status_code == 200
    assert await plugin.memos.list_active("Giftia", "g1") == []


@pytest.mark.asyncio
async def test_expired_and_archived_tasks_free_capacity_and_live_tasks_survive(
    task_runtime,
):
    plugin, client = task_runtime
    scope = {"bot_name": "Giftia", "group_or_user_id": "g1"}
    for memo_id, status, duration in (
        ("old", "active", "+1 day"),
        ("past", "active", "-1 minute"),
        ("done", "completed", "+2 hours"),
    ):
        await plugin.db.conn.execute(
            """INSERT INTO short_tasks(task_id, bot_name, group_or_user_id, creator_user_id,
                   content, created_at, expires_at, status)
               VALUES (?, 'Giftia', 'g1', 'owner', 'legacy', datetime('now','localtime'),
                   datetime('now', ?, 'localtime'), ?)""",
            (memo_id, duration, status),
        )
    await plugin.db.conn.commit()
    assert [m.memo_id for m in await plugin.memos.list_active("Giftia", "g1")] == [
        "old"
    ]
    assert (
        await client.post(
            "/memos/renew", json={**scope, "memo_id": "past", "duration": "5min"}
        )
    ).status_code == 400
    responses = await asyncio.gather(
        *[
            client.post("/memos/add", json={**scope, "content": f"plan {index}"})
            for index in range(12)
        ]
    )
    assert sum(r.status_code == 200 for r in responses) == 2
    assert len(await plugin.memos.list_active("Giftia", "g1")) == 3
    assert all("上限" in r.json()["message"] for r in responses if r.status_code != 200)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"bot_name": ""},
        {"group_or_user_id": ""},
        {"content": "  "},
        {"duration": "tomorrow"},
        {"duration": "0h"},
        {"duration": "-3min"},
    ],
)
async def test_memo_rejects_invalid_creation(task_runtime, change):
    plugin, client = task_runtime
    res = await client.post(
        "/memos/add",
        json={
            "bot_name": "Giftia",
            "group_or_user_id": "g1",
            "content": "Plan",
            **change,
        },
    )
    assert res.status_code == 400
    assert await plugin.memos.list_active("Giftia", "g1") == []


@pytest.mark.asyncio
async def test_memo_feature_switch_uses_owning_bot(task_runtime):
    plugin, client = task_runtime
    plugin.get_bot_config = lambda bot: {
        "enabled_interactive_features": ["memo"] if bot == "Giftia" else []
    }
    for bot, code in (("Giftia", 200), ("Disabled", 400)):
        scope = {"bot_name": bot, "group_or_user_id": "group"}
        assert (
            await client.post("/memos/add", json={**scope, "content": "Plan"})
        ).status_code == code
        res = (await client.get("/memos", params=scope)).json()["data"]
        assert res["enabled"] is (bot == "Giftia")


@pytest.mark.asyncio
@pytest.mark.parametrize("group_id", ["g1", ""])
@pytest.mark.parametrize("use_existing_job", [False, True])
async def test_create_scheduled_task_preserves_real_routing(
    task_runtime, group_id, use_existing_job
):
    plugin, client = task_runtime
    context = {
        "unified_msg_origin": "adapter:GroupMessage:g1"
        if group_id
        else "adapter:FriendMessage:g1",
        "adapter_id": "adapter",
        "self_id": "bot-account",
        "platform_name": "aiocqhttp",
        "group_id": group_id,
    }
    if use_existing_job:
        plugin.task_manager.add_job(
            "old",
            "remind",
            "0 8 * * *",
            kwargs={
                **context,
                "bot_name": "Giftia",
                "group_or_user_id": "g1",
                "user_id": "original-user",
                "user_name": "Original user",
            },
        )
    else:
        await plugin.db.upsert_kv_data("task_session:Giftia:g1", json.dumps(context))
    for time_expr in ["0 8 * * *", (datetime.now() + timedelta(days=1)).isoformat()]:
        response = await client.post(
            "/scheduled_tasks/create",
            json={
                "bot_name": "Giftia",
                "group_or_user_id": "g1",
                "time_expr": time_expr,
                "remind_message": "  Drink water  ",
            },
        )
        assert response.status_code == 200, response.text
        job = plugin.task_manager.scheduler.get_job(response.json()["data"]["task_id"])
        assert job.args == ("remind",)
        assert all(job.kwargs[key] == value for key, value in context.items())
        assert job.kwargs["remind_message"] == "Drink water"
        assert job.kwargs["user_id"] == ("dashboard" if group_id else "g1")
        assert job.kwargs["user_name"] == "WebUI"
        assert job.kwargs["nickname"] == "Giftia"
    response = await client.get(
        "/scheduled_tasks", params={"bot_name": "Giftia", "group_or_user_id": "g1"}
    )
    assert response.json()["data"]["total"] == 2 + int(use_existing_job)


@pytest.mark.asyncio
async def test_scheduled_creation_errors_do_not_create_jobs(task_runtime):
    plugin, client = task_runtime
    payload = {
        "bot_name": "Giftia",
        "group_or_user_id": "g1",
        "time_expr": "0 8 * * *",
        "remind_message": "Task",
    }
    response = await client.post("/scheduled_tasks/create", json=payload)
    assert response.status_code == 400
    assert "会话路由信息" in response.json()["message"]
    await plugin.db.upsert_kv_data(
        "task_session:Giftia:g1",
        json.dumps(
            {
                "unified_msg_origin": "adapter:GroupMessage:g1",
                "adapter_id": "adapter",
                "self_id": "bot-account",
                "platform_name": "aiocqhttp",
                "group_id": "g1",
            }
        ),
    )
    for change in [
        {"bot_name": ""},
        {"group_or_user_id": ""},
        {"remind_message": "  "},
        {"time_expr": ""},
        {"time_expr": "invalid"},
        {"time_expr": "2000-01-01 08:00:00"},
    ]:
        response = await client.post(
            "/scheduled_tasks/create", json={**payload, **change}
        )
        assert response.status_code == 400, response.text
    plugin.task_manager.registered_funcs = {}
    response = await client.post("/scheduled_tasks/create", json=payload)
    assert response.status_code == 400
    assert plugin.task_manager.scheduler.get_jobs() == []
