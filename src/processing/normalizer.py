"""
消息标准化处理器
================
对拉取到的 RawMessage 进行清洗和标准化：
  - 去除无效消息（空内容、系统消息等）
  - 清理 HTML/Markdown 标签
  - 截断过长消息
  - 构建对话上下文窗口
"""

import re
import logging
from typing import List, Optional

from src.ingestion.base import RawMessage

logger = logging.getLogger("msg_reminder.normalizer")

# 最大消息长度（超过则截断）
MAX_CONTENT_LENGTH = 2000

# 需要过滤的系统消息模式
SYSTEM_MSG_PATTERNS = [
    r"^.*加入了群聊$",
    r"^.*退出了群聊$",
    r"^.*撤回了一条消息$",
    r"^.*修改了群名称$",
    r"^\[系统消息\]",
]

_system_re = [re.compile(p) for p in SYSTEM_MSG_PATTERNS]


def clean_content(text: str) -> str:
    """清理消息内容：去除 HTML 标签、多余空白"""
    # 去除 HTML 标签
    text = re.sub(r"<[^>]+>", "", text)
    # 去除多余空白
    text = re.sub(r"\s+", " ", text).strip()
    # 截断
    if len(text) > MAX_CONTENT_LENGTH:
        text = text[:MAX_CONTENT_LENGTH] + "..."
    return text


def is_valid_message(msg: RawMessage) -> bool:
    """判断消息是否有效（非系统消息、非空内容）"""
    if not msg.content or not msg.content.strip():
        return False
    if msg.msg_type in ("system", "event"):
        return False
    content = msg.content.strip()
    for pattern in _system_re:
        if pattern.match(content):
            return False
    # 过滤纯表情/贴纸
    if msg.msg_type in ("sticker", "emoji"):
        return False
    return True


def normalize_messages(messages: List[RawMessage]) -> List[RawMessage]:
    """
    批量标准化消息列表。
    过滤无效消息，清理内容。
    """
    result = []
    for msg in messages:
        if not is_valid_message(msg):
            continue
        msg.content = clean_content(msg.content)
        if msg.content:
            result.append(msg)
    logger.debug("标准化完成: 输入 %d 条, 有效 %d 条", len(messages), len(result))
    return result


def build_context_window(
    messages: List[RawMessage],
    target_msg_index: int,
    window_size: int = 10,
) -> List[RawMessage]:
    """
    构建目标消息的上下文窗口。
    取目标消息前后各 window_size/2 条消息。
    """
    half = window_size // 2
    start = max(0, target_msg_index - half)
    end = min(len(messages), target_msg_index + half + 1)
    return messages[start:end]
