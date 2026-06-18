"""
Web Dashboard API
=================
提供 RESTful API 供前端 Dashboard 使用，以及简单的 HTML 页面。

端点：
  GET  /dashboard              - HTML Dashboard 页面
  GET  /api/reminders          - 获取提醒列表
  POST /api/reminders/{id}/reply   - 标记为已回复
  POST /api/reminders/{id}/ignore  - 标记为已忽略
  GET  /api/messages/{id}/context  - 获取消息上下文
  GET  /api/stats              - 统计数据
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import HTMLResponse

from src.storage.db import (
    get_session, Reminder, Message, ReminderStatus,
)
from src.delivery.scheduler import mark_as_replied, mark_as_ignored

logger = logging.getLogger("msg_reminder.web")

router = APIRouter()


# ---------------------------------------------------------------------------
# API 端点
# ---------------------------------------------------------------------------

@router.get("/api/reminders")
def list_reminders(
    status: Optional[str] = Query(None, description="过滤状态: pending/reminded/replied/ignored"),
    platform: Optional[str] = Query(None, description="过滤平台: lark/telegram/whatsapp"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """获取提醒列表"""
    session = get_session()
    try:
        query = session.query(Reminder)
        if status:
            query = query.filter(Reminder.status == status)
        if platform:
            query = query.filter(Reminder.platform == platform)

        total = query.count()
        items = (
            query.order_by(Reminder.created_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

        return {
            "total": total,
            "items": [_serialize_reminder(r) for r in items],
        }
    finally:
        session.close()


@router.post("/api/reminders/{reminder_id}/reply")
def api_mark_replied(reminder_id: int):
    """标记提醒为已回复"""
    if mark_as_replied(reminder_id):
        return {"ok": True, "message": "已标记为已回复"}
    raise HTTPException(status_code=404, detail="提醒不存在")


@router.post("/api/reminders/{reminder_id}/ignore")
def api_mark_ignored(reminder_id: int):
    """标记提醒为已忽略"""
    if mark_as_ignored(reminder_id):
        return {"ok": True, "message": "已标记为已忽略"}
    raise HTTPException(status_code=404, detail="提醒不存在")


@router.get("/api/messages/{message_id}/context")
def get_message_context(message_id: int, window: int = Query(10, ge=1, le=50)):
    """获取消息的上下文（前后 N 条消息）"""
    session = get_session()
    try:
        msg = session.query(Message).filter(Message.id == message_id).first()
        if not msg:
            raise HTTPException(status_code=404, detail="消息不存在")

        # 获取同群组的上下文消息
        context = (
            session.query(Message)
            .filter(
                Message.platform == msg.platform,
                Message.chat_id == msg.chat_id,
                Message.created_at >= msg.created_at,
            )
            .order_by(Message.created_at.asc())
            .limit(window)
            .all()
        )

        # 也获取之前的消息
        before = (
            session.query(Message)
            .filter(
                Message.platform == msg.platform,
                Message.chat_id == msg.chat_id,
                Message.created_at < msg.created_at,
            )
            .order_by(Message.created_at.desc())
            .limit(window)
            .all()
        )
        before.reverse()

        all_msgs = before + context
        return {
            "target_message_id": message_id,
            "context": [_serialize_message(m) for m in all_msgs],
        }
    finally:
        session.close()


@router.get("/api/stats")
def get_stats():
    """获取统计数据"""
    session = get_session()
    try:
        total_messages = session.query(Message).count()
        total_reminders = session.query(Reminder).count()
        pending = session.query(Reminder).filter(
            Reminder.status == ReminderStatus.PENDING
        ).count()
        reminded = session.query(Reminder).filter(
            Reminder.status == ReminderStatus.REMINDED
        ).count()
        replied = session.query(Reminder).filter(
            Reminder.status == ReminderStatus.REPLIED
        ).count()

        return {
            "total_messages": total_messages,
            "total_reminders": total_reminders,
            "pending": pending,
            "reminded": reminded,
            "replied": replied,
            "reply_rate": round(replied / max(total_reminders, 1) * 100, 1),
        }
    finally:
        session.close()


# ---------------------------------------------------------------------------
# HTML Dashboard
# ---------------------------------------------------------------------------

@router.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    """简单的 HTML Dashboard 页面"""
    return DASHBOARD_HTML


# ---------------------------------------------------------------------------
# 序列化辅助函数
# ---------------------------------------------------------------------------

def _serialize_reminder(r: Reminder) -> dict:
    return {
        "id": r.id,
        "message_id": r.message_id,
        "platform": r.platform,
        "chat_id": r.chat_id,
        "chat_name": r.chat_name,
        "sender_name": r.sender_name,
        "summary": r.summary,
        "urgency": r.urgency,
        "status": r.status,
        "confidence": r.confidence,
        "deep_link": r.deep_link,
        "created_at": r.created_at.isoformat() if r.created_at else "",
        "reminded_at": r.reminded_at.isoformat() if r.reminded_at else "",
        "remind_count": r.remind_count,
    }


def _serialize_message(m: Message) -> dict:
    return {
        "id": m.id,
        "platform": m.platform,
        "platform_msg_id": m.platform_msg_id,
        "chat_id": m.chat_id,
        "chat_name": m.chat_name,
        "sender_id": m.sender_id,
        "sender_name": m.sender_name,
        "content": m.content,
        "msg_type": m.msg_type,
        "created_at": m.created_at.isoformat() if m.created_at else "",
    }


# ---------------------------------------------------------------------------
# Dashboard HTML 模板
# ---------------------------------------------------------------------------

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MSG Reminder Hub - Dashboard</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f5f7fa; color: #333; }
        .container { max-width: 1200px; margin: 0 auto; padding: 20px; }
        h1 { margin-bottom: 20px; color: #1a1a2e; }
        .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 24px; }
        .stat-card { background: white; border-radius: 12px; padding: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); }
        .stat-card .label { font-size: 14px; color: #666; margin-bottom: 4px; }
        .stat-card .value { font-size: 28px; font-weight: 700; color: #1a1a2e; }
        .filters { display: flex; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
        .filters select, .filters button { padding: 8px 16px; border: 1px solid #ddd; border-radius: 8px; background: white; cursor: pointer; }
        .filters button.active { background: #4361ee; color: white; border-color: #4361ee; }
        .reminder-list { display: flex; flex-direction: column; gap: 12px; }
        .reminder-card { background: white; border-radius: 12px; padding: 16px 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); display: flex; align-items: flex-start; gap: 12px; transition: transform 0.1s; }
        .reminder-card:hover { transform: translateY(-1px); box-shadow: 0 4px 12px rgba(0,0,0,0.1); }
        .urgency-dot { width: 12px; height: 12px; border-radius: 50%; flex-shrink: 0; margin-top: 4px; }
        .urgency-high { background: #e63946; }
        .urgency-medium { background: #f4a261; }
        .urgency-low { background: #2a9d8f; }
        .reminder-content { flex: 1; }
        .reminder-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px; }
        .reminder-chat { font-weight: 600; font-size: 14px; }
        .reminder-platform { font-size: 12px; padding: 2px 8px; border-radius: 4px; background: #e8f4fd; color: #1976d2; }
        .reminder-summary { font-size: 14px; color: #555; margin-bottom: 8px; }
        .reminder-meta { font-size: 12px; color: #999; }
        .reminder-actions { display: flex; gap: 8px; flex-shrink: 0; }
        .btn { padding: 6px 12px; border: none; border-radius: 6px; cursor: pointer; font-size: 12px; }
        .btn-reply { background: #4361ee; color: white; }
        .btn-ignore { background: #e9ecef; color: #666; }
        .empty { text-align: center; padding: 60px 20px; color: #999; }
        .context-modal { display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.5); z-index: 1000; justify-content: center; align-items: center; }
        .context-modal.active { display: flex; }
        .context-panel { background: white; border-radius: 16px; width: 90%; max-width: 700px; max-height: 80vh; overflow-y: auto; padding: 24px; }
        .context-msg { padding: 8px 0; border-bottom: 1px solid #f0f0f0; }
        .context-msg .sender { font-weight: 600; font-size: 13px; color: #4361ee; }
        .context-msg .text { font-size: 14px; margin-top: 2px; }
        .context-msg.highlight { background: #fff3cd; border-radius: 8px; padding: 8px; }
    </style>
</head>
<body>
    <div class="container">
        <h1>📬 MSG Reminder Hub</h1>
        <div class="stats" id="stats"></div>
        <div class="filters">
            <button class="active" onclick="filterStatus('')">全部</button>
            <button onclick="filterStatus('pending')">待回复</button>
            <button onclick="filterStatus('reminded')">已提醒</button>
            <button onclick="filterStatus('replied')">已处理</button>
            <select onchange="filterPlatform(this.value)">
                <option value="">所有平台</option>
                <option value="lark">飞书</option>
                <option value="telegram">Telegram</option>
            </select>
        </div>
        <div class="reminder-list" id="reminderList"></div>
    </div>
    <div class="context-modal" id="contextModal" onclick="closeContext(event)">
        <div class="context-panel" id="contextPanel"></div>
    </div>
    <script>
        let currentStatus = '';
        let currentPlatform = '';

        async function loadStats() {
            const resp = await fetch('/api/stats');
            const data = await resp.json();
            document.getElementById('stats').innerHTML = `
                <div class="stat-card"><div class="label">待回复</div><div class="value">${data.pending}</div></div>
                <div class="stat-card"><div class="label">已提醒</div><div class="value">${data.reminded}</div></div>
                <div class="stat-card"><div class="label">已处理</div><div class="value">${data.replied}</div></div>
                <div class="stat-card"><div class="label">回复率</div><div class="value">${data.reply_rate}%</div></div>
            `;
        }

        async function loadReminders() {
            let url = '/api/reminders?limit=50';
            if (currentStatus) url += `&status=${currentStatus}`;
            if (currentPlatform) url += `&platform=${currentPlatform}`;
            const resp = await fetch(url);
            const data = await resp.json();
            const list = document.getElementById('reminderList');
            if (!data.items.length) {
                list.innerHTML = '<div class="empty">暂无提醒记录</div>';
                return;
            }
            list.innerHTML = data.items.map(r => `
                <div class="reminder-card">
                    <div class="urgency-dot urgency-${r.urgency}"></div>
                    <div class="reminder-content" onclick="showContext(${r.message_id})">
                        <div class="reminder-header">
                            <span class="reminder-chat">${r.chat_name || '未知群组'}</span>
                            <span class="reminder-platform">${{lark:'飞书',telegram:'TG',whatsapp:'WA'}[r.platform]||r.platform}</span>
                        </div>
                        <div class="reminder-summary">${r.sender_name}: ${r.summary}</div>
                        <div class="reminder-meta">${r.created_at ? new Date(r.created_at).toLocaleString('zh-CN') : ''} · 提醒${r.remind_count}次</div>
                    </div>
                    <div class="reminder-actions">
                        ${r.status === 'pending' || r.status === 'reminded' ? `
                            <button class="btn btn-reply" onclick="markReply(${r.id})">已回复</button>
                            <button class="btn btn-ignore" onclick="markIgnore(${r.id})">忽略</button>
                        ` : `<span style="font-size:12px;color:#999">${{replied:'✅已处理',ignored:'⏭已忽略',reminded:'🔔已提醒'}[r.status]||r.status}</span>`}
                    </div>
                </div>
            `).join('');
        }

        function filterStatus(s) {
            currentStatus = s;
            document.querySelectorAll('.filters button').forEach(b => b.classList.remove('active'));
            event.target.classList.add('active');
            loadReminders();
        }
        function filterPlatform(p) { currentPlatform = p; loadReminders(); }

        async function markReply(id) {
            await fetch(`/api/reminders/${id}/reply`, {method:'POST'});
            loadReminders(); loadStats();
        }
        async function markIgnore(id) {
            await fetch(`/api/reminders/${id}/ignore`, {method:'POST'});
            loadReminders(); loadStats();
        }

        async function showContext(msgId) {
            const resp = await fetch(`/api/messages/${msgId}/context?window=10`);
            const data = await resp.json();
            const panel = document.getElementById('contextPanel');
            panel.innerHTML = '<h3 style="margin-bottom:12px">📝 消息上下文</h3>' +
                data.context.map(m => `
                    <div class="context-msg ${m.id === msgId ? 'highlight' : ''}">
                        <div class="sender">${m.sender_name || m.sender_id}</div>
                        <div class="text">${m.content}</div>
                    </div>
                `).join('');
            document.getElementById('contextModal').classList.add('active');
        }
        function closeContext(e) {
            if (e.target === document.getElementById('contextModal'))
                document.getElementById('contextModal').classList.remove('active');
        }

        loadStats();
        loadReminders();
        setInterval(() => { loadStats(); loadReminders(); }, 30000);
    </script>
</body>
</html>"""
