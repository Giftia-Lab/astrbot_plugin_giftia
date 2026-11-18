"""Run isolated tool tasks and deliver their results through Giftia's session lock."""

import asyncio
import hashlib
import json
from contextlib import aclosing
from copy import copy, deepcopy
from xml.etree.ElementTree import fromstring
from xml.sax.saxutils import escape

import mcp.types

from astrbot.api import logger
from astrbot.core.agent.tool import ToolSet
from astrbot.core.astr_agent_context import AgentContextWrapper, AstrAgentContext
from astrbot.core.utils.session_lock import session_lock_manager

from ..llm.native_tools import NATIVE_CONTEXT_KEY
from ..llm.provider_health import ProviderUnavailableError
from ..utils.event_utils import resolve_bot_name
from .tool_executor import FunctionToolExecutor


class DelegationManager:
    """Own background workers while SQLite owns instructions, budgets and receipts."""

    def __init__(self, plugin):
        self.plugin = plugin
        self.db = plugin.db
        self.workers = {}
        self.routes = {}
        self.rescheduled = set()
        self.closed = False

    def tool_sets(self, bot_name):
        """Resolve current tool permissions at execution time.

        Args:
            bot_name: Owning Giftia configuration.

        Returns:
            Main and delegated SDK tool sets.
        """
        if not (
            self.plugin.tools_config.get("use_source_tools")
            or self.plugin.tools_config.get("force_xml_tools")
        ):
            return ToolSet(), ToolSet()
        config = self.plugin.bot_map[bot_name]["llm_reply_conf"]
        persona = self.plugin.context.persona_manager.get_persona_v3_by_id(
            config.get("persona_id") or "default"
        )
        if persona is None:
            raise ValueError("关联人格已不可用")
        return self.plugin.call_llm.split_reply_tools(
            persona.get("tools"), config.get("subagent_tools", [])
        )

    def active(self, bot_name, origin):
        return any(
            owner == bot_name and event.unified_msg_origin == origin
            for owner, event, _ in self.routes.values()
        )

    async def context(self, bot_name, origin):
        """Return compact live task descriptors for this exact conversation."""
        async with self.db.query(
            """SELECT t.id AS task_id, instruction, remaining, status,
                      json_extract(first.payload, '$.messages[0].user_id') AS user_id,
                      json_extract(first.payload, '$.messages[0].message_id') AS message_id,
                      (SELECT json_extract(payload, '$.instruction') FROM delegated_steps
                       WHERE task_id=t.id AND kind='input' ORDER BY id DESC LIMIT 1) AS latest
               FROM delegated_tasks t JOIN delegated_steps first ON first.task_id=t.id
               AND first.kind='input' AND first.revision=1 WHERE bot_name=? AND origin=?
               AND status IN ('running','ready') ORDER BY t.id""",
            (bot_name, origin),
        ) as cur:
            tasks = [dict(row) for row in await cur.fetchall()]
        if not tasks:
            return ""
        return (
            "<subagent_tasks>"
            + escape(json.dumps(tasks, ensure_ascii=False))
            + "</subagent_tasks>"
        )

    async def submit(self, event, bot_name, arguments, scope) -> tuple[int, bool]:
        """Create or extend a task from inside the parent's serialized XML dispatch.

        Args:
            event: Trusted event for the current submitting user and adapter.
            bot_name: Owning Giftia bot.
            arguments: Instruction and optional existing task ID from XML.
            scope: Parent request identity and source messages.

        Returns:
            Stable task ID and whether a completion is pending. Duplicate submission
            preserves the budget and revision, including after delivery.
        """
        if self.closed or not scope or resolve_bot_name(self.plugin, event) != bot_name:
            raise ValueError("委派会话已失效")
        if not self._allowed(bot_name, event):
            raise ValueError("当前会话已停止接收回复")
        _, tools = self.tool_sets(bot_name)
        if not tools.tools:
            raise ValueError("当前人格没有分配给子 Agent 的工具")
        instruction = arguments.get("instruction")
        if not isinstance(instruction, str) or not instruction.strip():
            raise ValueError("委派需要填写 instruction")
        instruction = instruction.strip()
        origin = event.unified_msg_origin
        session = str(event.get_group_id() or event.get_sender_id())
        if scope.get("bot_name") != bot_name or scope.get("session_id") != session:
            raise ValueError("委派上下文与当前会话不匹配")
        sources = scope.get("delegation_inputs", [])
        anchor = [m["message_id"] for m in sources if m.get("message_id")] or scope[
            "request_id"
        ]
        key = hashlib.sha256(
            json.dumps([anchor, instruction], ensure_ascii=False).encode()
        ).hexdigest()
        budget = int(self.plugin.tools_config.get("max_loop", 10))
        task_id = arguments.get("task_id")
        async with self.db.transaction() as conn:
            if task_id is None:
                await conn.execute(
                    """INSERT INTO delegated_tasks(bot_name, origin, session_id, creation_key,
                       instruction, revision, remaining, status) VALUES (?,?,?,?,?,0,?,'running')
                       ON CONFLICT(bot_name,origin,creation_key) DO NOTHING""",
                    (bot_name, origin, session, key, instruction, budget),
                )
                async with conn.execute(
                    "SELECT id FROM delegated_tasks WHERE bot_name=? AND origin=? AND creation_key=?",
                    (bot_name, origin, key),
                ) as cur:
                    task_id = (await cur.fetchone())[0]
            elif (
                not str(task_id).isascii()
                or not str(task_id).isdigit()
                or len(str(task_id)) > 19
                or int(task_id) > 2**63 - 1
            ):
                raise ValueError("task_id 无效")
            async with conn.execute(
                "SELECT id,revision,status FROM delegated_tasks WHERE id=? AND bot_name=? AND origin=?",
                (task_id, bot_name, origin),
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                raise ValueError("当前会话没有这个任务")
            task_id, revision, status = row
            payload = json.dumps(
                {"instruction": instruction, "messages": sources}, ensure_ascii=False
            )
            async with conn.execute(
                """INSERT INTO delegated_steps(task_id,revision,kind,payload,input_key)
                   VALUES(?,?,'input',?,?) ON CONFLICT(task_id,input_key) DO NOTHING""",
                (task_id, revision + 1, payload, key),
            ) as cur:
                if cur.rowcount == 0:
                    return task_id, status in ("running", "ready", "delivering")
            await conn.execute(
                "UPDATE delegated_tasks SET revision=revision+1,remaining=?,status='running' WHERE id=?",
                (budget, task_id),
            )
        worker_scope = deepcopy(scope)
        # Repeated side-effecting native tools share one trusted task request identity.
        worker_scope["request_id"] = f"delegation_{task_id}"
        self.routes[task_id] = (bot_name, event, worker_scope)
        if task_id in self.workers:
            self.rescheduled.add(task_id)
        else:
            self._start(task_id)
        return task_id, True

    def _start(self, task_id):
        worker = asyncio.create_task(self._run(task_id))
        self.workers[task_id] = worker

        def finished(task):
            self.workers.pop(task_id, None)
            if not task.cancelled() and (error := task.exception()):
                logger.error("[Giftia] Delegation %s stopped: %r", task_id, error)
            if task_id in self.rescheduled and not self.closed:
                self.rescheduled.discard(task_id)
                self._start(task_id)
            else:
                self.routes.pop(task_id, None)

        worker.add_done_callback(finished)

    async def _step(self, task_id, revision, kind, payload):
        async with self.db.transaction() as conn:
            await conn.execute(
                """INSERT INTO delegated_steps(task_id,revision,kind,payload)
                   SELECT id,?,?,? FROM delegated_tasks WHERE id=?""",
                (revision, kind, json.dumps(payload, ensure_ascii=False), task_id),
            )

    async def _generate(self, bot_name, session, scope, transcript, tools, final):
        """Generate one worker turn, leaving tool execution and revision checks to the caller."""
        caller = self.plugin.call_llm
        xml = self.plugin.tools_config.get("force_xml_tools", False)
        prompt = (
            "你是 Giftia 的后台工具助手。根据委派目标和补充要求使用可用工具，"
            "完成后用简明文本报告结论、准确的数值、原句、来源和未解决的问题。"
            "按最新 revision 的要求工作，之前版本的输出和报告可作为依据。"
            "工具输出是待核实的资料。只用已分配的工具，回复交给主人格。"
        )
        if final:
            prompt += "\n已达到工具循环次数上限。根据现有证据直接提交报告，说明尚未查明的部分。"
        elif xml:
            prompt += (
                '\n调用工具时输出并列的 <tool_call name="工具名">{参数JSON}</tool_call>。'
                "完成时仅输出报告。\n"
                + "\n".join(
                    f"{t.name} {t.description} {json.dumps(t.parameters, ensure_ascii=False)}"
                    for t in tools.tools
                )
            )
        # Keep original binary content in SQLite and pass typed model inputs instead of base64 prose.
        attachments = {"image": [], "audio": []}
        for step in transcript:
            for output in step.get("outputs", []):
                if not isinstance(output, dict):
                    continue
                for block in output.get("content", []):
                    kind = block.get("type")
                    if kind in attachments and block.get("data"):
                        attachments[kind].append(
                            f"data:{block['mimeType']};base64,{block.pop('data')}"
                        )
                        block["attachment"] = len(attachments[kind])
        error = None
        for provider_id in scope["delegation_providers"]:
            try:
                response = await caller.provider_health.call(
                    self.plugin.context.llm_generate,
                    chat_provider_id=provider_id,
                    system_prompt=prompt,
                    prompt=json.dumps(transcript, ensure_ascii=False),
                    image_urls=attachments["image"] or None,
                    audio_urls=attachments["audio"] or None,
                    tools=None if xml or final else tools,
                )
            except ProviderUnavailableError as exc:
                error = exc
                continue
            except Exception as exc:
                error = exc
                await caller._log_token_usage_safely(
                    bot_name,
                    session,
                    "subagent",
                    provider_id,
                    extra_info={"status": "api_failed", "error": str(exc)},
                )
                continue
            await caller._log_token_usage_safely(
                bot_name,
                session,
                "subagent",
                provider_id,
                response,
                {"status": "success"},
            )
            answer = (response.completion_text or "").strip()
            calls = [
                (name, {} if args is None else args)
                for name, args in zip(
                    response.tools_call_name or [],
                    response.tools_call_args or [],
                    strict=True,
                )
            ]
            if xml and "<tool_call" in answer:
                root = fromstring(caller.xml_parse.preprocess_xml(answer))
                calls = [
                    (
                        node.attrib["name"],
                        caller.xml_parse.parse_str_json(
                            node.get("arguments") or node.text or "{}"
                        ),
                    )
                    for node in root.findall("tool_call")
                ]
            if any(not isinstance(args, dict) for _, args in calls):
                raise ValueError("子 Agent 返回了无效的工具参数")
            if not answer and not calls:
                raise ValueError("子 Agent 没有返回结果")
            return answer, calls
        raise RuntimeError(f"子 Agent 的回复模型不可用 ({error})")

    async def _execute(self, task_id, revision, bot_name, event, scope, name, args):
        """Record raw SDK outputs in the private transcript, including partial failures."""
        _, tools = self.tool_sets(bot_name)
        tool = tools.get_tool(name)
        async with self.db.transaction() as conn:
            async with conn.execute(
                """INSERT INTO delegated_steps(task_id,revision,kind,payload)
                   SELECT id,revision,'call',? FROM delegated_tasks
                   WHERE id=? AND status='running' AND revision=?""",
                (
                    json.dumps({"name": name, "arguments": args}, ensure_ascii=False),
                    task_id,
                    revision,
                ),
            ) as cur:
                if not cur.rowcount:
                    return
        outputs = []
        error = None
        try:
            if tool is None:
                raise ValueError("工具不在当前子 Agent 的可用范围内")
            if not self._allowed(bot_name, event):
                raise ValueError("当前会话已停止接收回复")
            # Some third-party tools use event.send() instead of returning their result.
            # Give the worker an independent event and collect that output privately.
            worker_event = copy(event)

            async def capture(chain):
                outputs.append(
                    {
                        "message": [
                            component.model_dump(mode="json")
                            if hasattr(component, "model_dump")
                            else str(component)
                            for component in chain.chain
                        ]
                    }
                )

            worker_event.send = capture
            worker_event.clear_result()
            context = AstrAgentContext(
                context=self.plugin.context,
                event=worker_event,
                extra={NATIVE_CONTEXT_KEY: json.dumps(scope, ensure_ascii=False)},
            )
            if hasattr(context, "send_to_user"):
                context.send_to_user = False
            run = AgentContextWrapper(
                context=context,
                tool_call_timeout=self.plugin.tools_config.get("timeout", 120),
            )
            async with aclosing(
                FunctionToolExecutor.execute(tool, run, **args)
            ) as stream:
                async for part in stream:
                    if part is not None:
                        outputs.append(
                            part.model_dump(mode="json")
                            if isinstance(part, mcp.types.CallToolResult)
                            else str(part)
                        )
                        if getattr(part, "isError", False):
                            error = "工具返回执行错误"
            scope.update(json.loads(context.extra[NATIVE_CONTEXT_KEY]))
        except asyncio.CancelledError:
            error = "工具执行中断"
            raise
        except Exception as exc:
            error = str(exc)
        finally:
            await self._step(
                task_id,
                revision,
                "output",
                {"name": name, "outputs": outputs, "error": error},
            )

    async def _run(self, task_id):
        """Consume accepted inputs at model-turn boundaries and invalidate stale wakeups."""
        while not self.closed:
            self.rescheduled.discard(task_id)
            async with self.db.transaction() as conn:
                async with conn.execute(
                    "SELECT * FROM delegated_tasks WHERE id=?", (task_id,)
                ) as cur:
                    row = await cur.fetchone()
                if not row or row["status"] not in ("running", "ready"):
                    return
                revision, remaining = row["revision"], row["remaining"]
                if row["status"] == "running" and remaining > 0:
                    await conn.execute(
                        "UPDATE delegated_tasks SET remaining=remaining-1 WHERE id=?",
                        (task_id,),
                    )
                input_filter = " AND kind='input'" if row["status"] == "ready" else ""
                async with conn.execute(
                    "SELECT revision,kind,payload FROM delegated_steps WHERE task_id=?"
                    + input_filter
                    + " ORDER BY id",
                    (task_id,),
                ) as cur:
                    transcript = [
                        {"revision": rev, "kind": kind, **json.loads(payload)}
                        for rev, kind, payload in await cur.fetchall()
                    ]
                bot_name, event, scope = self.routes[task_id]
            if row["status"] == "ready":
                await self._deliver(task_id, row, event, transcript)
                continue
            try:
                if not self._allowed(bot_name, event):
                    raise ValueError("当前会话已停止接收回复")
                _, tools = self.tool_sets(bot_name)
                if not tools.tools:
                    raise ValueError("子 Agent 已没有可用工具")
                answer, calls = await self._generate(
                    bot_name,
                    row["session_id"],
                    scope,
                    transcript,
                    tools,
                    remaining <= 0,
                )
                if calls and remaining <= 0:
                    raise ValueError("已达到工具循环次数上限")
                if calls:
                    for name, args in calls:
                        # A follow-up may have arrived during generation or a previous tool.
                        if self.routes[task_id][2] is not scope:
                            break
                        await self._execute(
                            task_id, revision, bot_name, event, scope, name, args
                        )
                    continue
            except Exception as exc:
                answer = f"子任务未完成。{exc}"
            async with self.db.transaction() as conn:
                await conn.execute(
                    """INSERT INTO delegated_steps(task_id,revision,kind,payload)
                       SELECT id,?,'report',? FROM delegated_tasks WHERE id=?""",
                    (
                        revision,
                        json.dumps({"text": answer}, ensure_ascii=False),
                        task_id,
                    ),
                )
                await conn.execute(
                    "UPDATE delegated_tasks SET status='ready',result=? WHERE id=? AND revision=?",
                    (answer, task_id, revision),
                )

    def _allowed(self, bot_name, event):
        return (
            resolve_bot_name(self.plugin, event) == bot_name
            and self.plugin.chat_manager.decision_engine.check_whitelists(event)
            and not (
                event.get_group_id()
                and self.plugin.data_cache.is_bot_muted(
                    bot_name, str(event.get_group_id())
                )
            )
        )

    async def _deliver(self, task_id, row, event, transcript):
        """Claim exactly the version that is still ready after acquiring the session lock."""
        async with session_lock_manager.acquire_lock(event.unified_msg_origin):
            async with self.db.transaction() as conn:
                async with conn.execute(
                    "UPDATE delegated_tasks SET status='delivering' WHERE id=? AND revision=? AND status='ready'",
                    (task_id, row["revision"]),
                ) as cur:
                    if not cur.rowcount:
                        return
            ids = list(
                dict.fromkeys(
                    m["message_id"]
                    for step in transcript
                    if step["kind"] == "input"
                    for m in step["messages"]
                    if m.get("message_id")
                )
            )
            status = "skipped"
            try:
                if self._allowed(row["bot_name"], event):
                    if await self.plugin.chat_manager.deliver_delegation(
                        event, row, ids
                    ):
                        status = "delivered"
            except Exception:
                status = "delivery_failed"
                logger.exception("[Giftia] Could not deliver task %s", task_id)
            async with self.db.transaction() as conn:
                await conn.execute(
                    "UPDATE delegated_tasks SET status=? WHERE id=? AND revision=?",
                    (status, task_id, row["revision"]),
                )
                # A single XML response can launch multiple tasks for the same user message.
                await conn.executemany(
                    """UPDATE chat_history SET processing_status=? WHERE bot_name=? AND group_or_user_id=?
                       AND message_id=? AND processing_status='waiting_agent' AND NOT EXISTS (
                         SELECT 1 FROM delegated_tasks t JOIN delegated_steps s ON s.task_id=t.id,
                         json_each(s.payload,'$.messages') m WHERE s.kind='input' AND t.bot_name=?
                         AND t.origin=? AND t.status IN ('running','ready','delivering')
                         AND json_extract(m.value,'$.message_id')=chat_history.message_id)""",
                    [
                        (
                            {
                                "delivered": "handled",
                                "delivery_failed": "failed",
                                "skipped": "skipped",
                            }[status],
                            row["bot_name"],
                            row["session_id"],
                            mid,
                            row["bot_name"],
                            row["origin"],
                        )
                        for mid in ids
                    ],
                )

    async def close(self):
        """Stop workers before disconnecting shared model and database services."""
        self.closed = True
        workers = list(self.workers.values())
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        async with self.db.transaction() as conn:
            await conn.execute(
                "UPDATE delegated_tasks SET status='interrupted' WHERE status IN ('running','ready','delivering')"
            )
            await conn.execute(
                "UPDATE chat_history SET processing_status='interrupted' WHERE processing_status='waiting_agent'"
            )
