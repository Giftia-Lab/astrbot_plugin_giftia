"""Capture native SDK tool calls in the owning Giftia conversation."""

import asyncio
import inspect
import json
import uuid
from contextlib import aclosing
from copy import copy
from datetime import datetime
from functools import wraps
from xml.sax.saxutils import escape, quoteattr

from astrbot.core.agent.hooks import BaseAgentRunHooks
from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.astr_agent_context import AstrAgentContext

from ..utils.schemas import MessageData


def tool_value(value):
    """Return the complete SDK value in a JSON-serializable form."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    return value


class ReplyToolHistory(BaseAgentRunHooks[AstrAgentContext]):
    """Keep start and completion records paired even when the native loop raises."""

    def __init__(self, plugin, event, bot_name, session):
        self.cache = getattr(plugin, "data_cache", None)
        self.nickname = (getattr(plugin, "bot_map", {}) or {}).get(bot_name, {}).get(
            "nickname"
        ) or bot_name
        self.event, self.bot_name, self.session = event, bot_name, session
        self.called_tools = []
        self.pending = None

    async def _record(self, status, body):
        if self.cache is None or not self.bot_name:
            return
        call_id, name = self.pending["id"], self.pending["name"]
        content = escape(json.dumps(body, ensure_ascii=False, default=str))
        await self.cache.add_message(
            self.bot_name,
            self.session,
            MessageData(
                nickname=self.nickname,
                user_id=str(self.event.get_self_id()),
                group_or_user_id=self.session,
                time=datetime.now().isoformat(),
                message_id=f"tool_{call_id}_{status}",
                role="operation_log",
                content=f"<tool_call id={quoteattr(call_id)} name={quoteattr(name)} status={quoteattr(status)}>{content}</tool_call>",
            ),
        )

    async def on_tool_start(self, run_context, tool, tool_args):
        self.called_tools.append(tool.name)
        await self.finish()
        self.pending = {
            "id": uuid.uuid4().hex,
            "name": tool.name,
            "outputs": [],
            "error": "",
        }
        await self._record("started", {"arguments": tool_args or {}})

    async def on_tool_end(self, run_context, tool, tool_args, tool_result):
        if self.pending is None:
            return
        outputs = self.pending["outputs"]
        if not outputs:
            outputs.append(tool_value(tool_result))
        failure = self.pending["error"] or (
            "工具返回执行错误" if getattr(tool_result, "isError", False) else ""
        )
        await self._record(
            "failed" if failure else "finished",
            {"outputs": outputs, "error": failure},
        )
        self.pending = None

    async def finish(self, error=None):
        """Close a start whose SDK end hook was skipped by an error or cancellation."""
        if self.pending is not None:
            detail = self.pending["error"] or str(
                error or "工具执行中断，未收到完成回执"
            )
            await self._record(
                "failed" if self.pending["error"] or error else "interrupted",
                {"outputs": self.pending["outputs"], "error": detail},
            )
            self.pending = None

    def observe(self, tools):
        """Observe yielded chunks without changing the registered tools or SDK dispatch.

        Args:
            tools: Permission-filtered native tools of this attempt.

        Returns:
            Per-attempt tool copies. The SDK end hook exposes only the last yield.
        """
        if self.cache is None:
            return tools
        observed = ToolSet()
        for original in tools.tools:
            tool = copy(original)
            if tool.handler:
                method = "handler"
            elif type(tool).call is not FunctionTool.call:
                method = "call"
            elif hasattr(tool, "run"):
                method = "run"
            else:
                observed.add_tool(tool)
                continue
            setattr(tool, method, self._observe_callable(getattr(original, method)))
            observed.add_tool(tool)
        return observed

    def _observe_callable(self, function):
        @wraps(function)
        def invoke(*args, **kwargs):
            # Save the invocation identity: background SDK tools can outlive their receipt.
            call = self.pending
            try:
                outcome = function(*args, **kwargs)
            except Exception as exc:
                if call is not None:
                    call["error"] = str(exc) or type(exc).__name__
                raise
            if inspect.isasyncgen(outcome):

                async def stream():
                    try:
                        async with aclosing(outcome):
                            async for part in outcome:
                                if call is not None:
                                    call["outputs"].append(tool_value(part))
                                    if getattr(part, "isError", False):
                                        call["error"] = "工具返回执行错误"
                                yield part
                    except (Exception, asyncio.CancelledError) as exc:
                        if call is not None:
                            call["error"] = str(exc) or type(exc).__name__
                        raise

                return stream()
            if inspect.isawaitable(outcome):

                async def result():
                    try:
                        value = await outcome
                        if call is not None and hasattr(value, "chain"):
                            call["outputs"].append(tool_value(value))
                        return value
                    except (Exception, asyncio.CancelledError) as exc:
                        if call is not None:
                            call["error"] = str(exc) or type(exc).__name__
                        raise

                return result()
            return outcome

        return invoke
