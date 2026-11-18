import json
import uuid
from datetime import datetime

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request


class TaskApi:
    """Session memo and scheduled task APIs."""

    async def get_memos(self):
        try:
            bot = request.query.get("bot_name")
            session = request.query.get("group_or_user_id")
            if not bot or not session:
                return error_response("缺少 bot_name 或 group_or_user_id 参数")
            data = await self.giftia.memos.dashboard(bot, session)
            return json_response({"status": "success", "data": data})
        except Exception as error:
            logger.exception("[Giftia API] Could not read session memos")
            return error_response(f"获取备忘录失败：{error}")

    async def add_memo(self):
        return await self._change_memo("add")

    async def renew_memo(self):
        return await self._change_memo("renew")

    async def delete_memo(self):
        return await self._change_memo("delete")

    async def _change_memo(self, action: str):
        try:
            body = await request.json()
            bot, session = body.get("bot_name"), body.get("group_or_user_id")
            if not bot or not session:
                return error_response("缺少 bot_name 或 group_or_user_id 参数")
            memo_id = await self.giftia.memos.apply(
                bot,
                session,
                action,
                content=body.get("content", ""),
                duration=body.get("duration", ""),
                memo_id=body.get("memo_id", ""),
            )
            return json_response({"status": "success", "data": {"memo_id": memo_id}})
        except ValueError as error:
            return error_response(str(error))
        except Exception as error:
            logger.exception("[Giftia API] Could not change session memo")
            return error_response(f"备忘操作失败：{error}")

    # ── Scheduled Task APIs ─────────────────────────────────────────────

    async def get_scheduled_tasks(self):
        """Get scheduled tasks for a session."""
        try:
            bot_name = request.query.get("bot_name")
            group_or_user_id = request.query.get("group_or_user_id")

            if not bot_name or not group_or_user_id:
                return error_response("缺少 bot_name 或 group_or_user_id 参数")
            if not hasattr(self.giftia, "task_manager"):
                return error_response("定时任务调度器不可用")

            jobs = self.giftia.task_manager.get_session_jobs_data(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
            )
            return json_response(
                {
                    "status": "success",
                    "data": {
                        "items": jobs,
                        "total": len(jobs),
                    },
                }
            )
        except Exception as e:
            logger.error(f"[Giftia API] get_scheduled_tasks error: {e}")
            return error_response(f"获取定时任务列表失败: {str(e)}")

    async def create_scheduled_task(self):
        """Create a reminder with routing recorded from the destination session.

        Returns:
            A JSON response with the job ID or an actionable creation error.
        """
        try:
            body = await request.json()
            bot_name = body.get("bot_name")
            group_or_user_id = body.get("group_or_user_id")
            time_expr = str(body.get("time_expr") or "").strip()
            remind_message = str(body.get("remind_message") or "").strip()
            if not bot_name or not group_or_user_id:
                return error_response("缺少 bot_name 或 group_or_user_id 参数")
            if not time_expr:
                return error_response("时间规则不能为空")
            if not remind_message:
                return error_response("任务内容不能为空")
            if not hasattr(self.giftia, "task_manager"):
                return error_response("定时任务调度器不可用")

            # Prefer recorded routing; existing reminders support older sessions.
            raw_context = await self.giftia.db.get_kv_data(
                f"task_session:{bot_name}:{group_or_user_id}"
            )
            context = json.loads(raw_context) if raw_context else None
            if not context:
                for job in self.giftia.task_manager.scheduler.get_jobs():
                    kwargs = job.kwargs or {}
                    if (
                        job.args
                        and job.args[0] == "remind"
                        and kwargs.get("bot_name") == bot_name
                        and kwargs.get("group_or_user_id") == group_or_user_id
                    ):
                        context = kwargs
                        break
            routing_fields = (
                "unified_msg_origin",
                "adapter_id",
                "self_id",
                "platform_name",
                "group_id",
            )
            if not context or any(field not in context for field in routing_fields):
                return error_response(
                    "缺少会话路由信息，请先在该会话发送一条消息后重试"
                )

            try:
                run_date = datetime.fromisoformat(time_expr)
            except ValueError:
                pass  # The scheduler validates cron expressions.
            else:
                if run_date <= datetime.now(run_date.tzinfo):
                    return error_response("执行时间必须晚于当前时间")

            task_id = f"{bot_name}_{group_or_user_id}_{uuid.uuid4().hex[:8]}"
            message = self.giftia.task_manager.add_job(
                task_id=task_id,
                func_name="remind",
                time_expr=time_expr,
                kwargs={
                    **{field: context[field] for field in routing_fields},
                    "bot_name": bot_name,
                    "nickname": self.giftia.get_bot_config(bot_name).get(
                        "nickname", bot_name
                    ),
                    "group_or_user_id": group_or_user_id,
                    "user_id": "dashboard" if context["group_id"] else group_or_user_id,
                    "user_name": "WebUI",
                    "remind_message": remind_message,
                },
            )
            if not self.giftia.task_manager.scheduler.get_job(task_id):
                return error_response(message)
            return json_response(
                {
                    "status": "success",
                    "message": message,
                    "data": {"task_id": task_id},
                }
            )
        except Exception as e:
            logger.error(f"[Giftia API] create_scheduled_task error: {e}")
            return error_response(f"创建定时任务失败: {str(e)}")

    async def update_scheduled_task(self):
        """Update scheduled task content and time expression with session ownership verification."""
        try:
            body = await request.json()
            bot_name = body.get("bot_name")
            group_or_user_id = body.get("group_or_user_id")
            task_id = body.get("task_id")
            time_expr = body.get("time_expr")
            remind_message = body.get("remind_message")

            if not bot_name or not group_or_user_id or not task_id:
                return error_response("缺少必要参数")
            if time_expr is None or str(time_expr).strip() == "":
                return error_response("时间规则不能为空")
            if remind_message is None or str(remind_message).strip() == "":
                return error_response("任务内容不能为空")
            if not hasattr(self.giftia, "task_manager"):
                return error_response("定时任务调度器不可用")

            # 会话隔离校验：验证该定时任务是否确实属于指定的 Bot 与会话
            session_jobs = self.giftia.task_manager.get_session_jobs_data(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
            )
            if not any(job["task_id"] == task_id for job in session_jobs):
                return error_response("指定的定时任务不存在或无权修改")

            ok, res_msg = self.giftia.task_manager.update_job(
                task_id=task_id,
                time_expr=str(time_expr).strip(),
                remind_message=str(remind_message).strip(),
            )
            if not ok:
                return error_response(res_msg)
            return json_response({"status": "success", "message": res_msg})
        except Exception as e:
            logger.error(f"[Giftia API] update_scheduled_task error: {e}")
            return error_response(f"更新定时任务失败: {str(e)}")

    async def delete_scheduled_task(self):
        """Delete a single scheduled task for a session with session ownership verification."""
        try:
            body = await request.json()
            bot_name = body.get("bot_name")
            group_or_user_id = body.get("group_or_user_id")
            task_id = body.get("task_id")

            if not bot_name or not group_or_user_id or not task_id:
                return error_response("缺少必要参数")
            if not hasattr(self.giftia, "task_manager"):
                return error_response("定时任务调度器不可用")

            # 会话隔离校验：验证该定时任务是否确实属于指定的 Bot 与会话
            session_jobs = self.giftia.task_manager.get_session_jobs_data(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
            )
            if not any(job["task_id"] == task_id for job in session_jobs):
                return error_response("指定的定时任务不存在或无权删除")

            res_msg = self.giftia.task_manager.remove_job(task_id)
            return json_response({"status": "success", "message": res_msg})
        except Exception as e:
            logger.error(f"[Giftia API] delete_scheduled_task error: {e}")
            return error_response(f"删除定时任务失败: {str(e)}")

    async def clear_scheduled_tasks(self):
        """Clear all scheduled tasks for a session."""
        try:
            body = await request.json()
            bot_name = body.get("bot_name")
            group_or_user_id = body.get("group_or_user_id")

            if not bot_name or not group_or_user_id:
                return error_response("缺少 bot_name 或 group_or_user_id 参数")
            if not hasattr(self.giftia, "task_manager"):
                return error_response("定时任务调度器不可用")

            count = self.giftia.task_manager.remove_session_jobs(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
            )
            return json_response(
                {
                    "status": "success",
                    "message": f"成功清空 {count} 条定时任务",
                    "cleared_count": count,
                }
            )
        except Exception as e:
            logger.error(f"[Giftia API] clear_scheduled_tasks error: {e}")
            return error_response(f"清空定时任务失败: {str(e)}")
