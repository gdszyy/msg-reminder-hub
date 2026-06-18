"""
Telegram 消息拉取器
===================
通过 Telegram Bot API 拉取群组和私聊消息。

实现方式：
  使用 Bot API 的 getUpdates 方法（长轮询），获取机器人可见的消息。
  注意：Bot 必须在群组中且关闭了 Privacy Mode 才能读取所有消息。

核心能力：
  1. 通过 Bot Token 认证
  2. 使用 getUpdates 增量拉取消息（基于 offset）
  3. 获取群组信息
  4. 转换为标准 RawMessage 格式
"""

import json
import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests

from config import settings
from src.ingestion.base import BaseFetcher, RawMessage

logger = logging.getLogger("msg_reminder.tg_fetcher")


class TelegramFetcher(BaseFetcher):
    """Telegram 消息拉取器"""

    def __init__(self):
        self._base_url = f"https://api.telegram.org/bot{settings.TG_BOT_TOKEN}"
        self._last_update_id: int = 0

    @property
    def platform_name(self) -> str:
        return "telegram"

    def is_configured(self) -> bool:
        return bool(settings.TG_BOT_TOKEN)

    # -----------------------------------------------------------------------
    # 认证
    # -----------------------------------------------------------------------

    def authenticate(self) -> bool:
        """验证 Bot Token 是否有效"""
        if not settings.TG_BOT_TOKEN:
            logger.warning("TG_BOT_TOKEN 未配置")
            return False

        try:
            resp = requests.get(f"{self._base_url}/getMe", timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data.get("ok"):
                bot_info = data.get("result", {})
                logger.info(
                    "Telegram 认证成功: @%s (%s)",
                    bot_info.get("username", ""),
                    bot_info.get("first_name", ""),
                )
                return True
            else:
                logger.error("Telegram 认证失败: %s", data.get("description"))
                return False
        except Exception as e:
            logger.error("Telegram 认证异常: %s", e)
            return False

    # -----------------------------------------------------------------------
    # 群组列表
    # -----------------------------------------------------------------------

    def list_chats(self) -> List[dict]:
        """
        列出已配置的监控群组。
        注意：Telegram Bot API 没有"列出所有群组"的接口，
        需要通过配置或从 getUpdates 中发现群组。
        """
        chats = []
        for chat_id in settings.TG_MONITORED_CHATS:
            info = self._get_chat_info(chat_id)
            if info:
                chats.append(info)
        return chats

    def _get_chat_info(self, chat_id: str) -> Optional[dict]:
        """获取群组信息"""
        try:
            resp = requests.get(
                f"{self._base_url}/getChat",
                params={"chat_id": chat_id},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("ok"):
                chat = data["result"]
                return {
                    "chat_id": str(chat.get("id", "")),
                    "name": chat.get("title", "") or chat.get("first_name", ""),
                    "chat_type": chat.get("type", ""),
                    "member_count": 0,  # 需要额外 API 调用
                }
        except Exception as e:
            logger.warning("获取群组信息失败: chat_id=%s, err=%s", chat_id, e)
        return None

    # -----------------------------------------------------------------------
    # 消息拉取
    # -----------------------------------------------------------------------

    def fetch_messages(
        self,
        chat_id: str,
        since_timestamp: int = 0,
        since_message_id: str = "",
        limit: int = 100,
    ) -> List[RawMessage]:
        """
        通过 getUpdates 拉取消息。

        Telegram Bot API 的 getUpdates 返回所有新消息（不区分群组），
        因此这里拉取后按 chat_id 过滤。
        """
        messages = []
        offset = self._last_update_id + 1 if self._last_update_id else 0

        try:
            resp = requests.get(
                f"{self._base_url}/getUpdates",
                params={
                    "offset": offset,
                    "limit": min(limit, 100),
                    "timeout": 1,  # 短超时，不做长轮询
                    "allowed_updates": json.dumps(["message"]),
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

            if not data.get("ok"):
                logger.warning("getUpdates 失败: %s", data.get("description"))
                return []

            updates = data.get("result", [])
            for update in updates:
                update_id = update.get("update_id", 0)
                if update_id > self._last_update_id:
                    self._last_update_id = update_id

                message = update.get("message")
                if not message:
                    continue

                msg_chat_id = str(message.get("chat", {}).get("id", ""))

                # 过滤指定群组
                if chat_id and msg_chat_id != chat_id:
                    continue

                # 时间戳过滤
                msg_date = message.get("date", 0)
                if since_timestamp and msg_date <= since_timestamp:
                    continue

                raw_msg = self._parse_message(message)
                if raw_msg:
                    messages.append(raw_msg)

        except Exception as e:
            logger.error("Telegram 拉取消息异常: %s", e)

        logger.info("Telegram 拉取完成: chat=%s, 消息数=%d", chat_id, len(messages))
        return messages

    def fetch_all_updates(self, limit: int = 100) -> List[RawMessage]:
        """
        拉取所有新消息（不区分群组）。
        适用于定时任务场景：一次拉取所有群的新消息。
        """
        messages = []
        offset = self._last_update_id + 1 if self._last_update_id else 0

        try:
            resp = requests.get(
                f"{self._base_url}/getUpdates",
                params={
                    "offset": offset,
                    "limit": min(limit, 100),
                    "timeout": 1,
                    "allowed_updates": json.dumps(["message"]),
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

            if not data.get("ok"):
                return []

            updates = data.get("result", [])
            for update in updates:
                update_id = update.get("update_id", 0)
                if update_id > self._last_update_id:
                    self._last_update_id = update_id

                message = update.get("message")
                if not message:
                    continue

                # 只处理配置中的监控群组
                msg_chat_id = str(message.get("chat", {}).get("id", ""))
                if settings.TG_MONITORED_CHATS and msg_chat_id not in settings.TG_MONITORED_CHATS:
                    continue

                raw_msg = self._parse_message(message)
                if raw_msg:
                    messages.append(raw_msg)

        except Exception as e:
            logger.error("Telegram 拉取所有更新异常: %s", e)

        return messages

    # -----------------------------------------------------------------------
    # 消息解析
    # -----------------------------------------------------------------------

    def _parse_message(self, message: dict) -> Optional[RawMessage]:
        """将 Telegram 消息解析为 RawMessage"""
        # 提取文本
        text = message.get("text", "")
        if not text:
            # 尝试 caption（图片/文件的说明文字）
            text = message.get("caption", "")
        if not text:
            return None

        # 提取发送者信息
        from_user = message.get("from", {})
        sender_id = str(from_user.get("id", ""))
        sender_name = (
            from_user.get("first_name", "") + " " + from_user.get("last_name", "")
        ).strip() or from_user.get("username", "")

        # 提取群组信息
        chat = message.get("chat", {})
        chat_id = str(chat.get("id", ""))
        chat_name = chat.get("title", "") or chat.get("first_name", "")

        # 提取消息ID和时间
        message_id = str(message.get("message_id", ""))
        msg_date = message.get("date", 0)
        created_at = datetime.fromtimestamp(msg_date, tz=timezone.utc) if msg_date else datetime.now(timezone.utc)

        # 提取 reply_to
        reply_to = ""
        reply_msg = message.get("reply_to_message")
        if reply_msg:
            reply_to = str(reply_msg.get("message_id", ""))

        return RawMessage(
            platform="telegram",
            platform_msg_id=f"{chat_id}_{message_id}",
            chat_id=chat_id,
            chat_name=chat_name,
            sender_id=sender_id,
            sender_name=sender_name,
            content=text,
            msg_type="text",
            reply_to_id=reply_to,
            created_at=created_at,
            raw_data=json.dumps(message, ensure_ascii=False)[:1000],
        )
