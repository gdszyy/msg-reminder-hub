"""
通知发送器
==========
负责通过各渠道向目标用户发送提醒消息。
支持：
  - Lark 私聊消息（文本 / 卡片）
  - Telegram Bot 消息
"""

import json
import logging
import time
from typing import Dict, List, Optional

import requests

from config import settings

logger = logging.getLogger("msg_reminder.notifier")

# ---------------------------------------------------------------------------
# Lark Token 缓存
# ---------------------------------------------------------------------------

_lark_token_cache = {"token": "", "expires_at": 0}


def _get_lark_token() -> str:
    """获取并缓存飞书 tenant_access_token"""
    now = time.time()
    if _lark_token_cache["token"] and now < _lark_token_cache["expires_at"]:
        return _lark_token_cache["token"]

    if not settings.LARK_APP_ID or not settings.LARK_APP_SECRET:
        logger.error("LARK_APP_ID 或 LARK_APP_SECRET 未配置")
        return ""

    url = f"{settings.LARK_BASE_URL}/open-apis/auth/v3/tenant_access_token/internal"
    try:
        resp = requests.post(url, json={
            "app_id": settings.LARK_APP_ID,
            "app_secret": settings.LARK_APP_SECRET,
        }, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") == 0:
            token = data["tenant_access_token"]
            expires_in = data.get("expire", 7200) - 300
            _lark_token_cache["token"] = token
            _lark_token_cache["expires_at"] = now + expires_in
            return token
        else:
            logger.error("获取飞书 token 失败: %s", data.get("msg"))
            return ""
    except Exception as e:
        logger.error("获取飞书 token 异常: %s", e)
        return ""


# ---------------------------------------------------------------------------
# Lark 消息发送
# ---------------------------------------------------------------------------

def send_lark_text(receive_id: str, text: str, receive_id_type: str = "open_id") -> bool:
    """发送飞书文本消息"""
    token = _get_lark_token()
    if not token:
        return False

    url = f"{settings.LARK_BASE_URL}/open-apis/im/v1/messages?receive_id_type={receive_id_type}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "receive_id": receive_id,
        "msg_type": "text",
        "content": json.dumps({"text": text}),
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") == 0:
            logger.info("飞书消息发送成功: receive_id=%s", receive_id)
            return True
        else:
            logger.error("飞书消息发送失败: %s", data.get("msg"))
            return False
    except Exception as e:
        logger.error("飞书消息发送异常: %s", e)
        return False


def send_lark_card(receive_id: str, card: Dict, receive_id_type: str = "open_id") -> bool:
    """发送飞书交互式卡片"""
    token = _get_lark_token()
    if not token:
        return False

    url = f"{settings.LARK_BASE_URL}/open-apis/im/v1/messages?receive_id_type={receive_id_type}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "receive_id": receive_id,
        "msg_type": "interactive",
        "content": json.dumps(card, ensure_ascii=False),
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") == 0:
            logger.info("飞书卡片发送成功: receive_id=%s", receive_id)
            return True
        else:
            logger.error("飞书卡片发送失败: %s", data.get("msg"))
            return False
    except Exception as e:
        logger.error("飞书卡片发送异常: %s", e)
        return False


def build_reminder_card(reminders: List[Dict]) -> Dict:
    """
    构建聚合提醒卡片。
    reminders: [{platform, chat_name, sender_name, summary, urgency, deep_link}, ...]
    """
    elements = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"📬 **你有 {len(reminders)} 条消息待回复**",
            },
        },
        {"tag": "hr"},
    ]

    urgency_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}

    for i, r in enumerate(reminders[:10], 1):
        emoji = urgency_emoji.get(r.get("urgency", "medium"), "🟡")
        platform_label = {"lark": "飞书", "telegram": "TG", "whatsapp": "WA"}.get(
            r.get("platform", ""), r.get("platform", "")
        )
        line = (
            f"{emoji} **{i}. [{platform_label}] {r.get('chat_name', '未知群组')}**\n"
            f"来自 {r.get('sender_name', '未知')}: {r.get('summary', '')}"
        )
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": line},
        })

    if len(reminders) > 10:
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"...还有 {len(reminders) - 10} 条，请查看 Dashboard",
            },
        })

    elements.append({"tag": "hr"})
    elements.append({
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": "💡 回复「已处理」可标记消息为已读",
        },
    })

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "📋 消息待回复提醒"},
            "template": "blue",
        },
        "elements": elements,
    }
    return card


# ---------------------------------------------------------------------------
# Telegram 消息发送
# ---------------------------------------------------------------------------

def send_telegram_message(chat_id: str, text: str) -> bool:
    """发送 Telegram 消息"""
    if not settings.TG_BOT_TOKEN:
        logger.warning("TG_BOT_TOKEN 未配置")
        return False

    url = f"https://api.telegram.org/bot{settings.TG_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
    }

    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("ok"):
            logger.info("Telegram 消息发送成功: chat_id=%s", chat_id)
            return True
        else:
            logger.error("Telegram 消息发送失败: %s", data.get("description"))
            return False
    except Exception as e:
        logger.error("Telegram 消息发送异常: %s", e)
        return False
