import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from core.database.data_cache import DataCache
from core.database.repositories.chat_history import ChatHistoryRepository
from core.utils.schemas import MessageData
from core.web.chat_history_api import ChatHistoryApi
from core.web.webui_manager import WebUIManager


class SingleMessageDeleteTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_history_repo_delete_by_db_id_success(self):
        """测试 ChatHistoryRepository.delete_message_by_db_id 成功删除单条记录并级联清理 forwarded_message"""

        class MockExecuteResult:
            def __init__(self, cursor):
                self.cursor = cursor

            async def __aenter__(self):
                return self.cursor

            async def __aexit__(self, *args):
                pass

            def __await__(self):
                async def _coro():
                    return self.cursor

                return _coro().__await__()

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone = AsyncMock(
            return_value={
                "id": 42,
                "bot_name": "Giftia",
                "group_or_user_id": "10001",
                "message_id": "msg_999",
                "content": "这是一条测试消息",
            }
        )
        mock_conn.execute = MagicMock(return_value=MockExecuteResult(mock_cursor))
        mock_conn.commit = AsyncMock()

        repo = ChatHistoryRepository(mock_conn)
        res = await repo.delete_message_by_db_id(42)

        self.assertIsNotNone(res)
        self.assertEqual(res["id"], 42)
        self.assertEqual(res["bot_name"], "Giftia")
        self.assertEqual(res["message_id"], "msg_999")
        mock_conn.commit.assert_called_once()

    async def test_chat_history_repo_delete_by_db_id_not_found(self):
        """测试 ChatHistoryRepository.delete_message_by_db_id 不存在时返回 None"""

        class MockExecuteResult:
            def __init__(self, cursor):
                self.cursor = cursor

            async def __aenter__(self):
                return self.cursor

            async def __aexit__(self, *args):
                pass

            def __await__(self):
                async def _coro():
                    return self.cursor

                return _coro().__await__()

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone = AsyncMock(return_value=None)
        mock_conn.execute = MagicMock(return_value=MockExecuteResult(mock_cursor))
        mock_conn.commit = AsyncMock()

        repo = ChatHistoryRepository(mock_conn)
        res = await repo.delete_message_by_db_id(9999)

        self.assertIsNone(res)
        mock_conn.commit.assert_not_called()

    async def test_data_cache_delete_by_db_id(self):
        """测试 DataCache.delete_message_by_db_id 同步从内存缓存中剔除目标消息"""
        mock_db = MagicMock()
        mock_db.delete_message_by_db_id = AsyncMock(
            return_value={
                "id": 100,
                "bot_name": "Giftia",
                "group_or_user_id": "group_123",
                "message_id": "msg_local_100",
            }
        )

        cache = DataCache.__new__(DataCache)
        cache.db = mock_db
        msg1 = MessageData(db_id=99, message_id="msg_99", content="msg 1")
        msg2 = MessageData(db_id=100, message_id="msg_local_100", content="msg 2")
        msg3 = MessageData(db_id=101, message_id="msg_101", content="msg 3")
        cache.recent_messages = {"Giftia:group_123": [msg1, msg2, msg3]}

        # 执行删除 id=100
        success = await cache.delete_message_by_db_id(100)
        self.assertTrue(success)
        remaining = cache.recent_messages["Giftia:group_123"]
        self.assertEqual(len(remaining), 2)
        self.assertNotIn(msg2, remaining)
        self.assertIn(msg1, remaining)
        self.assertIn(msg3, remaining)

    async def test_data_cache_delete_by_db_id_returns_false_when_not_found(self):
        """测试 DataCache.delete_message_by_db_id 在底层返回 None 时返回 False"""
        mock_db = MagicMock()
        mock_db.delete_message_by_db_id = AsyncMock(return_value=None)

        cache = DataCache.__new__(DataCache)
        cache.db = mock_db
        cache.recent_messages = {"Giftia:group_123": []}

        success = await cache.delete_message_by_db_id(99999)
        self.assertFalse(success)

    async def test_chat_history_api_delete_single_message_success(self):
        """测试 ChatHistoryApi.delete_single_message 接口成功删除"""
        api = ChatHistoryApi()
        mock_giftia = MagicMock()
        mock_giftia.data_cache.delete_message_by_db_id = AsyncMock(return_value=True)
        api.giftia = mock_giftia

        request = SimpleNamespace(json=AsyncMock(return_value={"id": 123}))
        with patch("core.web.chat_history_api.request", request):
            res = await api.delete_single_message()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(json.loads(res.body)["status"], "success")
        mock_giftia.data_cache.delete_message_by_db_id.assert_called_once_with(123)

    async def test_chat_history_api_delete_single_message_invalid_or_missing_id(self):
        """测试 ChatHistoryApi.delete_single_message 缺少或无效参数时返回错误响应"""
        api = ChatHistoryApi()
        api.giftia = MagicMock()
        for body, message in (
            ({}, "缺少 id 参数"),
            ({"id": "invalid_number"}, "无效的消息 id"),
        ):
            with self.subTest(body=body):
                request = SimpleNamespace(json=AsyncMock(return_value=body))
                with patch("core.web.chat_history_api.request", request):
                    res = await api.delete_single_message()
                self.assertEqual(res.status_code, 400)
                self.assertIn(message, json.loads(res.body)["message"])
        api.giftia.data_cache.delete_message_by_db_id.assert_not_called()

    async def test_chat_history_api_delete_single_message_not_found(self):
        """测试 ChatHistoryApi.delete_single_message 找不到记录时返回错误响应"""
        api = ChatHistoryApi()
        mock_giftia = MagicMock()
        mock_giftia.data_cache.delete_message_by_db_id = AsyncMock(return_value=False)
        api.giftia = mock_giftia

        request = SimpleNamespace(json=AsyncMock(return_value={"id": 99999}))
        with patch("core.web.chat_history_api.request", request):
            res = await api.delete_single_message()
        self.assertEqual(res.status_code, 400)
        self.assertIn("未找到该消息或已被删除", json.loads(res.body)["message"])
        mock_giftia.data_cache.delete_message_by_db_id.assert_awaited_once_with(99999)

    async def test_webui_manager_registers_delete_single_message_route(self):
        """测试 WebUIManager 成功注册 /astrbot_plugin_giftia/chat_history/message/delete 路由"""
        mock_plugin = MagicMock()
        mock_ctx = MagicMock()
        mock_plugin.context = mock_ctx

        manager = WebUIManager(mock_plugin)
        manager.register_routes()

        registered_routes = [
            call.kwargs.get("route")
            for call in mock_ctx.register_web_api.call_args_list
        ]
        self.assertIn(
            "/astrbot_plugin_giftia/chat_history/message/delete", registered_routes
        )

    async def test_chat_history_repo_delete_message_exact_success(self):
        """测试 ChatHistoryRepository.delete_message 按 message_id 精确删除成功并级联清理 forwarded_message"""

        class MockExecuteResult:
            def __init__(self, rowcount=1):
                self.rowcount = rowcount

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            def __await__(self):
                async def _coro():
                    return self

                return _coro().__await__()

        executed_sqls = []

        async def mock_execute(sql, params=None):
            executed_sqls.append((sql.strip(), params))
            return MockExecuteResult(rowcount=1)

        mock_conn = MagicMock()
        mock_conn.execute = MagicMock(side_effect=mock_execute)
        mock_conn.commit = AsyncMock()

        repo = ChatHistoryRepository(mock_conn)
        success = await repo.delete_message(
            bot_name="Giftia",
            group_or_user_id="10001",
            message_id="msg_real_123",
        )

        self.assertTrue(success)
        self.assertEqual(len(executed_sqls), 2)
        self.assertIn("DELETE FROM chat_history", executed_sqls[0][0])
        self.assertEqual(executed_sqls[0][1], ("msg_real_123", "10001", "Giftia"))
        self.assertIn("DELETE FROM forwarded_message", executed_sqls[1][0])
        self.assertEqual(executed_sqls[1][1], ("msg_real_123", "10001", "Giftia"))
        mock_conn.commit.assert_called_once()

    async def test_chat_history_repo_delete_message_not_found(self):
        """测试 ChatHistoryRepository.delete_message 未匹配到记录时返回 False 且不级联清理 forwarded_message"""

        class MockExecuteResult:
            def __init__(self, rowcount=0):
                self.rowcount = rowcount

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            def __await__(self):
                async def _coro():
                    return self

                return _coro().__await__()

        executed_sqls = []

        async def mock_execute(sql, params=None):
            executed_sqls.append((sql.strip(), params))
            return MockExecuteResult(rowcount=0)

        mock_conn = MagicMock()
        mock_conn.execute = MagicMock(side_effect=mock_execute)
        mock_conn.commit = AsyncMock()

        repo = ChatHistoryRepository(mock_conn)
        success = await repo.delete_message(
            bot_name="Giftia",
            group_or_user_id="10001",
            message_id="msg_non_existent",
        )

        self.assertFalse(success)
        self.assertEqual(len(executed_sqls), 1)
        self.assertIn("DELETE FROM chat_history", executed_sqls[0][0])
        mock_conn.commit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
