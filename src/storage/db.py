"""
数据库模型与操作
================
使用 SQLAlchemy 2.0 ORM，支持 SQLite（开发）和 MySQL/PostgreSQL（生产）。

核心表：
  - messages: 原始消息记录（标准化后）
  - reminders: 待回复/待处理提醒队列
  - cursors: 各平台/群组的拉取游标
"""

import sys
import logging
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import (
    create_engine, Column, String, Text, Integer, Float,
    Boolean, DateTime, Enum as SAEnum, Index,
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session

from config import settings

logger = logging.getLogger("msg_reminder.storage")

# ---------------------------------------------------------------------------
# 数据库引擎
# ---------------------------------------------------------------------------

# 根据数据库类型调整 engine 参数
_engine_kwargs = {
    "echo": False,
    "pool_pre_ping": True,
}
# MySQL 需要指定字符集和连接池大小
if "mysql" in settings.DATABASE_URL:
    _engine_kwargs["pool_size"] = 5
    _engine_kwargs["max_overflow"] = 10
    _engine_kwargs["pool_recycle"] = 3600  # 1小时回收连接，避免 MySQL gone away
    # 确保 URL 带上 charset
    _db_url = settings.DATABASE_URL
    if "charset" not in _db_url:
        sep = "&" if "?" in _db_url else "?"
        _db_url = f"{_db_url}{sep}charset=utf8mb4"
else:
    _db_url = settings.DATABASE_URL

engine = create_engine(_db_url, **_engine_kwargs)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


# ---------------------------------------------------------------------------
# 枚举定义
# ---------------------------------------------------------------------------

class Platform:
    LARK = "lark"
    TELEGRAM = "telegram"
    WHATSAPP = "whatsapp"


class ReminderStatus:
    PENDING = "pending"          # 待提醒
    REMINDED = "reminded"        # 已提醒
    REPLIED = "replied"          # 已回复
    IGNORED = "ignored"          # 已忽略
    EXPIRED = "expired"          # 已过期


class Urgency:
    HIGH = "high"                # 高优先级（直接@、明确提问）
    MEDIUM = "medium"            # 中优先级（间接相关、需要关注）
    LOW = "low"                  # 低优先级（可能相关）


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

class Message(Base):
    """标准化消息记录"""
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    platform = Column(String(20), nullable=False, index=True)       # lark / telegram / whatsapp
    platform_msg_id = Column(String(128), nullable=False)           # 平台原始消息ID
    chat_id = Column(String(128), nullable=False, index=True)       # 群组/对话ID
    chat_name = Column(String(255), default="")                     # 群组名称
    sender_id = Column(String(128), nullable=False)                 # 发送者ID
    sender_name = Column(String(128), default="")                   # 发送者名称
    content = Column(Text, nullable=False)                          # 消息文本内容
    msg_type = Column(String(32), default="text")                   # 消息类型
    reply_to_id = Column(String(128), default="")                   # 回复的消息ID
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))  # 消息原始时间
    fetched_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))  # 拉取时间
    raw_data = Column(Text, default="")                             # 原始JSON（调试用）

    __table_args__ = (
        Index("ix_msg_platform_chat", "platform", "chat_id"),
        Index("ix_msg_unique", "platform", "platform_msg_id", unique=True),
    )


class Reminder(Base):
    """待回复/待处理提醒记录"""
    __tablename__ = "reminders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    message_id = Column(Integer, nullable=False, index=True)        # 关联的消息ID
    platform = Column(String(20), nullable=False)                   # 来源平台
    chat_id = Column(String(128), nullable=False)                   # 来源群组
    chat_name = Column(String(255), default="")                     # 群组名称
    sender_name = Column(String(128), default="")                   # 提问者名称
    summary = Column(Text, nullable=False)                          # AI 生成的摘要
    urgency = Column(String(16), default=Urgency.MEDIUM)            # 紧急程度
    status = Column(String(20), default=ReminderStatus.PENDING, index=True)
    confidence = Column(Float, default=0.0)                         # AI 置信度
    context_messages = Column(Text, default="[]")                   # 上下文消息ID列表(JSON)
    deep_link = Column(String(512), default="")                     # 跳转到原消息的链接
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    reminded_at = Column(DateTime, nullable=True)                   # 最后提醒时间
    replied_at = Column(DateTime, nullable=True)                    # 回复时间
    remind_count = Column(Integer, default=0)                       # 提醒次数

    __table_args__ = (
        Index("ix_reminder_status_created", "status", "created_at"),
    )


class Cursor(Base):
    """各平台/群组的拉取游标"""
    __tablename__ = "cursors"

    id = Column(Integer, primary_key=True, autoincrement=True)
    platform = Column(String(20), nullable=False)
    chat_id = Column(String(128), nullable=False)
    last_message_id = Column(String(128), default="")               # 最后拉取的消息ID
    last_timestamp = Column(Integer, default=0)                     # 最后拉取的时间戳(秒)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_cursor_unique", "platform", "chat_id", unique=True),
    )


# ---------------------------------------------------------------------------
# 数据库操作
# ---------------------------------------------------------------------------

def init_db():
    """初始化数据库（创建所有表）"""
    Base.metadata.create_all(engine)
    logger.info("数据库初始化完成: %s", settings.DATABASE_URL)


def get_session() -> Session:
    """获取数据库 Session"""
    return SessionLocal()


def save_message(session: Session, msg: dict) -> Optional[Message]:
    """
    保存标准化消息到数据库（去重：platform + platform_msg_id）。
    返回 Message 对象，若已存在则返回 None。
    """
    existing = session.query(Message).filter(
        Message.platform == msg["platform"],
        Message.platform_msg_id == msg["platform_msg_id"],
    ).first()
    if existing:
        return None

    record = Message(
        platform=msg["platform"],
        platform_msg_id=msg["platform_msg_id"],
        chat_id=msg["chat_id"],
        chat_name=msg.get("chat_name", ""),
        sender_id=msg["sender_id"],
        sender_name=msg.get("sender_name", ""),
        content=msg["content"],
        msg_type=msg.get("msg_type", "text"),
        reply_to_id=msg.get("reply_to_id", ""),
        created_at=msg.get("created_at", datetime.now(timezone.utc)),
        raw_data=msg.get("raw_data", ""),
    )
    session.add(record)
    session.commit()
    return record


def create_reminder(session: Session, reminder_data: dict) -> Reminder:
    """创建一条提醒记录"""
    reminder = Reminder(
        message_id=reminder_data["message_id"],
        platform=reminder_data["platform"],
        chat_id=reminder_data["chat_id"],
        chat_name=reminder_data.get("chat_name", ""),
        sender_name=reminder_data.get("sender_name", ""),
        summary=reminder_data["summary"],
        urgency=reminder_data.get("urgency", Urgency.MEDIUM),
        confidence=reminder_data.get("confidence", 0.0),
        context_messages=reminder_data.get("context_messages", "[]"),
        deep_link=reminder_data.get("deep_link", ""),
    )
    session.add(reminder)
    session.commit()
    return reminder


def get_pending_reminders(session: Session, limit: int = 50) -> List[Reminder]:
    """获取待提醒的记录"""
    return (
        session.query(Reminder)
        .filter(Reminder.status == ReminderStatus.PENDING)
        .order_by(Reminder.created_at.asc())
        .limit(limit)
        .all()
    )


def get_cursor(session: Session, platform: str, chat_id: str) -> Optional[Cursor]:
    """获取指定平台/群组的游标"""
    return session.query(Cursor).filter(
        Cursor.platform == platform,
        Cursor.chat_id == chat_id,
    ).first()


def upsert_cursor(session: Session, platform: str, chat_id: str,
                  last_message_id: str = "", last_timestamp: int = 0):
    """更新或创建游标"""
    cursor = get_cursor(session, platform, chat_id)
    if cursor:
        if last_message_id:
            cursor.last_message_id = last_message_id
        if last_timestamp:
            cursor.last_timestamp = last_timestamp
        cursor.updated_at = datetime.now(timezone.utc)
    else:
        cursor = Cursor(
            platform=platform,
            chat_id=chat_id,
            last_message_id=last_message_id,
            last_timestamp=last_timestamp,
        )
        session.add(cursor)
    session.commit()


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if "--init" in sys.argv:
        init_db()
        print("✅ 数据库初始化完成")
    else:
        print("用法: python -m src.storage.db --init")
