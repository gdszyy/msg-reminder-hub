"""
Lark (飞书) 消息拉取器
======================
从飞书群聊和私聊中增量拉取消息。

能力来源：ai-secretary-architecture 仓库中的：
  - daily_progress_updater.py (增量拉取、游标机制)
  - lark_sdk_client.py (SDK 封装)
  - cold_start_step2_fetch_messages.py (消息拉取)

核心能力：
  1. 获取并缓存 tenant_access_token
  2. 列出机器人加入的所有群组
  3. 增量拉取群消息（基于时间戳游标）
  4. 解析消息内容（text / post / rich text）
  5. 转换为标准 RawMessage 格式
"""

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests

from config import settings
from src.ingestion.base import BaseFetcher, RawMessage

logger = logging.getLogger("msg_reminder.lark_fetcher")

# API 调用间隔（秒），避免触发限流
API_INTERVAL = 0.3


class LarkFetcher(BaseFetcher):
    """飞书消息拉取器"""

    def __init__(self):
        self._token_cache: Dict = {"token": "", "expires_at": 0}
        self._bot_open_id: str = ""

    @property
    def platform_name(self) -> str:
        return "lark"

    def is_configured(self) -> bool:
        return bool(settings.LARK_APP_ID and settings.LARK_APP_SECRET)

    # -----------------------------------------------------------------------
    # 认证
    # -----------------------------------------------------------------------

    def authenticate(self) -> bool:
        """获取 tenant_access_token"""
        token = self._get_token()
        if token:
            logger.info("Lark 认证成功")
            return True
        logger.error("Lark 认证失败")
        return False

    def _get_token(self) -> str:
        """获取并缓存 tenant_access_token"""
        now = time.time()
        if self._token_cache["token"] and now < self._token_cache["expires_at"]:
            return self._token_cache["token"]

        url = f"{settings.LARK_BASE_URL}/open-apis/auth/v3/tenant_access_token/internal"
        try:
            resp = requests.post(url, json={
                "app_id": settings.LARK_APP_ID,
                "app_secret": settings.LARK_APP_SECRET,
            }, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") == 0 or "tenant_access_token" in data:
                token = data["tenant_access_token"]
                expires_in = data.get("expire", 7200) - 300
                self._token_cache["token"] = token
                self._token_cache["expires_at"] = now + expires_in
                return token
            else:
                logger.error("获取 Lark token 失败: %s", data.get("msg"))
                return ""
        except Exception as e:
            logger.error("获取 Lark token 异常: %s", e)
            return ""

    def _get_headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._get_token()}"}

    def _lark_get(self, url: str, params: dict = None) -> dict:
        """统一的 GET 请求封装"""
        resp = requests.get(url, headers=self._get_headers(), params=params or {}, timeout=15)
        resp.raise_for_status()
        return resp.json()

    # -----------------------------------------------------------------------
    # 群组列表
    # -----------------------------------------------------------------------

    def list_chats(self) -> List[dict]:
        """列出机器人加入的所有群组"""
        url = f"{settings.LARK_BASE_URL}/open-apis/im/v1/chats"
        chats = []
        page_token = ""

        while True:
            params = {"page_size": 100}
            if page_token:
                params["page_token"] = page_token

            try:
                data = self._lark_get(url, params)
                if data.get("code") != 0:
                    logger.warning("列出群组失败: %s", data.get("msg"))
                    break

                items = data.get("data", {}).get("items", [])
                for item in items:
                    chats.append({
                        "chat_id": item.get("chat_id", ""),
                        "name": item.get("name", ""),
                        "member_count": item.get("user_count", 0),
                        "chat_type": item.get("chat_type", ""),
                    })

                if not data.get("data", {}).get("has_more"):
                    break
                page_token = data.get("data", {}).get("page_token", "")
                time.sleep(API_INTERVAL)
            except Exception as e:
                logger.error("列出群组异常: %s", e)
                break

        logger.info("获取到 %d 个群组", len(chats))

        # 如果配置了监控列表，则过滤
        if settings.LARK_MONITORED_CHATS:
            chats = [c for c in chats if c["chat_id"] in settings.LARK_MONITORED_CHATS]
            logger.info("过滤后保留 %d 个监控群组", len(chats))

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
        增量拉取指定群组的消息。

        使用时间戳范围查询，从 since_timestamp 到当前时间。
        返回标准化的 RawMessage 列表（按时间正序）。
        """
        url = f"{settings.LARK_BASE_URL}/open-apis/im/v1/messages"
        messages = []
        page_token = None

        # 如果没有起始时间戳，默认拉取最近 1 小时
        if not since_timestamp:
            since_timestamp = int(time.time()) - 3600

        end_timestamp = int(time.time())

        while len(messages) < limit:
            params = {
                "container_id_type": "chat",
                "container_id": chat_id,
                "start_time": str(since_timestamp),
                "end_time": str(end_timestamp),
                "page_size": min(50, limit - len(messages)),
                "sort_type": "ByCreateTimeAsc",
            }
            if page_token:
                params["page_token"] = page_token

            try:
                data = self._lark_get(url, params)
                code = data.get("code", -1)

                if code == 230050:
                    logger.warning("群 %s 历史消息权限受限", chat_id)
                    break
                if code != 0:
                    logger.warning("拉取消息失败: chat=%s, code=%s, msg=%s",
                                   chat_id, code, data.get("msg"))
                    break

                items = data.get("data", {}).get("items", [])
                for item in items:
                    raw_msg = self._parse_message(item, chat_id)
                    if raw_msg:
                        messages.append(raw_msg)

                if not data.get("data", {}).get("has_more"):
                    break
                page_token = data.get("data", {}).get("page_token")
                time.sleep(API_INTERVAL)

            except Exception as e:
                logger.error("拉取消息异常: chat=%s, err=%s", chat_id, e)
                break

        logger.info("拉取完成: chat=%s, 消息数=%d", chat_id, len(messages))
        return messages

    # -----------------------------------------------------------------------
    # 消息解析
    # -----------------------------------------------------------------------

    def _parse_message(self, item: dict, chat_id: str) -> Optional[RawMessage]:
        """将飞书原始消息解析为 RawMessage"""
        msg_type = item.get("msg_type", "")
        message_id = item.get("message_id", "")
        sender = item.get("sender", {})
        sender_id = sender.get("id", "")
        create_time = item.get("create_time", "")

        # 提取文本内容
        body_str = item.get("body", {}).get("content", "")
        content = self._extract_text(body_str, msg_type)

        # 跳过空内容（图片、文件等非文本消息）
        if not content or not content.strip():
            return None

        # 解析时间戳
        try:
            ts = int(create_time)
            created_at = datetime.fromtimestamp(ts, tz=timezone.utc)
        except (ValueError, TypeError):
            created_at = datetime.now(timezone.utc)

        # 提取 reply_to
        reply_to = ""
        parent_id = item.get("parent_id", "")
        if parent_id:
            reply_to = parent_id

        return RawMessage(
            platform="lark",
            platform_msg_id=message_id,
            chat_id=chat_id,
            sender_id=sender_id,
            sender_name=sender.get("sender_type", ""),  # 后续可通过 API 查询真实姓名
            content=content,
            msg_type=msg_type,
            reply_to_id=reply_to,
            created_at=created_at,
            raw_data=json.dumps(item, ensure_ascii=False)[:1000],
        )

    def _extract_text(self, body_str: str, msg_type: str) -> str:
        """从飞书消息 body 中提取纯文本"""
        if not body_str:
            return ""

        try:
            body = json.loads(body_str)
        except (json.JSONDecodeError, TypeError):
            return body_str[:500] if body_str else ""

        if msg_type == "text":
            text = body.get("text", "")
            # 清理 @mention 标签
            text = re.sub(r"<at[^>]*>[^<]*</at>", "@someone", text)
            return text.strip()

        elif msg_type == "post":
            # 富文本消息
            texts = []
            title = body.get("title", "")
            if title:
                texts.append(title)
            for line in body.get("content", []):
                if not isinstance(line, list):
                    continue
                for node in line:
                    if not isinstance(node, dict):
                        continue
                    tag = node.get("tag", "")
                    if tag == "text":
                        texts.append(node.get("text", ""))
                    elif tag == "at":
                        texts.append(f"@{node.get('user_name', '')}")
                    elif tag == "a":
                        link_text = node.get("text", "")
                        link_href = node.get("href", "")
                        if link_text and link_href:
                            texts.append(f"{link_text}({link_href})")
                        elif link_href:
                            texts.append(link_href)
                        elif link_text:
                            texts.append(link_text)
            return " ".join(texts).strip()

        elif msg_type in ("image", "file", "audio", "video", "sticker", "media"):
            return ""  # 非文本消息返回空

        else:
            return ""

    # -----------------------------------------------------------------------
    # 辅助方法
    # -----------------------------------------------------------------------

    def get_bot_open_id(self) -> str:
        """获取机器人自身的 open_id"""
        if self._bot_open_id:
            return self._bot_open_id

        url = f"{settings.LARK_BASE_URL}/open-apis/bot/v3/info"
        try:
            data = self._lark_get(url)
            self._bot_open_id = data.get("bot", {}).get("open_id", "")
        except Exception as e:
            logger.warning("获取机器人 open_id 失败: %s", e)
        return self._bot_open_id

    def get_user_name(self, open_id: str) -> str:
        """通过 contact API 获取用户姓名"""
        if not open_id:
            return ""
        url = f"{settings.LARK_BASE_URL}/open-apis/contact/v3/users/{open_id}"
        try:
            data = self._lark_get(url, {"user_id_type": "open_id"})
            if data.get("code") == 0:
                return data.get("data", {}).get("user", {}).get("name", "")
        except Exception:
            pass
        return ""
