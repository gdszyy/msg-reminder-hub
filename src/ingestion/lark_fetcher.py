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
  2. 列出机器人加入的所有群组（含群名缓存）
  3. 增量拉取群消息（基于时间戳游标，分页）
  4. 解析消息内容（text / post / rich text / @mentions）
  5. 用户名解析与缓存（open_id → 姓名）
  6. Webhook 事件去重（message_id 幂等）
  7. 转换为标准 RawMessage 格式
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

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
        # 用户名缓存: open_id → display_name
        self._user_name_cache: Dict[str, str] = {}
        # 群组名缓存: chat_id → chat_name
        self._chat_name_cache: Dict[str, str] = {}
        # Webhook 消息去重集合（最近 1000 条 message_id）
        self._seen_message_ids: Set[str] = set()
        self._seen_message_ids_list: List[str] = []

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
            # 顺便获取机器人自身 open_id
            self._fetch_bot_info()
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

    def _fetch_bot_info(self):
        """获取机器人自身信息"""
        try:
            url = f"{settings.LARK_BASE_URL}/open-apis/bot/v3/info"
            data = self._lark_get(url)
            self._bot_open_id = data.get("bot", {}).get("open_id", "")
            if self._bot_open_id:
                logger.info("机器人 open_id: %s", self._bot_open_id)
        except Exception as e:
            logger.warning("获取机器人信息失败: %s", e)

    # -----------------------------------------------------------------------
    # 用户名解析（带缓存）
    # -----------------------------------------------------------------------

    def resolve_user_name(self, open_id: str) -> str:
        """
        通过 open_id 获取用户显示名称（带内存缓存）。
        飞书 API: GET /open-apis/contact/v3/users/:user_id
        """
        if not open_id:
            return ""

        # 缓存命中
        if open_id in self._user_name_cache:
            return self._user_name_cache[open_id]

        # 机器人自身
        if open_id == self._bot_open_id:
            self._user_name_cache[open_id] = "[Bot]"
            return "[Bot]"

        url = f"{settings.LARK_BASE_URL}/open-apis/contact/v3/users/{open_id}"
        try:
            data = self._lark_get(url, {"user_id_type": "open_id"})
            if data.get("code") == 0:
                name = data.get("data", {}).get("user", {}).get("name", "")
                self._user_name_cache[open_id] = name or open_id[-6:]
                return self._user_name_cache[open_id]
        except Exception as e:
            logger.debug("获取用户名失败 (open_id=%s): %s", open_id, e)

        # 回退：用 open_id 后6位
        fallback = open_id[-6:] if len(open_id) > 6 else open_id
        self._user_name_cache[open_id] = fallback
        return fallback

    def batch_resolve_user_names(self, open_ids: List[str]) -> Dict[str, str]:
        """
        批量解析用户名（飞书支持批量接口）。
        POST /open-apis/contact/v3/users/batch
        """
        # 过滤已缓存的
        to_resolve = [uid for uid in open_ids if uid and uid not in self._user_name_cache]
        if not to_resolve:
            return {uid: self._user_name_cache.get(uid, uid[-6:]) for uid in open_ids}

        # 批量查询（每次最多 50 个）
        url = f"{settings.LARK_BASE_URL}/open-apis/contact/v3/users/batch"
        for i in range(0, len(to_resolve), 50):
            batch = to_resolve[i:i+50]
            params = {"user_ids": batch, "user_id_type": "open_id"}
            try:
                # 批量接口用 GET + 多个 user_ids 参数
                query_str = "&".join(f"user_ids={uid}" for uid in batch)
                full_url = f"{url}?user_id_type=open_id&{query_str}"
                data = self._lark_get(full_url)
                if data.get("code") == 0:
                    items = data.get("data", {}).get("items", [])
                    for item in items:
                        uid = item.get("user_id", "")
                        name = item.get("name", "")
                        if uid:
                            self._user_name_cache[uid] = name or uid[-6:]
            except Exception as e:
                logger.debug("批量获取用户名失败: %s", e)
            time.sleep(API_INTERVAL)

        # 未查到的用回退值
        for uid in to_resolve:
            if uid not in self._user_name_cache:
                self._user_name_cache[uid] = uid[-6:] if len(uid) > 6 else uid

        return {uid: self._user_name_cache.get(uid, uid[-6:]) for uid in open_ids}

    # -----------------------------------------------------------------------
    # 群组列表（带名称缓存）
    # -----------------------------------------------------------------------

    def list_chats(self) -> List[dict]:
        """列出机器人加入的所有群组，并缓存群名"""
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
                    chat_id = item.get("chat_id", "")
                    name = item.get("name", "")
                    # 缓存群名
                    if chat_id and name:
                        self._chat_name_cache[chat_id] = name

                    chats.append({
                        "chat_id": chat_id,
                        "name": name,
                        "member_count": item.get("user_count", 0),
                        "chat_type": item.get("chat_type", ""),
                        "owner_id": item.get("owner_id", ""),
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

    def get_chat_name(self, chat_id: str) -> str:
        """获取群组名称（优先缓存）"""
        if chat_id in self._chat_name_cache:
            return self._chat_name_cache[chat_id]

        # 单独查询
        url = f"{settings.LARK_BASE_URL}/open-apis/im/v1/chats/{chat_id}"
        try:
            data = self._lark_get(url)
            if data.get("code") == 0:
                name = data.get("data", {}).get("name", "")
                self._chat_name_cache[chat_id] = name
                return name
        except Exception:
            pass
        return ""

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

        # 如果没有起始时间戳（首次拉取），默认拉取最近 24 小时
        if not since_timestamp:
            default_hours = int(os.environ.get("FETCH_INITIAL_HOURS", "24"))
            since_timestamp = int(time.time()) - (default_hours * 3600)

        end_timestamp = int(time.time())

        # 获取群名
        chat_name = self.get_chat_name(chat_id)

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
                    logger.warning("群 %s (%s) 历史消息权限受限", chat_id, chat_name)
                    break
                if code != 0:
                    logger.warning("拉取消息失败: chat=%s, code=%s, msg=%s",
                                   chat_id, code, data.get("msg"))
                    break

                items = data.get("data", {}).get("items", [])
                for item in items:
                    raw_msg = self._parse_message(item, chat_id, chat_name)
                    if raw_msg:
                        messages.append(raw_msg)

                if not data.get("data", {}).get("has_more"):
                    break
                page_token = data.get("data", {}).get("page_token")
                time.sleep(API_INTERVAL)

            except Exception as e:
                logger.error("拉取消息异常: chat=%s, err=%s", chat_id, e)
                break

        # 批量解析用户名
        if messages:
            sender_ids = list(set(m.sender_id for m in messages if m.sender_id))
            name_map = self.batch_resolve_user_names(sender_ids)
            for msg in messages:
                if msg.sender_id and msg.sender_id in name_map:
                    msg.sender_name = name_map[msg.sender_id]

        logger.info("拉取完成: chat=%s (%s), 消息数=%d", chat_id, chat_name, len(messages))
        return messages

    # -----------------------------------------------------------------------
    # Webhook 消息处理（实时场景）
    # -----------------------------------------------------------------------

    def is_duplicate_message(self, message_id: str) -> bool:
        """
        检查消息是否已处理过（幂等去重）。
        飞书 Webhook 可能重复推送同一条消息。
        """
        if message_id in self._seen_message_ids:
            return True

        self._seen_message_ids.add(message_id)
        self._seen_message_ids_list.append(message_id)

        # 保持集合大小在 1000 以内
        if len(self._seen_message_ids_list) > 1000:
            old_id = self._seen_message_ids_list.pop(0)
            self._seen_message_ids.discard(old_id)

        return False

    def parse_webhook_event(self, payload: dict) -> Optional[RawMessage]:
        """
        解析飞书 Webhook 事件为 RawMessage。
        用于实时接收场景。
        """
        event = payload.get("event", {})
        message = event.get("message", {})
        sender = event.get("sender", {})

        message_id = message.get("message_id", "")
        if not message_id:
            return None

        # 去重
        if self.is_duplicate_message(message_id):
            logger.debug("重复消息，跳过: %s", message_id)
            return None

        # 提取内容
        content_str = message.get("content", "{}")
        msg_type = message.get("msg_type", "text")
        content = self._extract_text(content_str, msg_type)
        if not content:
            return None

        # 发送者
        sender_id = sender.get("sender_id", {}).get("open_id", "")
        sender_name = self.resolve_user_name(sender_id)

        # 跳过机器人自身的消息
        if sender_id == self._bot_open_id:
            return None

        # 群组
        chat_id = message.get("chat_id", "")
        chat_type = message.get("chat_type", "")
        chat_name = self.get_chat_name(chat_id)

        # @提及信息
        mentions = message.get("mentions", [])
        mention_names = []
        for m in mentions:
            mention_name = m.get("name", "")
            mention_id = m.get("id", {}).get("open_id", "")
            if mention_name:
                mention_names.append(mention_name)
            # 检查是否 @了目标用户
            if mention_id == settings.LARK_TARGET_USER_ID:
                mention_names.append("[TARGET_USER]")

        # 在内容中标注 @信息
        if mention_names:
            content = f"[提及: {', '.join(mention_names)}] {content}"

        # 时间
        create_time = message.get("create_time", "")
        try:
            created_at = datetime.fromtimestamp(int(create_time), tz=timezone.utc)
        except (ValueError, TypeError):
            created_at = datetime.now(timezone.utc)

        return RawMessage(
            platform="lark",
            platform_msg_id=message_id,
            chat_id=chat_id,
            chat_name=chat_name,
            sender_id=sender_id,
            sender_name=sender_name,
            content=content,
            msg_type=msg_type,
            reply_to_id=message.get("parent_id", ""),
            created_at=created_at,
            raw_data=json.dumps({
                "chat_type": chat_type,
                "mentions": mentions,
            }, ensure_ascii=False),
        )

    def is_target_user_mentioned(self, payload: dict) -> bool:
        """
        快速判断 Webhook 事件中是否 @了目标用户。
        用于决定是否需要立即触发 LLM 分析。
        """
        event = payload.get("event", {})
        message = event.get("message", {})
        mentions = message.get("mentions", [])

        for m in mentions:
            mention_id = m.get("id", {}).get("open_id", "")
            if mention_id == settings.LARK_TARGET_USER_ID:
                return True

        # 检查消息内容中是否包含目标用户名
        content_str = message.get("content", "")
        if settings.LARK_TARGET_USER_ID and settings.LARK_TARGET_USER_ID in content_str:
            return True

        return False

    # -----------------------------------------------------------------------
    # 消息解析
    # -----------------------------------------------------------------------

    def _parse_message(self, item: dict, chat_id: str, chat_name: str = "") -> Optional[RawMessage]:
        """将飞书原始消息解析为 RawMessage"""
        msg_type = item.get("msg_type", "")
        message_id = item.get("message_id", "")
        sender = item.get("sender", {})
        sender_id = sender.get("id", "")
        create_time = item.get("create_time", "")

        # 跳过机器人自身的消息
        if sender_id == self._bot_open_id:
            return None

        # 提取文本内容
        body_str = item.get("body", {}).get("content", "")
        content = self._extract_text(body_str, msg_type)

        # 跳过空内容
        if not content or not content.strip():
            return None

        # 提取 @提及信息（消息列表 API 中的 mentions 字段）
        mentions = item.get("mentions", [])
        if mentions:
            mention_tags = []
            for m in mentions:
                name = m.get("name", "")
                mid = m.get("id", "")
                if mid == settings.LARK_TARGET_USER_ID:
                    mention_tags.append("[TARGET_USER]")
                elif name:
                    mention_tags.append(f"@{name}")
            if mention_tags:
                content = f"[提及: {', '.join(mention_tags)}] {content}"

        # 解析时间戳
        try:
            ts = int(create_time)
            created_at = datetime.fromtimestamp(ts, tz=timezone.utc)
        except (ValueError, TypeError):
            created_at = datetime.now(timezone.utc)

        # 提取 reply_to
        reply_to = item.get("parent_id", "") or ""

        return RawMessage(
            platform="lark",
            platform_msg_id=message_id,
            chat_id=chat_id,
            chat_name=chat_name,
            sender_id=sender_id,
            sender_name="",  # 后续批量解析
            content=content,
            msg_type=msg_type,
            reply_to_id=reply_to,
            created_at=created_at,
            raw_data=json.dumps({"sender_type": sender.get("sender_type", "")}, ensure_ascii=False),
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
            # 保留 @mention 的可读形式
            text = re.sub(r'<at user_id="([^"]+)">([^<]*)</at>', r'@\2', text)
            # 清理其他 HTML 标签
            text = re.sub(r"<[^>]+>", "", text)
            return text.strip()

        elif msg_type == "post":
            # 富文本消息
            texts = []
            title = body.get("title", "")
            if title:
                texts.append(f"[{title}]")

            content_blocks = body.get("content", [])
            # post 消息可能有多语言版本
            if isinstance(content_blocks, dict):
                # 取第一个语言版本
                for lang, blocks in content_blocks.items():
                    content_blocks = blocks
                    break

            if isinstance(content_blocks, list):
                for line in content_blocks:
                    if not isinstance(line, list):
                        continue
                    for node in line:
                        if not isinstance(node, dict):
                            continue
                        tag = node.get("tag", "")
                        if tag == "text":
                            texts.append(node.get("text", ""))
                        elif tag == "at":
                            user_name = node.get("user_name", "") or node.get("user_id", "")
                            texts.append(f"@{user_name}")
                        elif tag == "a":
                            link_text = node.get("text", "")
                            link_href = node.get("href", "")
                            if link_text:
                                texts.append(link_text)
                            elif link_href:
                                texts.append(link_href)
                        elif tag == "img":
                            texts.append("[图片]")
                        elif tag == "media":
                            texts.append("[文件]")
            return " ".join(texts).strip()

        elif msg_type in ("image", "file", "audio", "video", "sticker", "media"):
            return ""

        elif msg_type == "interactive":
            # 卡片消息，尝试提取标题
            title = body.get("header", {}).get("title", {}).get("content", "")
            return f"[卡片] {title}" if title else ""

        else:
            return ""

    # -----------------------------------------------------------------------
    # 深度链接
    # -----------------------------------------------------------------------

    @staticmethod
    def build_deep_link(chat_id: str, message_id: str) -> str:
        """构建飞书消息深度链接（点击可跳转到原消息）"""
        if settings.LARK_DOMAIN_TYPE == "feishu":
            return f"https://applink.feishu.cn/client/message/link?chatId={chat_id}&messageId={message_id}"
        else:
            return f"https://applink.larksuite.com/client/message/link?chatId={chat_id}&messageId={message_id}"

    # -----------------------------------------------------------------------
    # 辅助属性
    # -----------------------------------------------------------------------

    @property
    def bot_open_id(self) -> str:
        return self._bot_open_id
