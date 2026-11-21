"""Registered memory and reminder tools for native or XML invocation."""

import json
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from astrbot.api import FunctionTool
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext

from ..conversation.memory_recall import search_memories_with_rerank
from ..memory.recall_scope import resolve_memory_recall_scope
from ..utils.event_utils import get_adapter_id, resolve_bot_name
from .prompt import build_memory_items

NATIVE_CONTEXT_KEY = "giftia_native_tools"
NATIVE_TOOL_CONFIG_KEYS = {
    "search_memory": "search_memory_enabled",
    "create_scheduled_task": "scheduled_tasks_enabled",
    "list_scheduled_tasks": "scheduled_tasks_enabled",
    "delete_scheduled_task": "scheduled_tasks_enabled",
}
DEFAULT_MEMORY_PAGE_SIZE = 10
MAX_MEMORY_PAGE_SIZE = 50


def _tool_scope(plugin, context, name: str) -> tuple[Any, dict]:
    """Resolve trusted routing and identities shared by the four function tools.

    Args:
        plugin: Owning plugin instance.
        context: Current agent run, independent of model-supplied arguments.
        name: Tool whose plugin configuration switch must be enabled.

    Returns:
        Routing event and mutable run state serialized in the agent context.

    Raises:
        ValueError: The bot, plugin switch or trusted run scope is invalid.
    """
    event = context.context.event
    bot_name = resolve_bot_name(plugin, event)
    if not bot_name:
        raise ValueError("未找到当前会话对应的 Bot")
    bot = plugin.bot_map.get(bot_name)
    if not isinstance(bot, dict):
        raise ValueError("当前 Bot 配置无效")
    if not getattr(plugin, "tools_config", {}).get(NATIVE_TOOL_CONFIG_KEYS[name], True):
        raise ValueError("插件配置未启用此工具")
    session_id = str(event.get_group_id() or event.get_sender_id() or "")
    if not session_id:
        raise ValueError("未找到当前会话")
    extra = getattr(context.context, "extra", None)
    if not isinstance(extra, dict):
        extra = context.context.extra = {}
    raw = extra.get(NATIVE_CONTEXT_KEY)
    if raw:
        try:
            state = json.loads(raw)
        except (TypeError, ValueError) as error:
            raise ValueError("工具上下文无效") from error
        if (
            not isinstance(state, dict)
            or state.get("bot_name") != bot_name
            or state.get("session_id") != session_id
        ):
            raise ValueError("工具上下文与当前 Bot 或会话不匹配")
        if (
            not isinstance(state.get("creator_names"), dict)
            or not isinstance(state.get("user_ids"), list)
            or not state.get("request_id")
        ):
            raise ValueError("工具上下文缺少可信用户或请求信息")
    else:
        sender = str(event.get_sender_id() or "")
        state = {
            "bot_name": bot_name,
            "session_id": session_id,
            "creator_names": {sender: str(event.get_sender_name() or sender)}
            if sender
            else {},
            "user_ids": [sender] if sender else [],
            "request_id": uuid.uuid4().hex,
        }
        extra[NATIVE_CONTEXT_KEY] = json.dumps(state, ensure_ascii=False)
    return event, state


def _session_reminders(plugin, state: dict) -> list:
    """Read reminders whose stored routing exactly matches the current run.

    Args:
        plugin: Plugin owning the scheduler.
        state: Verified bot and conversation identifiers.

    Returns:
        Non-system reminder jobs; ID prefixes alone never establish ownership.
    """
    return [
        job
        for job in plugin.task_manager.scheduler.get_jobs()
        if not str(job.id).startswith("system_")
        and not (job.kwargs or {}).get("system_job")
        and job.args
        and job.args[0] == "remind"
        and (job.kwargs or {}).get("bot_name") == state["bot_name"]
        and (job.kwargs or {}).get("group_or_user_id") == state["session_id"]
    ]


@dataclass
class SearchMemoryTool(FunctionTool):
    plugin: Any = None
    name: str = "search_memory"
    description: str = (
        "按配置的召回范围检索自身的长期记忆。所有参数均可省略，"
        "省略 query 时浏览已保存的记忆。可按创建日期筛选并翻页，默认最新记录在前。"
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "语义检索的问题或关键词。省略或留空时按条件浏览记忆。",
                },
                "user_id": {
                    "type": "string",
                    "description": "可选目标用户 ID，只能缩小配置允许的召回范围。",
                },
                "start_date": {
                    "type": "string",
                    "description": "创建日期下限，YYYY-MM-DD，包含当天。",
                },
                "end_date": {
                    "type": "string",
                    "description": "创建日期上限，YYYY-MM-DD，包含当天。",
                },
                "page": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 1,
                    "description": "页码，从 1 开始。返回 has_more 为 true 时可查询下一页。",
                },
                "page_size": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_MEMORY_PAGE_SIZE,
                    "default": DEFAULT_MEMORY_PAGE_SIZE,
                    "description": "每页最多返回的记忆条数。",
                },
                "sort_order": {
                    "type": "string",
                    "enum": ["asc", "desc"],
                    "default": "desc",
                    "description": "按记忆创建时间排序。asc 从旧到新，desc 从新到旧。",
                },
            },
            "required": [],
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        query: str | None = None,
        user_id: str = "",
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        page: int = 1,
        page_size: int = DEFAULT_MEMORY_PAGE_SIZE,
        sort_order: str = "desc",
    ) -> str:
        """Return a page of memory context in creation-time order.

        Args:
            context: Trusted agent run context.
            query: Semantic recall query, or empty to browse saved memories.
            user_id: Optional explicit target account.
            start_date: Inclusive first creation date.
            end_date: Inclusive last creation date.
            page: One-based page number.
            page_size: Maximum records on this page.
            sort_order: Ascending or descending creation-time order.

        Returns:
            JSON containing memory context and pagination, or an actionable error.
        """
        try:
            _, state = _tool_scope(self.plugin, context, self.name)
            if query is not None and not isinstance(query, str):
                raise ValueError("query 必须为字符串")
            query = (query or "").strip()
            for name, value in (("start_date", start_date), ("end_date", end_date)):
                if value in (None, ""):
                    continue
                try:
                    if date.fromisoformat(value).isoformat() == value:
                        continue
                except (TypeError, ValueError):
                    pass
                raise ValueError(f"{name} 必须是有效的 YYYY-MM-DD 日期")
            if start_date and end_date and start_date > end_date:
                raise ValueError("start_date 不能晚于 end_date")
            if type(page) is not int or page < 1:
                raise ValueError("page 必须为正整数")
            if type(page_size) is not int or not 1 <= page_size <= MAX_MEMORY_PAGE_SIZE:
                raise ValueError(f"page_size 必须为 1 到 {MAX_MEMORY_PAGE_SIZE} 的整数")
            if sort_order not in ("asc", "desc"):
                raise ValueError("sort_order 必须为 asc 或 desc")
            offset = (page - 1) * page_size
            if offset > (1 << 63) - 1:
                raise ValueError("page 超出允许范围")

            target = str(user_id or "").strip()
            user_ids = [target] if target else None
            if not target and resolve_memory_recall_scope(self.plugin) == "user":
                user_ids = state["user_ids"]
            filters = {
                "bot_name": state["bot_name"],
                "group_or_user_id": state["session_id"],
                "recent_messages": [],
                "user_ids": user_ids,
                "start_date": start_date,
                "end_date": end_date,
            }
            if query:
                if not self.plugin.embedding_conf.get("enabled", False):
                    raise ValueError("长期记忆语义检索未启用嵌入模型")
                memories = await search_memories_with_rerank(
                    self.plugin,
                    query=query,
                    log_context="Explicit memory search",
                    record_hits=False,
                    **filters,
                )
                direction = 1 if sort_order == "asc" else -1
                memories.sort(
                    key=lambda memory: (
                        memory.get("created_at_us") is None,
                        (memory.get("created_at_us") or 0) * direction,
                        str(memory.get("memory_id") or memory.get("id") or ""),
                    )
                )
                memories = memories[offset : offset + page_size + 1]
            else:
                memories = (
                    await self.plugin.passive_memory_manager.search_and_filter_memories(
                        query=None,
                        limit=page_size + 1,
                        sort_order=sort_order,
                        offset=offset,
                        **filters,
                    )
                )
            has_more = len(memories) > page_size
            memories = memories[:page_size]
            if memories:
                await self.plugin.data_cache.record_memory_hits(memories)
            return json.dumps(
                {
                    "status": "success",
                    "count": len(memories),
                    "page": page,
                    "page_size": page_size,
                    "has_more": has_more,
                    "memory_context": build_memory_items(
                        memories, current_session_id=state["session_id"]
                    ),
                },
                ensure_ascii=False,
            )
        except ValueError as error:
            return json.dumps(
                {"status": "error", "message": str(error)}, ensure_ascii=False
            )


@dataclass
class CreateScheduledTaskTool(FunctionTool):
    plugin: Any = None
    name: str = "create_scheduled_task"
    description: str = "在当前 Bot、当前会话创建定时提醒。时间或内容不明确时先向用户询问。必须读取返回的执行状态，成功后才能确认已设置；同一轮相同请求不会重复创建。"
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "任务创建者的用户 ID，必须来自本轮可信上下文或当前会话已查询到的任务。",
                },
                "time_expr": {
                    "type": "string",
                    "description": "五段 Cron 表达式，或 ISO8601 具体时间（建议包含时区偏移）。",
                },
                "content": {"type": "string", "description": "提醒内容。"},
            },
            "required": ["user_id", "time_expr", "content"],
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        user_id: str,
        time_expr: str,
        content: str,
    ) -> str:
        """Create one idempotent reminder with verified creator and routing.

        Args:
            context: Trusted agent run context.
            user_id: Account that owns the reminder.
            time_expr: Cron expression or future ISO timestamp.
            content: Reminder body.

        Returns:
            JSON with the actual task ID, next execution time and status.
        """
        try:
            event, state = _tool_scope(self.plugin, context, self.name)
            creator = str(user_id or "").strip()
            if not creator or creator not in state["creator_names"]:
                raise ValueError("创建者 user_id 不在本轮可信上下文中，请先确认创建者")
            if (
                not isinstance(time_expr, str)
                or not isinstance(content, str)
                or not time_expr.strip()
                or not content.strip()
            ):
                raise ValueError("提醒时间和内容不能为空")
            time_expr, content = time_expr.strip(), content.strip()
            try:
                when = datetime.fromisoformat(time_expr)
            except ValueError:
                when = None
            if when is not None and when <= datetime.now(when.tzinfo):
                raise ValueError("提醒时间必须晚于当前时间")
            identity = json.dumps(
                [
                    state["bot_name"],
                    state["session_id"],
                    state["request_id"],
                    creator,
                    time_expr,
                    content,
                ],
                ensure_ascii=False,
            )
            task_id = f"{state['bot_name']}_{state['session_id']}_{uuid.uuid5(uuid.NAMESPACE_URL, identity).hex}"
            existing = self.plugin.task_manager.scheduler.get_job(task_id)
            if existing is not None:
                if existing not in _session_reminders(self.plugin, state):
                    raise ValueError("任务 ID 与当前会话不匹配")
                return json.dumps(
                    {
                        "status": "exists",
                        "task_id": task_id,
                        "message": "本轮相同任务已创建",
                    },
                    ensure_ascii=False,
                )
            bot = self.plugin.bot_map[state["bot_name"]]
            message = self.plugin.task_manager.add_job(
                task_id,
                "remind",
                time_expr,
                kwargs={
                    "unified_msg_origin": event.unified_msg_origin,
                    "adapter_id": get_adapter_id(event),
                    "bot_name": state["bot_name"],
                    "nickname": bot.get("nickname", state["bot_name"]),
                    "self_id": event.get_self_id(),
                    "platform_name": event.get_platform_name(),
                    "user_id": creator,
                    "user_name": state["creator_names"][creator],
                    "group_id": event.get_group_id(),
                    "group_or_user_id": state["session_id"],
                    "remind_message": content,
                },
            )
            job = self.plugin.task_manager.scheduler.get_job(task_id)
            if job is None:
                raise ValueError(message or "任务创建失败")
            return json.dumps(
                {
                    "status": "created",
                    "task_id": task_id,
                    "user_id": creator,
                    "time_expr": time_expr,
                    "next_run_time": str(getattr(job, "next_run_time", "") or ""),
                    "message": message,
                },
                ensure_ascii=False,
            )
        except ValueError as error:
            return json.dumps(
                {"status": "error", "message": str(error)}, ensure_ascii=False
            )


@dataclass
class ListScheduledTasksTool(FunctionTool):
    plugin: Any = None
    name: str = "list_scheduled_tasks"
    description: str = "查看当前 Bot、当前会话的定时提醒，取得真实任务 ID、创建者、时间和内容；删除或调整任务前可先查询。"
    parameters: dict = field(
        default_factory=lambda: {"type": "object", "properties": {}, "required": []}
    )

    async def call(self, context: ContextWrapper[AstrAgentContext]) -> str:
        """List verified reminders and retain their trusted creator identities.

        Args:
            context: Trusted agent run context.

        Returns:
            JSON containing current-conversation reminder records.
        """
        try:
            _, state = _tool_scope(self.plugin, context, self.name)
            items = []
            for job in _session_reminders(self.plugin, state):
                creator = str(job.kwargs.get("user_id") or "")
                creator_name = str(job.kwargs.get("user_name") or creator)
                if creator:
                    state["creator_names"][creator] = creator_name
                items.append(
                    {
                        "task_id": job.id,
                        "user_id": creator,
                        "user_name": creator_name,
                        "time_expr": job.name.split("|", 1)[1]
                        if "|" in job.name
                        else "",
                        "content": job.kwargs.get("remind_message", ""),
                        "next_run_time": str(getattr(job, "next_run_time", "") or ""),
                    }
                )
            context.context.extra[NATIVE_CONTEXT_KEY] = json.dumps(
                state, ensure_ascii=False
            )
            return json.dumps({"status": "success", "tasks": items}, ensure_ascii=False)
        except ValueError as error:
            return json.dumps(
                {"status": "error", "message": str(error)}, ensure_ascii=False
            )


@dataclass
class DeleteScheduledTaskTool(FunctionTool):
    plugin: Any = None
    name: str = "delete_scheduled_task"
    description: str = "删除当前 Bot、当前会话中的定时提醒。task_id 必须使用查询返回的真实 ID；不能删除其他会话或系统任务。读取返回结果后再确认删除。"
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "定时任务查询返回的完整任务 ID。",
                }
            },
            "required": ["task_id"],
        }
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], task_id: str
    ) -> str:
        """Delete one scoped reminder and verify it no longer exists.

        Args:
            context: Trusted agent run context.
            task_id: Stored reminder identifier.

        Returns:
            JSON with a verified deletion status or an actionable error.
        """
        try:
            _, state = _tool_scope(self.plugin, context, self.name)
            task_id = str(task_id or "").strip()
            if task_id not in {
                job.id for job in _session_reminders(self.plugin, state)
            }:
                raise ValueError("未找到属于当前 Bot、当前会话的提醒任务")
            message = self.plugin.task_manager.remove_job(task_id)
            if self.plugin.task_manager.scheduler.get_job(task_id) is not None:
                raise ValueError(message or "任务删除失败")
            return json.dumps(
                {"status": "deleted", "task_id": task_id, "message": message},
                ensure_ascii=False,
            )
        except ValueError as error:
            return json.dumps(
                {"status": "error", "message": str(error)}, ensure_ascii=False
            )
