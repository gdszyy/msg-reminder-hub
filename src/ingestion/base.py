"""
消息拉取抽象基类
================
定义统一的消息拉取接口，所有平台 Fetcher 必须实现此接口。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional


@dataclass
class RawMessage:
    """
    标准化消息格式。
    所有平台的消息拉取后都必须转换为此格式。
    """
    platform: str                       # 来源平台: lark / telegram / whatsapp
    platform_msg_id: str                # 平台原始消息ID
    chat_id: str                        # 群组/对话ID
    chat_name: str = ""                 # 群组名称
    sender_id: str = ""                 # 发送者ID
    sender_name: str = ""               # 发送者名称
    content: str = ""                   # 消息文本内容
    msg_type: str = "text"              # 消息类型 (text/image/file/...)
    reply_to_id: str = ""              # 回复的消息ID（用于构建对话链）
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    raw_data: str = ""                  # 原始 JSON 数据（调试用）

    def to_dict(self) -> dict:
        """转换为字典，用于数据库写入"""
        return {
            "platform": self.platform,
            "platform_msg_id": self.platform_msg_id,
            "chat_id": self.chat_id,
            "chat_name": self.chat_name,
            "sender_id": self.sender_id,
            "sender_name": self.sender_name,
            "content": self.content,
            "msg_type": self.msg_type,
            "reply_to_id": self.reply_to_id,
            "created_at": self.created_at,
            "raw_data": self.raw_data,
        }


class BaseFetcher(ABC):
    """
    消息拉取器抽象基类。
    每个平台实现自己的 Fetcher，负责：
      1. 认证与连接
      2. 增量拉取消息
      3. 转换为 RawMessage 格式
    """

    @property
    @abstractmethod
    def platform_name(self) -> str:
        """平台标识名称"""
        ...

    @abstractmethod
    def authenticate(self) -> bool:
        """
        执行平台认证。
        返回 True 表示认证成功，False 表示失败。
        """
        ...

    @abstractmethod
    def list_chats(self) -> List[dict]:
        """
        列出所有可监控的群组/对话。
        返回格式: [{"chat_id": "...", "name": "...", "member_count": N}, ...]
        """
        ...

    @abstractmethod
    def fetch_messages(
        self,
        chat_id: str,
        since_timestamp: int = 0,
        since_message_id: str = "",
        limit: int = 100,
    ) -> List[RawMessage]:
        """
        增量拉取指定群组的消息。

        参数:
          chat_id: 群组ID
          since_timestamp: 从此时间戳之后开始拉取（秒）
          since_message_id: 从此消息ID之后开始拉取
          limit: 最大拉取条数

        返回: 标准化消息列表（按时间正序）
        """
        ...

    def is_configured(self) -> bool:
        """检查该平台是否已配置（有必要的凭证）"""
        return True
