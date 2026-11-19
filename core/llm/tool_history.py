"""Capture native SDK tool calls in the owning Giftia conversation."""

import asyncio
import inspect
import uuid
from contextlib import aclosing
from copy import copy
from datetime import datetime
from functools import wraps
from xml.sax.saxutils import quoteattr

from astrbot.core.agent.hooks import BaseAgentRunHooks
from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.astr_agent_context import AstrAgentContext

from ..utils.schemas import MessageData


class ReplyToolHistory(BaseAgentRunHooks[AstrAgentContext]):
    """Fold a reply turn's native tool calls into one deduplicated call-only log."""

    def __init__(self, plugin, event, bot_name, session):
        self.cache = getattr(plugin, "data_cache", None)
        bot = (getattr(plugin, "bot_map", {}) or {}).get(bot_name, {})
        self.nickname = bot.get("nickname") or bot_name
        self.event, self.bot_name, self.session = event, bot_name, session
        self.called_tools: list[str] = []
        self.tool_statuses: dict[str, str] = {}
        self.pending = None
        self._recorded = False
        self._run_id = uuid.uuid4().hex

    def _finalize_pending(self, status: str) -> None:
        if self.pending is None:
            return
        self.tool_statuses[self.pending["name"]] = status
        self.pending = None

    async def on_tool_start(self, run_context, tool, tool_args):
        self.called_tools.append(tool.name)
        if self.pending is not None:
            self._finalize_pending("failed" if self.pending["error"] else "interrupted")
        self.pending = {"name": tool.name, "error": False}
        self.tool_statuses.setdefault(tool.name, "started")

    async def on_tool_end(self, run_context, tool, tool_args, tool_result):
        if self.pending is None:
            return
        failed = self.pending["error"] or bool(getattr(tool_result, "isError", False))
        self._finalize_pending("failed" if failed else "finished")

    async def finish(self, error=None, *, tools_called=None):
        """Record one deduplicated call-only operation_log for the finished turn."""
        if self.pending is not None:
            self._finalize_pending(
                "failed" if self.pending["error"] or error else "interrupted"
            )
        for name in tools_called or ():
            if name and name not in self.tool_statuses:
                self.tool_statuses[name] = "failed" if error else "finished"
        if (
            self._recorded
            or self.cache is None
            or not self.bot_name
            or not self.tool_statuses
        ):
            return
        self._recorded = True
        content = "\n".join(
            f"<tool_call name={quoteattr(name)} status={quoteattr(status)}/>"
            for name, status in self.tool_statuses.items()
        )
        await self.cache.add_message(
            self.bot_name,
            self.session,
            MessageData(
                nickname=self.nickname,
                user_id=str(self.event.get_self_id()),
                group_or_user_id=self.session,
                time=datetime.now().isoformat(),
                message_id=f"tool_{self._run_id}",
                role="operation_log",
                content=content,
            ),
        )

    def observe(self, tools):
        """Observe tool execution outcomes without recording arguments or outputs.

        Args:
            tools: Permission-filtered native tools of this attempt.

        Returns:
            Per-attempt tool copies whose failure states are tracked.
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
            except Exception:
                if call is not None:
                    call["error"] = True
                raise
            if inspect.isasyncgen(outcome):

                async def stream():
                    try:
                        async with aclosing(outcome):
                            async for part in outcome:
                                if call is not None and getattr(part, "isError", False):
                                    call["error"] = True
                                yield part
                    except (Exception, asyncio.CancelledError):
                        if call is not None:
                            call["error"] = True
                        raise

                return stream()
            if inspect.isawaitable(outcome):

                async def result():
                    try:
                        return await outcome
                    except (Exception, asyncio.CancelledError):
                        if call is not None:
                            call["error"] = True
                        raise

                return result()
            return outcome

        return invoke
