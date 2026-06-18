"""
提醒调度器
==========
定时扫描待提醒队列，聚合后通过 Notifier 发送。

调度策略：
  1. 每隔 REMINDER_INTERVAL_MINUTES 扫描一次
  2. 按紧急程度排序，高优先级优先发送
  3. 同一条消息在 REMINDER_COOLDOWN_HOURS 内不重复提醒
  4. 单次最多发送 REMINDER_MAX_PER_BATCH 条
  5. 聚合为一张卡片发送（避免消息轰炸）
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import List

from config import settings
from src.storage.db import (
    get_session, get_pending_reminders, Reminder, ReminderStatus,
)
from src.delivery.notifier import (
    send_lark_text, send_lark_card, build_reminder_card,
    send_telegram_message,
)

logger = logging.getLogger("msg_reminder.scheduler")


def run_reminder_cycle() -> dict:
    """
    执行一轮提醒发送。

    流程：
      1. 从 DB 获取所有 pending 状态的提醒
      2. 过滤冷却期内的（避免重复打扰）
      3. 按紧急程度排序
      4. 聚合发送
      5. 更新状态为 reminded

    返回: {"total": N, "sent": N, "skipped": N}
    """
    session = get_session()
    try:
        reminders = get_pending_reminders(session, limit=100)
        if not reminders:
            logger.debug("无待发送提醒")
            return {"total": 0, "sent": 0, "skipped": 0}

        now = datetime.now(timezone.utc)
        cooldown = timedelta(hours=settings.REMINDER_COOLDOWN_HOURS)

        # 过滤冷却期内的
        eligible = []
        skipped = 0
        for r in reminders:
            if r.reminded_at and (now - r.reminded_at) < cooldown:
                skipped += 1
                continue
            eligible.append(r)

        if not eligible:
            return {"total": len(reminders), "sent": 0, "skipped": skipped}

        # 按紧急程度排序
        urgency_order = {"high": 0, "medium": 1, "low": 2}
        eligible.sort(key=lambda r: urgency_order.get(r.urgency, 1))

        # 取前 N 条
        batch = eligible[:settings.REMINDER_MAX_PER_BATCH]

        # 构建提醒数据
        reminder_data = []
        for r in batch:
            reminder_data.append({
                "platform": r.platform,
                "chat_name": r.chat_name,
                "sender_name": r.sender_name,
                "summary": r.summary,
                "urgency": r.urgency,
                "deep_link": r.deep_link,
            })

        # 发送聚合提醒
        sent = _send_aggregated_reminder(reminder_data)

        if sent:
            # 更新状态
            for r in batch:
                r.status = ReminderStatus.REMINDED
                r.reminded_at = now
                r.remind_count += 1
            session.commit()
            logger.info("提醒发送成功: %d 条", len(batch))
        else:
            logger.warning("提醒发送失败")

        return {
            "total": len(reminders),
            "sent": len(batch) if sent else 0,
            "skipped": skipped,
        }

    except Exception as e:
        logger.error("提醒调度异常: %s", e)
        return {"total": 0, "sent": 0, "skipped": 0, "error": str(e)}
    finally:
        session.close()


def _send_aggregated_reminder(reminders: List[dict]) -> bool:
    """发送聚合提醒（优先飞书卡片，备选文本）"""
    target_user = settings.LARK_TARGET_USER_ID

    if target_user:
        # 尝试发送飞书卡片
        card = build_reminder_card(reminders)
        if send_lark_card(target_user, card):
            return True

        # 降级为文本
        text = _build_text_reminder(reminders)
        if send_lark_text(target_user, text):
            return True

    # Telegram 备选
    tg_user = settings.TG_TARGET_USER_ID
    if tg_user:
        text = _build_text_reminder(reminders)
        return send_telegram_message(tg_user, text)

    logger.warning("无可用的提醒渠道（LARK_TARGET_USER_ID 和 TG_TARGET_USER_ID 均未配置）")
    return False


def _build_text_reminder(reminders: List[dict]) -> str:
    """构建纯文本提醒"""
    lines = [f"📬 你有 {len(reminders)} 条消息待回复：\n"]
    urgency_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}

    for i, r in enumerate(reminders[:10], 1):
        emoji = urgency_emoji.get(r.get("urgency", "medium"), "🟡")
        platform_label = {"lark": "飞书", "telegram": "TG", "whatsapp": "WA"}.get(
            r.get("platform", ""), r.get("platform", "")
        )
        lines.append(
            f"{emoji} {i}. [{platform_label}] {r.get('chat_name', '')}\n"
            f"   来自 {r.get('sender_name', '未知')}: {r.get('summary', '')}"
        )

    if len(reminders) > 10:
        lines.append(f"\n...还有 {len(reminders) - 10} 条")

    return "\n".join(lines)


def mark_as_replied(reminder_id: int) -> bool:
    """标记提醒为已回复"""
    session = get_session()
    try:
        reminder = session.query(Reminder).filter(Reminder.id == reminder_id).first()
        if reminder:
            reminder.status = ReminderStatus.REPLIED
            reminder.replied_at = datetime.now(timezone.utc)
            session.commit()
            return True
        return False
    finally:
        session.close()


def mark_as_ignored(reminder_id: int) -> bool:
    """标记提醒为已忽略"""
    session = get_session()
    try:
        reminder = session.query(Reminder).filter(Reminder.id == reminder_id).first()
        if reminder:
            reminder.status = ReminderStatus.IGNORED
            session.commit()
            return True
        return False
    finally:
        session.close()
