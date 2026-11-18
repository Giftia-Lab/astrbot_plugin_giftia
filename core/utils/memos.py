import re
import uuid
from dataclasses import asdict
from datetime import datetime

from .schemas import FeatureKey, Memo


class MemoManager:
    """Manage future plans within the owning bot and conversation."""

    def __init__(self, plugin):
        self.plugin = plugin

    def is_enabled(self, bot_name: str) -> bool:
        features = self.plugin.get_bot_config(bot_name).get(
            "enabled_interactive_features"
        )
        return features is None or FeatureKey.MEMO in features

    @property
    def limit(self) -> int:
        try:
            return max(0, int(self.plugin.tools_config.get("task_board_max_active", 3)))
        except (TypeError, ValueError):
            return 3

    @property
    def default_duration(self) -> str:
        try:
            hours = max(
                1,
                int(
                    self.plugin.tools_config.get("task_board_default_expire_hours", 24)
                ),
            )
        except (TypeError, ValueError):
            hours = 24
        return f"{hours}h"

    async def list_active(self, bot_name: str, session_id: str) -> list[Memo]:
        if not self.is_enabled(bot_name):
            return []
        return await self.plugin.db.memos_repo.list_active(bot_name, session_id)

    async def dashboard(self, bot_name: str, session_id: str) -> dict:
        memos = await self.list_active(bot_name, session_id)
        return {
            "enabled": self.is_enabled(bot_name),
            "limit": self.limit,
            "default_duration": self.default_duration,
            "items": [{**asdict(memo), "remaining": memo.remaining} for memo in memos],
        }

    async def apply(
        self,
        bot_name: str,
        session_id: str,
        action: str,
        *,
        content: str = "",
        duration: str = "",
        memo_id: str = "",
    ) -> str:
        """Apply the same scoped mutation for a model or a dashboard request.

        Args:
            bot_name: Owning bot configuration.
            session_id: Private or group conversation.
            action: Add, delete or renew.
            content: Text of a new memo.
            duration: Positive integer followed by min, h or d.
            memo_id: Stable identifier for deletion or renewal.

        Returns:
            Identifier of the affected memo.

        Raises:
            ValueError: The action, duration, memo or capacity is invalid.
        """
        if not self.is_enabled(bot_name):
            raise ValueError("该 Bot 的备忘录未启用")
        if action not in ("add", "delete", "renew"):
            raise ValueError("备忘录支持 add、delete 和 renew")
        repo = self.plugin.db.memos_repo
        if action != "add":
            if not isinstance(memo_id, str) or not memo_id.strip():
                raise ValueError("缺少备忘 ID")
            memo_id = memo_id.strip()
        if action == "delete":
            if not await repo.delete(bot_name, session_id, memo_id):
                raise ValueError("备忘不存在或已到期")
            return memo_id

        if action == "add" and duration in ("", None):
            duration = self.default_duration
        match = re.fullmatch(r"([0-9]+)\s*(min|m|h|d)", str(duration).strip().lower())
        if not match:
            raise ValueError("留存时间请使用正整数和单位，例如 30min、2h 或 3d")
        seconds = int(match[1]) * {"min": 60, "m": 60, "h": 3600, "d": 86400}[match[2]]
        if not 0 < seconds < (datetime.max - datetime.now()).total_seconds():
            raise ValueError("留存时间超出有效范围")
        if action == "renew":
            if not await repo.renew(bot_name, session_id, memo_id, seconds):
                raise ValueError("备忘不存在、已到期或续期超出有效范围")
            return memo_id

        if not isinstance(content, str) or not content.strip():
            raise ValueError("备忘内容不能为空")
        memo_id = f"memo_{uuid.uuid4().hex[:12]}"
        if not await repo.add(
            bot_name, session_id, memo_id, content.strip(), seconds, self.limit
        ):
            raise ValueError(f"当前会话的备忘已达上限（{self.limit} 条）")
        return memo_id
