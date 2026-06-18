"""
Telegram 消息拉取器（User API / MTProto）
==========================================
通过 Telethon 使用你自己的手机号登录"用户客户端"，
能读取你账号的全部会话历史和新消息。

为什么不用 Bot API：
  Bot API 只能收到别人发给 bot 的消息，看不到你的私聊和群聊全量消息。
  User API（MTProto）是 Telegram 官方支持的协议，用你自己的手机号登录，
  能读到你全部会话历史和新消息。

核心能力：
  1. 通过 api_id + api_hash + 手机号登录（首次需要验证码）
  2. 列出所有对话（私聊、群组、频道）
  3. 增量拉取指定对话的消息（基于 min_id 或 offset_date）
  4. 监听实时新消息（NewMessage 事件）
  5. 转换为标准 RawMessage 格式

依赖：
  pip install telethon

环境变量：
  TG_API_ID        - 从 https://my.telegram.org 获取
  TG_API_HASH      - 从 https://my.telegram.org 获取
  TG_PHONE         - 你的手机号（如 +8613800138000）
  TG_SESSION_NAME  - Session 文件名（默认 tg_user_session）
  TG_MONITORED_CHATS - 监控的对话ID列表（逗号分隔，留空则监控所有）

首次登录：
  首次运行时 Telethon 会要求输入验证码（发到你的 Telegram 客户端），
  之后会保存 session 文件，后续启动无需再次验证。
  可通过 `python scripts/tg_login.py` 单独完成登录流程。
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

from config import settings
from src.ingestion.base import BaseFetcher, RawMessage

logger = logging.getLogger("msg_reminder.tg_fetcher")

# Telethon 延迟导入（可能未安装）
try:
    from telethon import TelegramClient, events
    from telethon.tl.types import (
        User, Chat, Channel,
        PeerUser, PeerChat, PeerChannel,
        Message as TLMessage,
    )
    TELETHON_AVAILABLE = True
except ImportError:
    TELETHON_AVAILABLE = False
    logger.warning("Telethon 未安装，Telegram User API 不可用。请运行: pip install telethon")


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

TG_API_ID = os.environ.get("TG_API_ID", "")
TG_API_HASH = os.environ.get("TG_API_HASH", "")
TG_PHONE = os.environ.get("TG_PHONE", "")
TG_SESSION_NAME = os.environ.get("TG_SESSION_NAME", "tg_user_session")
TG_SESSION_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "data")


class TelegramFetcher(BaseFetcher):
    """
    Telegram 消息拉取器（基于 Telethon User API / MTProto）。
    使用你自己的手机号登录，能读取所有会话。
    """

    def __init__(self):
        self._client: Optional["TelegramClient"] = None
        self._me = None  # 当前登录用户信息
        self._loop = None

    @property
    def platform_name(self) -> str:
        return "telegram"

    def is_configured(self) -> bool:
        """检查是否配置了 Telegram User API 凭证"""
        return bool(TG_API_ID and TG_API_HASH and TG_PHONE and TELETHON_AVAILABLE)

    # -----------------------------------------------------------------------
    # 认证
    # -----------------------------------------------------------------------

    def authenticate(self) -> bool:
        """
        登录 Telegram User API。
        首次登录需要输入验证码（交互式），之后 session 文件会保存登录状态。
        """
        if not TELETHON_AVAILABLE:
            logger.error("Telethon 未安装")
            return False

        if not TG_API_ID or not TG_API_HASH or not TG_PHONE:
            logger.error("TG_API_ID / TG_API_HASH / TG_PHONE 未配置")
            return False

        try:
            # 确保 session 目录存在
            os.makedirs(TG_SESSION_DIR, exist_ok=True)
            session_path = os.path.join(TG_SESSION_DIR, TG_SESSION_NAME)

            self._client = TelegramClient(
                session_path,
                int(TG_API_ID),
                TG_API_HASH,
            )

            # 同步方式启动（在非异步环境中）
            self._run_sync(self._async_authenticate())
            return True

        except Exception as e:
            logger.error("Telegram 认证失败: %s", e)
            return False

    async def _async_authenticate(self):
        """异步认证流程"""
        await self._client.connect()

        if not await self._client.is_user_authorized():
            logger.info("Telegram 需要登录验证，发送验证码到 %s...", TG_PHONE)
            await self._client.send_code_request(TG_PHONE)
            # 注意：在服务端自动化场景中，验证码需要通过其他方式获取
            # 建议先通过 scripts/tg_login.py 完成首次登录
            logger.warning(
                "首次登录需要验证码！请先运行 `python scripts/tg_login.py` 完成登录。"
            )
            return False

        self._me = await self._client.get_me()
        logger.info(
            "Telegram 认证成功: @%s (%s %s), user_id=%d",
            self._me.username or "",
            self._me.first_name or "",
            self._me.last_name or "",
            self._me.id,
        )
        return True

    # -----------------------------------------------------------------------
    # 群组/对话列表
    # -----------------------------------------------------------------------

    def list_chats(self) -> List[dict]:
        """
        列出所有对话（私聊、群组、频道）。
        如果配置了 TG_MONITORED_CHATS，则只返回指定的对话。
        """
        return self._run_sync(self._async_list_chats())

    async def _async_list_chats(self) -> List[dict]:
        """异步获取对话列表"""
        if not self._client or not self._client.is_connected():
            return []

        chats = []
        async for dialog in self._client.iter_dialogs(limit=200):
            entity = dialog.entity
            chat_id = str(dialog.id)
            chat_type = "unknown"
            name = dialog.name or ""

            if isinstance(entity, User):
                chat_type = "private"
                name = f"{entity.first_name or ''} {entity.last_name or ''}".strip()
            elif isinstance(entity, Chat):
                chat_type = "group"
            elif isinstance(entity, Channel):
                chat_type = "supergroup" if entity.megagroup else "channel"

            chats.append({
                "chat_id": chat_id,
                "name": name,
                "chat_type": chat_type,
                "member_count": getattr(entity, "participants_count", 0) or 0,
                "unread_count": dialog.unread_count,
            })

        # 如果配置了监控列表，则过滤
        monitored = settings.TG_MONITORED_CHATS
        if monitored:
            chats = [c for c in chats if c["chat_id"] in monitored]
            logger.info("Telegram: 过滤后保留 %d 个监控对话", len(chats))
        else:
            logger.info("Telegram: 获取到 %d 个对话", len(chats))

        return chats

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
        增量拉取指定对话的消息。

        参数:
          chat_id: 对话ID
          since_timestamp: 从此时间戳之后开始拉取（秒）
          since_message_id: 从此消息ID之后开始拉取（min_id）
          limit: 最大拉取条数

        返回: 标准化消息列表（按时间正序）
        """
        return self._run_sync(
            self._async_fetch_messages(chat_id, since_timestamp, since_message_id, limit)
        )

    async def _async_fetch_messages(
        self,
        chat_id: str,
        since_timestamp: int = 0,
        since_message_id: str = "",
        limit: int = 100,
    ) -> List[RawMessage]:
        """异步拉取消息"""
        if not self._client or not self._client.is_connected():
            return []

        messages = []

        try:
            # 构建拉取参数
            kwargs = {"limit": limit}

            # 基于消息ID增量拉取
            if since_message_id:
                try:
                    kwargs["min_id"] = int(since_message_id)
                except (ValueError, TypeError):
                    pass

            # 基于时间戳拉取
            if since_timestamp and not since_message_id:
                kwargs["offset_date"] = datetime.fromtimestamp(
                    since_timestamp, tz=timezone.utc
                )

            # 获取实体（支持 int 和 str 格式的 chat_id）
            try:
                entity = await self._client.get_entity(int(chat_id))
            except (ValueError, TypeError):
                entity = await self._client.get_entity(chat_id)

            # 拉取消息
            async for msg in self._client.iter_messages(entity, **kwargs):
                raw_msg = self._parse_message(msg, chat_id)
                if raw_msg:
                    messages.append(raw_msg)

        except Exception as e:
            logger.error("Telegram 拉取消息异常: chat=%s, err=%s", chat_id, e)

        # 按时间正序排列
        messages.reverse()
        logger.info("Telegram 拉取完成: chat=%s, 消息数=%d", chat_id, len(messages))
        return messages

    # -----------------------------------------------------------------------
    # 实时监听（可选，用于 Webhook 模式）
    # -----------------------------------------------------------------------

    async def start_listening(self, callback):
        """
        启动实时消息监听。
        callback: async def callback(raw_message: RawMessage)
        """
        if not self._client:
            logger.error("客户端未初始化，无法启动监听")
            return

        @self._client.on(events.NewMessage)
        async def handler(event):
            msg = event.message
            chat_id = str(event.chat_id)

            # 如果配置了监控列表，过滤
            if settings.TG_MONITORED_CHATS and chat_id not in settings.TG_MONITORED_CHATS:
                return

            raw_msg = self._parse_message(msg, chat_id)
            if raw_msg:
                await callback(raw_msg)

        logger.info("Telegram 实时监听已启动")
        await self._client.run_until_disconnected()

    # -----------------------------------------------------------------------
    # 消息解析
    # -----------------------------------------------------------------------

    def _parse_message(self, msg, chat_id: str) -> Optional[RawMessage]:
        """将 Telethon Message 对象解析为 RawMessage"""
        # 跳过空消息和服务消息
        if not msg or not hasattr(msg, "text"):
            return None

        text = msg.text or msg.message or ""
        if not text or not text.strip():
            # 尝试获取 caption（图片/文件的说明）
            if hasattr(msg, "caption") and msg.caption:
                text = msg.caption
            else:
                return None

        # 提取发送者信息
        sender_id = ""
        sender_name = ""
        if msg.sender:
            sender_id = str(msg.sender_id or "")
            if isinstance(msg.sender, User):
                sender_name = f"{msg.sender.first_name or ''} {msg.sender.last_name or ''}".strip()
                if not sender_name:
                    sender_name = msg.sender.username or str(sender_id)
            elif hasattr(msg.sender, "title"):
                sender_name = msg.sender.title or ""
        elif msg.sender_id:
            sender_id = str(msg.sender_id)

        # 消息ID
        message_id = str(msg.id)

        # 时间
        created_at = msg.date if msg.date else datetime.now(timezone.utc)
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)

        # 回复关系
        reply_to_id = ""
        if msg.reply_to and hasattr(msg.reply_to, "reply_to_msg_id"):
            reply_to_id = str(msg.reply_to.reply_to_msg_id or "")

        return RawMessage(
            platform="telegram",
            platform_msg_id=f"{chat_id}_{message_id}",
            chat_id=chat_id,
            chat_name="",  # 在 list_chats 时已获取，这里不重复查询
            sender_id=sender_id,
            sender_name=sender_name,
            content=text,
            msg_type="text",
            reply_to_id=reply_to_id,
            created_at=created_at,
            raw_data=json.dumps({
                "msg_id": msg.id,
                "sender_id": sender_id,
                "date": str(created_at),
            }, ensure_ascii=False),
        )

    # -----------------------------------------------------------------------
    # 辅助方法
    # -----------------------------------------------------------------------

    def _run_sync(self, coro):
        """在同步环境中运行异步协程"""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 如果已有运行中的 event loop（如在 FastAPI 中），创建新线程
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    future = pool.submit(asyncio.run, coro)
                    return future.result(timeout=60)
            else:
                return loop.run_until_complete(coro)
        except RuntimeError:
            # 没有 event loop，创建新的
            return asyncio.run(coro)

    def disconnect(self):
        """断开连接"""
        if self._client and self._client.is_connected():
            self._run_sync(self._client.disconnect())
            logger.info("Telegram 客户端已断开")

    def get_my_user_id(self) -> str:
        """获取当前登录用户的 ID"""
        if self._me:
            return str(self._me.id)
        return ""
