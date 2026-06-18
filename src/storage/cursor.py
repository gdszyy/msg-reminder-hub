"""
游标管理
========
封装对 Cursor 表的操作，提供简洁的接口供 Fetcher 使用。
"""

import logging
from datetime import datetime, timezone
from typing import Optional, Tuple

from .db import get_session, get_cursor, upsert_cursor

logger = logging.getLogger("msg_reminder.cursor")


class CursorManager:
    """游标管理器，封装各平台的增量拉取状态"""

    def __init__(self, platform: str):
        self.platform = platform

    def get_last_position(self, chat_id: str) -> Tuple[str, int]:
        """
        获取指定群组的最后拉取位置。
        返回 (last_message_id, last_timestamp)。
        """
        session = get_session()
        try:
            cursor = get_cursor(session, self.platform, chat_id)
            if cursor:
                return cursor.last_message_id or "", cursor.last_timestamp or 0
            return "", 0
        finally:
            session.close()

    def update_position(self, chat_id: str, message_id: str = "", timestamp: int = 0):
        """更新指定群组的拉取位置"""
        session = get_session()
        try:
            upsert_cursor(session, self.platform, chat_id, message_id, timestamp)
            logger.debug(
                "游标已更新: platform=%s, chat=%s, msg_id=%s, ts=%d",
                self.platform, chat_id, message_id, timestamp,
            )
        finally:
            session.close()
