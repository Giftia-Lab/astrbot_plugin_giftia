from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest
import pytest_asyncio
from core.database.repositories.token_usage import TokenUsageRepository
from core.database.schema import initialize_database


@pytest_asyncio.fixture
async def repository():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await initialize_database(conn)
        now = datetime.now().astimezone()
        await conn.executemany(
            "INSERT INTO token_usage "
            "(bot_name, group_or_user_id, type, provider_id, model_name, "
            "total_tokens, call_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (*row, now.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
                for row in [
                    (
                        "alpha",
                        "group-a",
                        "reply",
                        "provider-a/shared",
                        "shared",
                        12,
                        None,
                    ),
                    ("alpha", "group-a", "reply", "provider-a/shared", "shared", 23, 2),
                    ("alpha", "group-a", "decision", "zero-provider", "decision", 0, 3),
                    ("alpha", "group-a", "tts", "provider-a/shared", "shared", 400, 9),
                    ("alpha", "group-b", "reply", "provider-b/shared", "shared", 44, 4),
                    (
                        "beta",
                        "group-a",
                        "reply",
                        "provider-a/shared",
                        "shared",
                        800,
                        100,
                    ),
                ]
            ],
        )
        await conn.executemany(
            "INSERT INTO token_daily_stats "
            "(bot_name, group_or_user_id, type, provider_id, model_name, "
            "total_tokens, call_count, date, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'success')",
            [
                (*row, (now - timedelta(days=days)).strftime("%Y-%m-%d"))
                for days, row in [
                    (
                        2,
                        (
                            "alpha",
                            "group-a",
                            "reply",
                            "provider-a/shared",
                            "shared",
                            120,
                            7,
                        ),
                    ),
                    (
                        2,
                        (
                            "alpha",
                            "group-b",
                            "reply",
                            "provider-b/shared",
                            "shared",
                            220,
                            11,
                        ),
                    ),
                    (
                        60,
                        (
                            "alpha",
                            "group-a",
                            "reply",
                            "provider-a/shared",
                            "shared",
                            320,
                            13,
                        ),
                    ),
                    (
                        2,
                        (
                            "alpha",
                            "group-a",
                            "image_caption",
                            "legacy",
                            "legacy",
                            50,
                            0,
                        ),
                    ),
                    (
                        2,
                        (
                            "alpha",
                            "group-a",
                            "tts",
                            "provider-a/shared",
                            "shared",
                            500,
                            10,
                        ),
                    ),
                    (
                        2,
                        (
                            "beta",
                            "group-a",
                            "reply",
                            "provider-a/shared",
                            "shared",
                            900,
                            200,
                        ),
                    ),
                ]
            ],
        )
        await conn.commit()
        yield TokenUsageRepository(conn)


@pytest.mark.asyncio
async def test_breakdown_counts_merge_live_and_archived_calls(repository):
    stats = await repository.get_token_stats(bot_name="alpha")
    assert stats["summary"]["total_calls"] == 41
    assert stats["summary"]["reply_calls"] == 38
    assert stats["summary"]["decision_calls"] == 3
    assert stats["summary"]["image_caption_calls"] == 0
    assert stats["summary"]["tts_calls"] == 19
    assert stats["summary"]["total_chars_tts"] == 900
    assert {
        row["group_or_user_id"]: row["call_count"] for row in stats["by_group"]
    } == {"group-a": 26, "group-b": 15}
    models = {row["provider_id"]: row for row in stats["by_model"]}
    assert models["provider-a"]["call_count"] == 23
    assert models["provider-b"]["call_count"] == 15
    assert (
        models["provider-a"]["model_name"]
        == models["provider-b"]["model_name"]
        == "shared"
    )
    assert models["legacy"]["call_count"] == 0
    assert models["zero-provider"]["call_count"] == 3
    assert models["zero-provider"]["total_tokens"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bot,group,time_range,calls,tokens",
    [
        ("alpha", None, None, 41, 789),
        ("alpha", "group-a", None, 26, 525),
        ("alpha", None, "today", 10, 79),
        ("alpha", None, "week", 28, 469),
        ("alpha", "group-a", "week", 13, 205),
        ("beta", "group-a", None, 300, 1700),
        ("missing", None, None, 0, 0),
    ],
)
async def test_call_counts_follow_token_filters(
    repository, bot, group, time_range, calls, tokens
):
    stats = await repository.get_token_stats(bot, group, time_range)
    assert stats["summary"]["total_calls"] == calls
    assert stats["summary"]["total_tokens"] == tokens
    assert sum(row["call_count"] for row in stats["by_group"]) == calls
    assert sum(row["call_count"] for row in stats["by_model"]) == calls


@pytest.mark.asyncio
async def test_archiving_keeps_breakdown_counts(repository):
    old_time = (datetime.now(timezone.utc) - timedelta(days=2)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    await repository.conn.execute(
        "UPDATE token_usage SET created_at=? "
        "WHERE bot_name='alpha' AND group_or_user_id='group-a' AND type='reply'",
        (old_time,),
    )
    await repository.conn.commit()
    before = await repository.get_token_stats(bot_name="alpha")
    assert await repository.squash_token_logs() == 2
    after = await repository.get_token_stats(bot_name="alpha")
    for key in ("summary", "by_group", "by_model"):
        assert after[key] == before[key]
