import asyncio
import json
import uuid
from datetime import datetime
from xml.sax.saxutils import escape, quoteattr

import mcp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Image
from astrbot.core.astr_agent_context import AgentContextWrapper, AstrAgentContext

try:
    from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
except ModuleNotFoundError as exc:
    if exc.name != "astrbot.core.astr_agent_tool_exec":
        raise
    from astrbot.core.astr_agent_runtime.tool_executor import FunctionToolExecutor

from ..llm.native_tools import NATIVE_CONTEXT_KEY
from ..utils.schemas import MessageData


class ToolExecutor:
    def __init__(self, plugin):
        self.plugin = plugin

    async def _record_operation_log(
        self,
        event: AstrMessageEvent,
        bot_name: str,
        nickname: str,
        group_or_user_id: str,
        message_id: str,
        content: str,
    ) -> None:
        """Persist one call-only tool or subagent receipt into conversation history."""
        await self.plugin.data_cache.add_message(
            bot_name,
            group_or_user_id,
            MessageData(
                nickname=nickname,
                user_id=event.get_self_id(),
                group_or_user_id=group_or_user_id,
                time=datetime.now().isoformat(),
                message_id=message_id,
                content=content,
                is_recalled=False,
                media_id_list=[],
                role="operation_log",
            ),
        )

    async def execute_delegation(
        self,
        event: AstrMessageEvent,
        bot_name: str,
        nickname: str,
        group_or_user_id: str,
        task_args: dict,
        tool_context: dict | None = None,
    ) -> dict:
        """Submit or extend a background subagent task from a <sub_agent> tag."""
        result = []
        delegated = None
        waiting = False
        failed = False
        interrupted = None
        instruction = str((task_args or {}).get("instruction") or "").strip()
        try:
            if not (tool_context or {}).get("delegation_providers") or tool_context.get(
                "delegation_delivery"
            ):
                raise ValueError("当前回复不接受新的后台委派")
            delegated, waiting = await self.plugin.delegations.submit(
                event, bot_name, task_args or {}, tool_context
            )
            result.append(
                f"已受理子任务 {delegated}" if waiting else f"子任务 {delegated} 已结束"
            )
        except asyncio.CancelledError as exc:
            interrupted = exc
        except Exception as e:
            failed = True
            logger.error(f"Error executing subagent delegation: {e}", exc_info=True)
            result.append(f"子任务委派失败: {e}")

        tool_output = "\n".join(result)
        status = (
            "interrupted"
            if interrupted is not None
            else "failed"
            if failed
            else "finished"
        )
        if delegated is not None:
            message_id = f"delegate_{delegated}_{uuid.uuid4().hex}"
            content = (
                f"<sub_agent task_id={quoteattr(str(delegated))} "
                f"status={quoteattr(status)}>{escape(instruction)}</sub_agent>"
            )
        else:
            message_id = f"subagent_{uuid.uuid4().hex}_{status}"
            content = f"<sub_agent status={quoteattr(status)}/>"
        await self._record_operation_log(
            event, bot_name, nickname, group_or_user_id, message_id, content
        )
        if interrupted is not None:
            raise interrupted
        receipt = {"name": "sub_agent", "results": tool_output}
        if delegated is not None:
            receipt["delegated"] = delegated
            receipt["waiting"] = waiting
        return receipt

    async def execute_tool(
        self,
        event: AstrMessageEvent,
        bot_name: str,
        nickname: str,
        group_or_user_id: str,
        tool_name: str,
        tool_args: dict,
        tool_context: dict | None = None,
    ) -> dict:
        """执行单个 XML 工具，并在派发下一个输出前直接发送工具生成的图片。

        Args:
            event: 传递给工具并用于发送图片的事件对象。
            bot_name: 机器人配置名称。
            nickname: 写入历史记录的机器人显示名称。
            group_or_user_id: 当前会话 ID。
            tool_name: 请求调用的工具名称（可带命名空间）。
            tool_args: 解析后的工具调用参数。
            tool_context: Trusted run scope shared across tool calls and continuations.

        Returns:
            包含执行状态和文本输出的工具执行结果字典，供下一轮 LLM 循环消费。
        """
        result = []
        failed = False
        interrupted = None
        clean_tool_name = tool_name.rsplit(":", 1)[-1]
        log_name = tool_name
        try:
            if getattr(self.plugin, "delegations", None) is not None:
                tools, _ = self.plugin.delegations.tool_sets(bot_name)
                name = tool_name if tools.get_tool(tool_name) else clean_tool_name
                log_name = name
                allowed = (tool_context or {}).get("xml_tools")
                if allowed is not None and name not in allowed:
                    raise ValueError("工具不在主人格当前可用范围内")
                tool = tools.get_tool(name)
            else:
                tool_manager = self.plugin.context.get_llm_tool_manager()
                tool = tool_manager.get_func(clean_tool_name) or tool_manager.get_func(
                    tool_name
                )
            if tool is None:
                failed = True
                logger.error(f"[Giftia] {bot_name} Tool not found: {tool_name}")
                result.append("工具不存在")
            else:
                log_name = tool.name
                run_context = AgentContextWrapper(
                    context=AstrAgentContext(
                        context=self.plugin.context,
                        event=event,
                        extra={
                            NATIVE_CONTEXT_KEY: json.dumps(
                                tool_context, ensure_ascii=False
                            )
                        }
                        if tool_context is not None
                        else {},
                    ),
                    tool_call_timeout=self.plugin.tools_config.get("timeout", 120),
                )
                async for tool_result in FunctionToolExecutor.execute(
                    tool, run_context, **tool_args
                ):
                    if isinstance(tool_result, str):
                        result.append(tool_result)
                    elif isinstance(tool_result, mcp.types.CallToolResult):
                        failed = failed or tool_result.isError
                        for content in tool_result.content:
                            if isinstance(content, mcp.types.TextContent):
                                result.append(content.text)
                            elif isinstance(content, mcp.types.ImageContent):
                                await event.send(
                                    MessageChain([Image.fromBase64(content.data)])
                                )
                                result.append("图片已直接发送给用户")
                if tool_context is not None:
                    tool_context.update(
                        json.loads(
                            run_context.context.extra.get(NATIVE_CONTEXT_KEY, "{}")
                        )
                    )
        except asyncio.CancelledError as exc:
            interrupted = exc
        except Exception as e:
            failed = True
            logger.error(f"Error executing tool {tool_name}: {e}", exc_info=True)
            result.append(f"工具执行失败: {e}")

        tool_output = "\n".join(result)
        status = (
            "interrupted"
            if interrupted is not None
            else "failed"
            if failed
            else "finished"
        )
        message_id = f"tool_{uuid.uuid4().hex}_{status}"
        content = f"<tool_call name={quoteattr(log_name)} status={quoteattr(status)}/>"
        await self._record_operation_log(
            event, bot_name, nickname, group_or_user_id, message_id, content
        )
        if interrupted is not None:
            raise interrupted
        return {"name": tool_name, "results": tool_output}
