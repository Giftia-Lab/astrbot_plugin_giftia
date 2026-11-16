import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from core.conversation.chat_manager import ChatManager
from core.handlers.commands import CommandHandler
from core.utils.schemas import MessageData, Status

from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain, Reply


class MockFilter:
    def __init__(self, class_name: str, command_name: str = ""):
        self.__class__.__name__ = class_name
        self.command_name = command_name

    def get_complete_command_names(self):
        return [self.command_name]


class MockHandler:
    def __init__(self, handler_name: str, event_filters: list):
        self.handler_name = handler_name
        self.event_filters = event_filters


class CommandBlockingAndStatusTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plugin = MagicMock()
        self.plugin._terminated = False
        self.plugin.running_tasks = {}
        self.plugin.adapter_id_map = {"adapter_1": "Giftia"}
        self.plugin.bot_map = {"Giftia": {"nickname": "小吉", "name": "Giftia"}}
        self.plugin.active_reply_counters = {}
        self.plugin.replying_status = {}
        self.plugin.conf = {}
        self.plugin.parse_locks = {
            "Giftia:adapter_1:GroupMessage:10001": asyncio.Lock()
        }
        self.plugin.data_cache.add_message = AsyncMock()
        self.plugin.data_cache.get_bot_status = AsyncMock()
        self.plugin.data_cache.update_bot_custom_status = AsyncMock()
        self.plugin.data_cache.observe_group_metadata = AsyncMock()
        self.plugin.data_cache.initialize_user_profile = AsyncMock()
        self.plugin.data_cache.is_bot_muted.return_value = False
        self.plugin.db.get_kv_data = AsyncMock(return_value=None)
        self.plugin.db.upsert_kv_data = AsyncMock()
        self.plugin.message_parser.parse_user_message = AsyncMock(
            return_value=(
                MessageData(message_id="incoming", user_id="12345678"),
                [],
                [],
            )
        )
        self.plugin.message_parser.notice_parser.parse_notice = AsyncMock(
            return_value=None
        )
        self.plugin.message_parser.chain_to_result = AsyncMock(
            return_value=MagicMock(
                content="bot reply", media_id_list=[], forward_messages=[]
            )
        )

        self.plugin.passive_memory_enabled = False
        self.plugin.data_cache.get_stronghold = AsyncMock(return_value=None)

        self.chat_manager = ChatManager(self.plugin)
        self.chat_manager.decision_engine.check_whitelists = MagicMock(
            return_value=True
        )

    async def asyncTearDown(self):
        await asyncio.gather(
            *self.plugin.running_tasks.values(), return_exceptions=True
        )

    def _create_mock_event(
        self, activated_handlers: list | None = None, message_str: str = ""
    ):
        event = MagicMock()
        event._giftia_bypass_logging = False
        event._notice_result = None
        event.get_self_id = MagicMock(return_value="3970706156")
        event.get_sender_id = MagicMock(return_value="12345678")
        event.get_group_id = MagicMock(return_value="10001")
        event.get_platform_name = MagicMock(return_value="aiocqhttp")
        event.unified_msg_origin = "adapter_1:GroupMessage:10001"
        event.get_messages = MagicMock(return_value=[])
        event.get_message_str = MagicMock(return_value=message_str)
        event.platform_meta = MagicMock()
        event.platform_meta.id = "adapter_1"
        event.send = AsyncMock(side_effect=lambda msg: msg)
        event.message_obj = MagicMock()
        event.message_obj.raw_message = {"post_type": "message"}

        extras = {}
        if activated_handlers is not None:
            extras["activated_handlers"] = activated_handlers

        def get_extra(key, default=None):
            return extras.get(key, default)

        def set_extra(key, val):
            extras[key] = val

        event.get_extra = MagicMock(side_effect=get_extra)
        event.set_extra = MagicMock(side_effect=set_extra)
        return event

    async def test_normal_command_blocked_when_switch_enabled(self):
        """当 block_command_messages 为 True 时，普通指令及其响应不入库"""
        self.plugin.block_command_messages = True

        cmd_handler = MockHandler("help_cmd", [MockFilter("CommandFilter", "help")])
        event = self._create_mock_event(
            activated_handlers=[cmd_handler, MockHandler("on_message", [])],
            message_str="/help",
        )

        await self.chat_manager.handle_message(event)

        # 检查 bypass 标记是否设置
        self.assertTrue(getattr(event, "_giftia_bypass_logging", False))

        # 检查用户指令是否未被解析入库
        self.plugin.message_parser.parse_user_message.assert_not_called()
        self.plugin.data_cache.add_message.assert_not_called()

        # 测试 Bot 响应通过 event.send 时的表现
        await event.send(MagicMock())
        # data_cache.add_message 仍不应被调用（被 intercepted_send 拦截屏蔽）
        self.plugin.data_cache.add_message.assert_not_called()

    async def test_normal_command_allowed_when_switch_disabled(self):
        """当 block_command_messages 为 False 时，普通指令及其响应正常入库"""
        self.plugin.block_command_messages = False

        cmd_handler = MockHandler("help_cmd", [MockFilter("CommandFilter", "help")])
        event = self._create_mock_event(
            activated_handlers=[cmd_handler, MockHandler("on_message", [])],
            message_str="/help",
        )

        await self.chat_manager.handle_message(event)

        # 检查 bypass 标记未设置
        self.assertFalse(getattr(event, "_giftia_bypass_logging", False))

        # Command parsing now finishes in the tracked background task.
        await asyncio.gather(*self.plugin.running_tasks.values())
        self.plugin.message_parser.parse_user_message.assert_called_once()

        # 测试 Bot 响应通过 event.send
        msg = MessageChain([Plain("bot reply")])
        await event.send(msg)
        self.plugin.data_cache.add_message.assert_called_once()

    async def test_delete_message_command_always_blocked_even_when_switch_disabled(
        self,
    ):
        """无论 block_command_messages 是否开启，/删除消息 指令及其响应均强制屏蔽入库"""
        self.plugin.block_command_messages = False

        del_handler = MockHandler(
            "delete_message", [MockFilter("CommandFilter", "删除消息")]
        )
        event = self._create_mock_event(
            activated_handlers=[del_handler, MockHandler("on_message", [])],
            message_str="/删除消息",
        )

        await self.chat_manager.handle_message(event)

        # 检查 bypass 标记为 True
        self.assertTrue(getattr(event, "_giftia_bypass_logging", False))

        # 用户指令未入库
        self.plugin.message_parser.parse_user_message.assert_not_called()

        # Bot 响应不入库
        await event.send(MagicMock())
        self.plugin.data_cache.add_message.assert_not_called()

    async def test_pseudo_command_is_treated_as_normal_message(self):
        """未注册的伪指令（如 /起飞）不命中 CommandFilter，应正常存入数据库"""
        self.plugin.block_command_messages = True

        # 伪指令没有激活任何 CommandFilter，仅激活 on_message
        event = self._create_mock_event(
            activated_handlers=[MockHandler("on_message", [])],
            message_str="/起飞",
        )

        await self.chat_manager.handle_message(event)

        # bypass 标记应为 False
        self.assertFalse(getattr(event, "_giftia_bypass_logging", False))

        # Message parsing now finishes in the tracked background task.
        await asyncio.gather(*self.plugin.running_tasks.values())
        self.plugin.message_parser.parse_user_message.assert_called_once()

    async def test_set_persistent_status_command(self):
        """测试 /设置常驻状态 指令添加、修改与删除"""
        cmd_handler = CommandHandler(self.plugin)
        event = self._create_mock_event()

        # 1. 正常设置状态
        chunks = [
            c async for c in cmd_handler.set_persistent_status(event, "服装", "女仆装")
        ]
        self.plugin.data_cache.update_bot_custom_status.assert_called_with(
            bot_name="Giftia",
            group_id="10001",
            custom_status_updates={"服装": "女仆装"},
        )
        self.assertEqual(len(chunks), 1)

        # 2. 状态值为空（删除）
        chunks = [c async for c in cmd_handler.set_persistent_status(event, "服装", "")]
        self.plugin.data_cache.update_bot_custom_status.assert_called_with(
            bot_name="Giftia", group_id="10001", custom_status_updates={"服装": ""}
        )

        # 3. 状态值为 "删除" 关键字
        chunks = [
            c async for c in cmd_handler.set_persistent_status(event, "服装", "删除")
        ]
        self.plugin.data_cache.update_bot_custom_status.assert_called_with(
            bot_name="Giftia", group_id="10001", custom_status_updates={"服装": ""}
        )

        # 4. 状态名为空
        chunks = [
            c async for c in cmd_handler.set_persistent_status(event, "", "女仆装")
        ]
        self.assertIn("状态名不能为空", chunks[0].chain[0].text)

    async def test_get_bot_status_command(self):
        """测试 /查看状态 指令返回结构化看板"""
        cmd_handler = CommandHandler(self.plugin)
        event = self._create_mock_event()

        mock_status = Status(
            mood="开心",
            state="空闲",
            action="品茶",
            memory="想和大家聊聊今天的趣事",
            energy="95.5",
            custom_status={"服装": "水手服", "场景": "教室"},
        )
        self.plugin.data_cache.get_bot_status.return_value = mock_status

        chunks = [c async for c in cmd_handler.get_bot_status(event)]
        self.assertEqual(len(chunks), 1)
        output_text = chunks[0].chain[0].text

        self.assertIn("临时状态：", output_text)
        self.assertIn("常驻状态：", output_text)
        self.assertIn("心情：开心", output_text)
        self.assertIn("状态：空闲", output_text)
        self.assertIn("动作：品茶", output_text)
        self.assertIn("思考：想和大家聊聊今天的趣事", output_text)
        self.assertIn("能量：96%", output_text)
        self.assertIn("服装：水手服", output_text)
        self.assertIn("场景：教室", output_text)

    def test_delete_table_removed(self):
        """验证 delete_table 指令已从 CommandHandler 彻底移除"""
        cmd_handler = CommandHandler(self.plugin)
        self.assertFalse(hasattr(cmd_handler, "delete_table"))

    async def test_status_image_response_uses_current_private_session(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        self.plugin.conf = {"report_config": {"render_mode": "图片响应"}}
        self.plugin.data_cache.get_bot_status.return_value = Status(
            mood="开心", energy="0", memory="稍作休息再继续聊天"
        )
        event = self._create_mock_event()
        event.get_group_id.return_value = ""
        with TemporaryDirectory() as folder:
            image_path = Path(folder) / "status.jpg"
            image_path.write_bytes(b"rendered")
            self.plugin.reports.render_image = AsyncMock(return_value=str(image_path))
            component = object()
            with patch(
                "core.handlers.commands.Image.fromFileSystem",
                return_value=component,
                create=True,
            ):
                chunks = [
                    chunk
                    async for chunk in CommandHandler(self.plugin).get_bot_status(event)
                ]
            self.assertIs(chunks[0].chain[0], component)
            self.assertFalse(image_path.exists())
        self.plugin.data_cache.get_bot_status.assert_awaited_once_with(
            "Giftia", "12345678"
        )
        data = self.plugin.reports.render_image.call_args.args[1]
        self.assertEqual(data["session_id"], "12345678")
        self.assertEqual(data["energy"], "0%")
        self.assertEqual(data["memory"], "稍作休息再继续聊天")

    async def test_status_render_error_falls_back_to_text(self):
        self.plugin.conf = {"report_config": {"render_mode": "图片响应"}}
        self.plugin.reports.render_image = AsyncMock(
            side_effect=RuntimeError("t2i unavailable")
        )
        self.plugin.data_cache.get_bot_status.return_value = Status(
            mood="开心", memory="想听听大家的近况", custom_status={"服装": "水手服"}
        )
        chunks = [
            chunk
            async for chunk in CommandHandler(self.plugin).get_bot_status(
                self._create_mock_event()
            )
        ]
        self.assertEqual(len(chunks), 1)
        self.assertIn("心情：开心", chunks[0].chain[0].text)
        self.assertIn("思考：想听听大家的近况", chunks[0].chain[0].text)
        self.assertIn("服装：水手服", chunks[0].chain[0].text)

    async def test_status_default_text_does_not_call_renderer(self):
        self.plugin.conf = {}
        self.plugin.reports.render_image = AsyncMock()
        self.plugin.data_cache.get_bot_status.return_value = Status()
        chunks = [
            chunk
            async for chunk in CommandHandler(self.plugin).get_bot_status(
                self._create_mock_event()
            )
        ]
        self.assertIn("暂无常驻状态", chunks[0].chain[0].text)
        self.assertIn("思考：暂无思考", chunks[0].chain[0].text)
        self.plugin.reports.render_image.assert_not_called()

    async def test_delete_message_command_and_logging(self):
        """测试 /删除消息 指令的执行过程与各层日志记录"""
        cmd_handler = CommandHandler(self.plugin)

        # 1. 无 Reply 组件，提示未找到引用消息
        event_no_reply = self._create_mock_event()
        chunks = [c async for c in cmd_handler.delete_message(event_no_reply)]
        self.assertIn("未找到引用消息的消息ID", chunks[0].chain[0].text)

        # 2. 有 Reply 组件，删除成功返回成功提示
        self.plugin.data_cache.delete_message = AsyncMock(return_value=True)
        event_with_reply = self._create_mock_event()
        reply = Reply(
            id="msg_88888", sender_id="user_123", message_str="这是一条指令回复"
        )
        event_with_reply.get_messages.return_value = [reply]

        chunks2 = [c async for c in cmd_handler.delete_message(event_with_reply)]
        self.assertIn("删除消息成功", chunks2[0].chain[0].text)
        self.plugin.data_cache.delete_message.assert_called_once_with(
            bot_name="Giftia",
            group_or_user_id="10001",
            message_id="msg_88888",
        )

        # 3. 有 Reply 组件，但库中未匹配到（返回 False），提示前往 WebUI 删除
        self.plugin.data_cache.delete_message = AsyncMock(return_value=False)
        chunks3 = [c async for c in cmd_handler.delete_message(event_with_reply)]
        self.assertEqual(
            chunks3[0].chain[0].text,
            "删除消息失败：指令响应消息请前往 WebUI 决策审计页面手动删除。",
        )

    async def test_intercepted_send_captures_platform_message_id(self):
        """测试 intercepted_send 通过包装 bot 方法成功捕获平台返回的真实 message_id"""
        self.plugin.block_command_messages = False

        event = self._create_mock_event(
            activated_handlers=[
                MockHandler("help_cmd", [MockFilter("CommandFilter", "help")])
            ],
            message_str="/help",
        )
        mock_bot = MagicMock()
        mock_bot.send_group_msg = AsyncMock(return_value={"message_id": 1710535200})
        mock_bot.call_action = AsyncMock(return_value={"message_id": 1710535200})
        mock_bot.send_private_msg = AsyncMock(return_value={"message_id": 1710535200})
        event.bot = mock_bot

        # 模拟 AstrBot 的底层实现：适配器的 event.send 调用协议端 bot.send_group_msg
        async def adapter_send(chain):
            return await event.bot.send_group_msg(group_id=10001, message=[])

        event.send = adapter_send

        # handle_message 执行，用 intercepted_send 替换 event.send
        await self.chat_manager.handle_message(event)

        # 触发拦截发送
        await event.send(MessageChain([Plain("bot reply")]))

        # 验证存入数据库的 MessageData 带有真实的 message_id
        self.plugin.data_cache.add_message.assert_called_once()
        saved_msg_data = self.plugin.data_cache.add_message.call_args[0][2]
        self.assertEqual(saved_msg_data.message_id, "1710535200")
        self.assertTrue(
            self.plugin.data_cache.add_message.call_args.kwargs["observed_by_bot"]
        )

    async def test_intercepted_send_concurrency_isolation(self):
        """测试同一个 bot 实例在多个并发事件中发送时，通过 ContextVar 隔离捕获正确的 message_id"""
        self.plugin.block_command_messages = False
        shared_bot = MagicMock()

        async def mock_call_action(action, **params):
            if params.get("msg") == "msg1":
                await asyncio.sleep(0.02)
                return {"message_id": 1001}
            elif params.get("msg") == "msg2":
                await asyncio.sleep(0.01)
                return {"message_id": 1002}
            return {"message_id": 9999}

        shared_bot.call_action = mock_call_action
        shared_bot.send_group_msg = AsyncMock()
        shared_bot.send_private_msg = AsyncMock()

        event1 = self._create_mock_event(
            activated_handlers=[
                MockHandler("cmd1", [MockFilter("CommandFilter", "cmd1")])
            ],
            message_str="/cmd1",
        )
        event1.bot = shared_bot

        async def send1(chain):
            return await shared_bot.call_action(
                "send_group_msg", group_id=10001, msg="msg1"
            )

        event1.send = send1

        event2 = self._create_mock_event(
            activated_handlers=[
                MockHandler("cmd2", [MockFilter("CommandFilter", "cmd2")])
            ],
            message_str="/cmd2",
        )
        event2.bot = shared_bot

        async def send2(chain):
            return await shared_bot.call_action(
                "send_group_msg", group_id=10001, msg="msg2"
            )

        event2.send = send2

        await self.chat_manager.handle_message(event1)
        await self.chat_manager.handle_message(event2)

        self.plugin.message_parser.chain_to_result = AsyncMock(
            side_effect=lambda *args, **kwargs: MagicMock(
                content=args[0][0].text
                if args and args[0] and hasattr(args[0][0], "text")
                else "bot reply",
                media_id_list=[],
                forward_messages=[],
            )
        )

        saved_msgs = []
        self.plugin.data_cache.add_message = AsyncMock(
            side_effect=lambda b, g, msg, **kwargs: saved_msgs.append(msg)
        )

        # 并发执行两个 event.send
        await asyncio.gather(
            event1.send(MessageChain([Plain("reply 1")])),
            event2.send(MessageChain([Plain("reply 2")])),
        )

        msg1_record = next((m for m in saved_msgs if m.content == "reply 1"), None)
        msg2_record = next((m for m in saved_msgs if m.content == "reply 2"), None)
        self.assertIsNotNone(msg1_record)
        self.assertIsNotNone(msg2_record)
        self.assertEqual(msg1_record.message_id, "1001")
        self.assertEqual(msg2_record.message_id, "1002")

    async def test_data_cache_delete_message_exact_match(self):
        """测试 DataCache.delete_message 依据 message_id 精确删除并返回结果"""
        from core.database.data_cache import DataCache
        from core.utils.schemas import MessageData

        db_mock = MagicMock()
        db_mock.delete_message = AsyncMock(return_value=True)

        cache = DataCache.__new__(DataCache)
        cache.recent_messages = {"Giftia:10001": []}
        cache.db = db_mock

        target_msg = MessageData(
            nickname="小吉",
            user_id="3970706156",
            group_or_user_id="10001",
            time="2026-09-05T18:18:07",
            message_id="1710535200",
            content="这是一条正常回复",
        )
        cache.recent_messages["Giftia:10001"].append(target_msg)

        # 匹配成功
        success = await cache.delete_message(
            bot_name="Giftia",
            group_or_user_id="10001",
            message_id="1710535200",
        )
        self.assertTrue(success)
        self.assertEqual(len(cache.recent_messages["Giftia:10001"]), 0)
        db_mock.delete_message.assert_called_once_with(
            bot_name="Giftia",
            group_or_user_id="10001",
            message_id="1710535200",
        )

        # 匹配失败
        db_mock.delete_message.return_value = False
        fail_res = await cache.delete_message(
            bot_name="Giftia",
            group_or_user_id="10001",
            message_id="non_existent_id",
        )
        self.assertFalse(fail_res)

    async def test_get_bot_status_with_zero_energy(self):
        """测试 get_bot_status 在能量为 0 时能正确展示 0% 而不会被误 fallback 为 100.0%"""
        from core.handlers.commands import CommandHandler
        from core.utils.schemas import Status

        cmd_handler = CommandHandler(self.plugin)
        mock_status = Status(
            mood="平稳",
            state="空闲",
            action="待机",
            energy=0,
            custom_status={},
        )
        self.plugin.data_cache.get_bot_status = AsyncMock(return_value=mock_status)

        event = self._create_mock_event()
        chunks = [c async for c in cmd_handler.get_bot_status(event)]
        reply_text = chunks[0].chain[0].text
        self.assertIn("• 能量：0%", reply_text)
        self.assertNotIn("• 能量：100.0%", reply_text)

    async def test_silence_session_command(self):
        """Verify the silence command resets only the current reply window."""
        from core.handlers.commands import CommandHandler

        cmd_handler = CommandHandler(self.plugin)
        self.plugin.active_reply_counters = {"Giftia:10001": 10, "Giftia:other": 6}
        self.plugin.data_cache.set_bot_status = AsyncMock()

        event = self._create_mock_event()
        chunks = [c async for c in cmd_handler.silence_session(event)]

        self.assertEqual(
            self.plugin.active_reply_counters, {"Giftia:10001": 0, "Giftia:other": 6}
        )
        self.plugin.data_cache.set_bot_status.assert_not_awaited()

        reply_text = chunks[0].chain[0].text
        self.assertIn("已重置【小吉】在当前会话的接话分析窗口", reply_text)


if __name__ == "__main__":
    unittest.main()
