"""用户配额管理。

基于 sqlite3 实现每用户每日调用次数限制。
异步接口通过 asyncio.to_thread 包装同步数据库操作。

采用"先扣后退"模式防止并发绕过：
    try_acquire() — 原子检查+扣减，返回 (已用次数, 上限, date_key)
    refund()      — 生成失败时按 date_key 回退一次
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path


class QuotaExhausted(Exception):
    """配额不足异常，携带已用次数和上限。"""

    def __init__(self, used: int, limit: int) -> None:
        self.used = used
        self.limit = limit
        super().__init__(f"quota exhausted: {used}/{limit}")


class QuotaManager:
    """每用户每日配额管理器。

    参数:
        db_path: SQLite 数据库文件路径
        daily_limit: 每用户每日调用次数上限（≥1）
        reset_hour: 每日重置时间（0-23，本地时区）
        whitelist: 不受配额限制的用户 ID 集合
    """

    def __init__(
        self,
        db_path: Path,
        daily_limit: int,
        reset_hour: int,
        whitelist: set[str],
    ) -> None:
        self.daily_limit = max(1, int(daily_limit))
        self.reset_hour = min(23, max(0, int(reset_hour)))
        self.whitelist = frozenset(
            uid.strip() for uid in whitelist if uid.strip()
        )

        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._closed = False
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS usage (
                user_id   TEXT NOT NULL,
                date_key  TEXT NOT NULL,
                count     INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, date_key)
            )
            """
        )
        self._conn.commit()

    # ── 异步公开接口 ──

    async def try_acquire(self, user_id: str) -> tuple[int, int, str]:
        """原子检查+扣减配额。

        成功: 返回 (扣减后已用次数, 上限, date_key)。白名单用户上限为 -1。
        失败: 抛出 QuotaExhausted。

        返回的 date_key 应传给 refund() 以确保退到同一个配额周期。
        """
        return await asyncio.to_thread(
            self._try_acquire_sync, user_id.strip()
        )

    async def refund(self, user_id: str, date_key: str) -> None:
        """回退一次配额（生成失败时调用）。

        参数:
            date_key: try_acquire 返回的配额周期标识，确保退到正确周期。
        """
        await asyncio.to_thread(self._refund_sync, user_id.strip(), date_key)

    async def get_usage(self, user_id: str) -> tuple[int, int]:
        """查询用户当前配额使用情况。

        返回: (已用次数, 上限)。白名单用户上限为 -1。
        """
        return await asyncio.to_thread(self._get_usage_sync, user_id.strip())

    def close(self) -> None:
        """关闭数据库连接。"""
        with self._lock:
            if self._closed:
                return
            self._conn.close()
            self._closed = True

    # ── 内部同步实现 ──

    def _ensure_open(self) -> None:
        """在 _lock 内调用，检查连接是否已关闭。"""
        if self._closed:
            raise RuntimeError("QuotaManager already closed")

    def _date_key(self) -> str:
        """计算当前配额周期的日期键。

        若当前小时 < reset_hour，视为前一天的配额周期。
        """
        now = datetime.now()
        if now.hour < self.reset_hour:
            now -= timedelta(days=1)
        return now.strftime("%Y-%m-%d")

    def _get_count_locked(self, user_id: str, date_key: str) -> int:
        """读取指定用户和日期的已用次数（需在 _lock 内调用）。"""
        cursor = self._conn.execute(
            "SELECT count FROM usage WHERE user_id = ? AND date_key = ?",
            (user_id, date_key),
        )
        row = cursor.fetchone()
        return int(row[0]) if row else 0

    def _try_acquire_sync(self, user_id: str) -> tuple[int, int, str]:
        """原子检查+扣减，整个过程在同一把锁内完成。"""
        date_key = self._date_key()
        with self._lock:
            self._ensure_open()
            current = self._get_count_locked(user_id, date_key)

            # 白名单用户：记录但不限制
            is_whitelisted = user_id in self.whitelist
            if not is_whitelisted and current >= self.daily_limit:
                raise QuotaExhausted(used=current, limit=self.daily_limit)

            # 扣减
            self._conn.execute(
                """
                INSERT INTO usage (user_id, date_key, count)
                VALUES (?, ?, 1)
                ON CONFLICT (user_id, date_key)
                DO UPDATE SET count = count + 1
                """,
                (user_id, date_key),
            )
            self._conn.commit()
            new_count = current + 1
            return new_count, (-1 if is_whitelisted else self.daily_limit), date_key

    def _refund_sync(self, user_id: str, date_key: str) -> None:
        """回退一次配额，精确退到指定周期，count 不低于 0。"""
        with self._lock:
            self._ensure_open()
            self._conn.execute(
                """
                UPDATE usage SET count = MAX(count - 1, 0)
                WHERE user_id = ? AND date_key = ?
                """,
                (user_id, date_key),
            )
            self._conn.commit()

    def _get_usage_sync(self, user_id: str) -> tuple[int, int]:
        date_key = self._date_key()
        with self._lock:
            self._ensure_open()
            used = self._get_count_locked(user_id, date_key)
        limit = -1 if user_id in self.whitelist else self.daily_limit
        return used, limit
