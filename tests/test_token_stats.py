from array import array
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiosqlite
import pytest
import pytest_asyncio
from core.database.repositories.token_usage import TokenUsageRepository
from core.database.schema import initialize_database
from core.memory.memory import LTM


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


@pytest.fixture(params=[False, True], ids=["local", "remote"])
def memory_models(request):
    """Provide model stand-ins that exercise real LTM invocation paths.

    Args:
        request: Pytest fixture parameters.

    Returns:
        An LTM instance without model downloads or persistent vector storage.
    """
    ltm = LTM.__new__(LTM)
    ltm._lazy_init = Mock()
    ltm.usage_logger = AsyncMock()
    ltm.embedding_conf = {"use_external_provider": request.param}
    ltm.rerank_conf = {"use_external_provider": request.param}
    ltm.embed_model = SimpleNamespace(
        model_name="BAAI/bge-m3", embed=Mock(return_value=[array("f", [0.0, 0.0])])
    )
    ltm.embed_provider = SimpleNamespace(
        model="BAAI/bge-m3",
        provider_config={"id": "vendor/embedding"},
        get_embedding=AsyncMock(return_value=[0.0, 0.0]),
    )
    ltm.reranker = SimpleNamespace(
        model_name="BAAI/bge-reranker-v2-m3", rerank=Mock(return_value=iter([0.9, 0.8]))
    )
    ltm.rerank_provider = SimpleNamespace(
        model="BAAI/bge-reranker-v2-m3",
        provider_config={"id": "vendor/rerank"},
        rerank=AsyncMock(return_value=[SimpleNamespace(index=0, relevance_score=0.9)]),
    )
    ltm.table = Mock()
    ltm.table.search.return_value.where.return_value.limit.return_value.to_list.return_value = [
        {"id": "saved", "text": "fact", "_distance": 0.1}
    ]
    return ltm


@pytest.mark.asyncio
async def test_memory_model_calls_record_models_and_initiating_session(memory_models):
    ltm = memory_models
    assert await ltm.add_memory(
        "bot", "source", "fact", "2026-11-16", memory_id="saved"
    )
    assert await ltm.search_memory(
        "bot", None, "query", usage_group_or_user_id="current", include_ids=["saved"]
    )
    assert await ltm.rerank_memories(
        "query",
        [{"text": "fact"}, {"text": "other"}],
        bot_name="bot",
        group_or_user_id="current",
    )
    calls = [call.kwargs for call in ltm.usage_logger.await_args_list]
    assert [(call["type"], call["group_or_user_id"]) for call in calls] == [
        ("embedding", "source"),
        ("embedding", "current"),
        ("rerank", "current"),
    ]
    remote = ltm.embedding_conf["use_external_provider"]
    assert [call["provider_id"] for call in calls] == (
        ["vendor/embedding", "vendor/embedding", "vendor/rerank"]
        if remote
        else ["", "", ""]
    )
    assert [call["model_name"] for call in calls] == [
        "BAAI/bge-m3",
        "BAAI/bge-m3",
        "BAAI/bge-reranker-v2-m3",
    ]
    assert all(call["bot_name"] == "bot" for call in calls)
    assert all(call["extra_info"]["usage_missing"] for call in calls)
    assert all(
        call["extra_info"]["execution_mode"] == ("remote" if remote else "local")
        for call in calls
    )


@pytest.mark.asyncio
async def test_skipped_models_are_not_counted_and_failed_calls_are_counted(
    memory_models,
):
    ltm = memory_models
    assert await ltm.search_memory("bot", "session", "query", include_ids=[]) == []
    assert await ltm.rerank_memories("query", []) == []
    ltm.usage_logger.assert_not_awaited()
    remote = ltm.embedding_conf["use_external_provider"]
    model_call = ltm.embed_provider.get_embedding if remote else ltm.embed_model.embed
    model_call.side_effect = RuntimeError("embedding failed")
    with pytest.raises(RuntimeError, match="embedding failed"):
        await ltm.search_memory("bot", "session", "query", raise_errors=True)
    ltm.usage_logger.assert_awaited_once()
    assert ltm.usage_logger.await_args.kwargs["extra_info"]["status"] == "api_failed"
    ltm.usage_logger.reset_mock()
    rerank_call = ltm.rerank_provider.rerank if remote else ltm.reranker.rerank
    rerank_call.side_effect = RuntimeError("rerank failed")
    candidates = [{"text": "fact"}]
    assert await ltm.rerank_memories("query", candidates) == candidates
    ltm.usage_logger.assert_awaited_once()
    assert ltm.usage_logger.await_args.kwargs["extra_info"]["status"] == "api_failed"


@pytest.mark.asyncio
async def test_statistics_failure_does_not_break_model_results(memory_models):
    memory_models.usage_logger.side_effect = RuntimeError("statistics unavailable")
    assert await memory_models.search_memory("bot", "session", "query")


@pytest.mark.asyncio
async def test_memory_usage_survives_filters_and_archival(repository, memory_models):
    ltm = memory_models
    ltm.usage_logger = repository.log_token_usage
    await ltm.search_memory(
        "model-bot", None, "query", usage_group_or_user_id="session"
    )
    await ltm.rerank_memories(
        "query", [{"text": "fact"}], bot_name="model-bot", group_or_user_id="session"
    )
    before = await repository.get_token_stats("model-bot", "session", "today")
    assert (
        before["summary"]["total_calls"]
        == before["summary"]["usage_missing_calls"]
        == 2
    )
    assert before["summary"]["total_tokens"] == 0
    assert (
        before["summary"]["embedding_calls"] == before["summary"]["rerank_calls"] == 1
    )
    mode = "remote" if ltm.embedding_conf["use_external_provider"] else "local"
    assert before["summary"][f"embedding_{mode}_calls"] == 1
    assert before["summary"][f"rerank_{mode}_calls"] == 1
    assert before["by_group"][0]["usage_missing_calls"] == 2
    assert {model["model_type"] for model in before["by_model"]} == {
        "embedding",
        "rerank",
    }
    assert all(model["execution_mode"] == mode for model in before["by_model"])
    assert all(model["usage_missing_calls"] == 1 for model in before["by_model"])
    assert {model["model_name"] for model in before["by_model"]} == {
        "BAAI/bge-m3",
        "BAAI/bge-reranker-v2-m3",
    }
    assert all(
        not point["type"] and not point["model"] and not point["group"]
        for point in before["time_series"]["timeline"]
    )
    logs, count = await repository.get_token_logs(bot_name="model-bot")
    assert count == 2
    assert all(
        log["usage_missing_calls"] == 1 and log["execution_mode"] == mode
        for log in logs
    )
    assert (await repository.get_token_stats("model-bot", "other"))["summary"][
        "total_calls"
    ] == 0

    old_time = (datetime.now(timezone.utc) - timedelta(days=2)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    await repository.conn.execute(
        "UPDATE token_usage SET created_at=? WHERE bot_name='model-bot'", (old_time,)
    )
    await repository.conn.commit()
    assert await repository.squash_token_logs() == 2
    after = await repository.get_token_stats("model-bot", "session", "week")
    for key in ("summary", "by_group", "by_model"):
        assert after[key] == before[key]
    assert (await repository.get_token_stats("model-bot", "session", "today"))[
        "summary"
    ]["total_calls"] == 0
    assert await repository.squash_token_logs() == 0
    await ltm.search_memory("model-bot", "session", "another query")
    await repository.conn.execute(
        "UPDATE token_usage SET created_at=? WHERE bot_name='model-bot'", (old_time,)
    )
    await repository.conn.commit()
    assert await repository.squash_token_logs() == 1
    merged = await repository.get_token_stats("model-bot", "session", "week")
    assert (
        merged["summary"]["total_calls"]
        == merged["summary"]["usage_missing_calls"]
        == 3
    )
    assert merged["summary"]["embedding_calls"] == 2


@pytest.mark.asyncio
async def test_local_remote_and_operation_types_do_not_merge(repository):
    for type_name in ("embedding", "rerank"):
        for provider in ("", "vendor/endpoint"):
            await repository.log_token_usage(
                "models",
                "session",
                type_name,
                provider,
                "shared/model",
                0,
                0,
                0,
                {"usage_missing": True},
            )
    stats = await repository.get_token_stats("models")
    assert (
        stats["summary"]["total_calls"] == stats["summary"]["usage_missing_calls"] == 4
    )
    assert len(stats["by_model"]) == 4
    assert {model["model_name"] for model in stats["by_model"]} == {"shared/model"}
    assert {model["execution_mode"] for model in stats["by_model"]} == {
        "local",
        "remote",
    }


@pytest.mark.asyncio
async def test_reported_total_remains_visible_when_input_output_breakdown_is_missing(
    repository,
):
    await repository.log_token_usage(
        "reported",
        "session",
        "reply",
        "provider",
        "model",
        0,
        0,
        42,
        {"usage_missing": True},
    )
    stats = await repository.get_token_stats("reported")
    assert stats["summary"]["total_tokens"] == 42
    assert stats["summary"]["usage_missing_calls"] == 0
    assert stats["by_model"][0]["usage_missing_calls"] == 0
