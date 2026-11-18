from ...utils.schemas import Memo
from ..operations import serialized_operation
from .base import BaseRepository


class MemosRepository(BaseRepository):
    """Use the existing task rows so live memos retain their IDs and deadlines."""

    @serialized_operation
    async def list_active(self, bot_name: str, session_id: str) -> list[Memo]:
        async with self.conn.execute(
            """
            SELECT task_id AS memo_id, content, COALESCE(created_at, '') AS created_at,
                   strftime('%s', expires_at, 'utc') - strftime('%s', 'now') AS remaining_seconds
            FROM short_tasks
            WHERE bot_name=? AND group_or_user_id=? AND status='active'
              AND expires_at > datetime('now', 'localtime')
            ORDER BY created_at, task_id
            """,
            (bot_name, session_id),
        ) as cursor:
            return [Memo(**dict(row)) for row in await cursor.fetchall()]

    @serialized_operation
    async def add(
        self,
        bot_name: str,
        session_id: str,
        memo_id: str,
        content: str,
        seconds: int,
        limit: int,
    ) -> bool:
        # Reclaim expired rows on the next write to avoid writes on every reply.
        await self.conn.execute(
            """DELETE FROM short_tasks WHERE bot_name=? AND group_or_user_id=?
               AND status='active' AND expires_at <= datetime('now', 'localtime')""",
            (bot_name, session_id),
        )
        # Count and insert in one statement, including when a memo just expired.
        cursor = await self.conn.execute(
            """
            INSERT INTO short_tasks
                (task_id, bot_name, group_or_user_id, content, created_at, updated_at, expires_at)
            SELECT ?, ?, ?, ?, datetime('now', 'localtime'), datetime('now', 'localtime'),
                   datetime('now', ?, 'localtime')
            WHERE (SELECT COUNT(*) FROM short_tasks
                   WHERE bot_name=? AND group_or_user_id=? AND status='active'
                   AND expires_at > datetime('now', 'localtime')) < ?
            """,
            (
                memo_id,
                bot_name,
                session_id,
                content,
                f"+{seconds} seconds",
                bot_name,
                session_id,
                limit,
            ),
        )
        await self.conn.commit()
        return cursor.rowcount > 0

    @serialized_operation
    async def renew(
        self, bot_name: str, session_id: str, memo_id: str, seconds: int
    ) -> bool:
        # Concurrent extensions add to the stored deadline within the UPDATE.
        extension = f"+{seconds} seconds"
        cursor = await self.conn.execute(
            """
            UPDATE short_tasks
            SET expires_at=datetime(expires_at, 'utc', ?, 'localtime'), updated_at=datetime('now','localtime')
            WHERE bot_name=? AND group_or_user_id=? AND task_id=? AND status='active'
              AND expires_at > datetime('now','localtime')
              AND datetime(expires_at, 'utc', ?, 'localtime') IS NOT NULL
            """,
            (extension, bot_name, session_id, memo_id, extension),
        )
        await self.conn.commit()
        return cursor.rowcount > 0

    @serialized_operation
    async def delete(self, bot_name: str, session_id: str, memo_id: str) -> bool:
        cursor = await self.conn.execute(
            """DELETE FROM short_tasks WHERE bot_name=? AND group_or_user_id=? AND task_id=?
               AND status='active' AND expires_at > datetime('now','localtime')""",
            (bot_name, session_id, memo_id),
        )
        await self.conn.commit()
        return cursor.rowcount > 0
