"""
MSG Reminder Hub — 主入口
=========================
整合所有模块，提供：
  1. FastAPI Web 服务（Dashboard + API）
  2. APScheduler 定时任务（消息拉取 + 提醒发送）
  3. Lark Webhook 端点（实时接收消息）

启动方式：
  python main.py                    # 启动完整服务
  python main.py --fetch-only       # 仅执行一次消息拉取
  python main.py --remind-only      # 仅执行一次提醒发送
"""

import argparse
import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Dict, List

from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# 路径和环境初始化
# ---------------------------------------------------------------------------

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from config import settings
from src.storage.db import init_db, get_session, save_message, create_reminder, Message
from src.storage.cursor import CursorManager
from src.ingestion.lark_fetcher import LarkFetcher
from src.ingestion.tg_fetcher import TelegramFetcher
from src.processing.normalizer import normalize_messages
from src.processing.llm_analyzer import analyze_messages
from src.delivery.scheduler import run_reminder_cycle
from src.delivery.notifier import send_lark_text
from src.web.api import router as web_router

# ---------------------------------------------------------------------------
# 日志配置
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("msg_reminder.main")

# ---------------------------------------------------------------------------
# 全局 Fetcher 实例
# ---------------------------------------------------------------------------

lark_fetcher = LarkFetcher()
tg_fetcher = TelegramFetcher()
lark_cursor = CursorManager("lark")
tg_cursor = CursorManager("telegram")


# ---------------------------------------------------------------------------
# 核心业务流程
# ---------------------------------------------------------------------------

def run_fetch_cycle():
    """
    执行一轮消息拉取 + AI 分析 + 入库。

    流程：
      1. 对每个已配置的平台，获取群组列表
      2. 对每个群组，基于游标增量拉取消息
      3. 标准化消息
      4. 调用 LLM 分析，识别需要回复的消息
      5. 将消息和提醒写入数据库
      6. 更新游标
    """
    logger.info("=" * 60)
    logger.info("开始消息拉取周期: %s", datetime.now(timezone.utc).isoformat())
    logger.info("=" * 60)

    total_messages = 0
    total_reminders = 0

    # --- Lark ---
    if lark_fetcher.is_configured():
        try:
            if lark_fetcher.authenticate():
                lark_msgs, lark_rems = _process_platform(
                    fetcher=lark_fetcher,
                    cursor_mgr=lark_cursor,
                )
                total_messages += lark_msgs
                total_reminders += lark_rems
        except Exception as e:
            logger.error("Lark 拉取周期异常: %s", e)

    # --- Telegram ---
    if tg_fetcher.is_configured():
        try:
            if tg_fetcher.authenticate():
                tg_msgs, tg_rems = _process_platform(
                    fetcher=tg_fetcher,
                    cursor_mgr=tg_cursor,
                )
                total_messages += tg_msgs
                total_reminders += tg_rems
        except Exception as e:
            logger.error("Telegram 拉取周期异常: %s", e)

    logger.info(
        "拉取周期完成: 新消息=%d, 新提醒=%d",
        total_messages, total_reminders,
    )
    return {"messages": total_messages, "reminders": total_reminders}


def _process_platform(fetcher, cursor_mgr) -> tuple:
    """处理单个平台的消息拉取和分析"""
    platform = fetcher.platform_name
    total_msgs = 0
    total_rems = 0

    chats = fetcher.list_chats()
    logger.info("[%s] 获取到 %d 个群组", platform, len(chats))

    for chat_info in chats:
        chat_id = chat_info["chat_id"]
        chat_name = chat_info.get("name", "")

        # 获取游标
        last_msg_id, last_ts = cursor_mgr.get_last_position(chat_id)

        # 基于游标增量拉取（从上次截止时间到现在，拉完所有新消息）
        raw_messages = fetcher.fetch_messages(
            chat_id=chat_id,
            since_timestamp=last_ts,
            since_message_id=last_msg_id,
        )

        if not raw_messages:
            continue

        # 标准化
        valid_messages = normalize_messages(raw_messages)
        if not valid_messages:
            continue

        # 入库（入库后提取 id，避免 session 关闭后无法访问 ORM 对象）
        session = get_session()
        saved_msg_ids = []  # 保存每条消息的 DB id，与 valid_messages 一一对应
        try:
            for msg in valid_messages:
                msg_dict = msg.to_dict()
                msg_dict["chat_name"] = chat_name
                record = save_message(session, msg_dict)
                # record=None 表示已存在（去重），但仍然记录位置以保持索引对齐
                saved_msg_ids.append(record.id if record else None)
        finally:
            session.close()

        new_count = sum(1 for mid in saved_msg_ids if mid is not None)
        total_msgs += new_count

        if new_count == 0:
            # 所有消息都是重复的，但仍然更新游标
            last_valid = valid_messages[-1]
            new_ts = int(last_valid.created_at.timestamp()) if last_valid.created_at else int(time.time())
            cursor_mgr.update_position(chat_id, last_valid.platform_msg_id, new_ts)
            continue

        # ---- 对话分离 + 逐线程 LLM 分析 ----
        # 先把消息拆分为独立的对话线程，再逐线程分析
        # 这样 LLM 每次收到的是一个完整的对话，而不是被截断的片段
        from src.processing.thread_splitter import split_messages_into_threads

        # 构建用于分离的消息列表（带上全局索引）
        msgs_for_split = []
        for i, msg in enumerate(valid_messages):
            msgs_for_split.append({
                "_global_index": i,  # 保留全局索引，用于后续匹配 DB id
                "content": msg.content,
                "sender_id": msg.sender_id,
                "sender_name": msg.sender_name,
                "created_at": msg.created_at,
                "reply_to_id": msg.reply_to_id,
                "platform_msg_id": msg.platform_msg_id,
            })

        # 对话分离：500 条消息 → N 个独立线程
        threads = split_messages_into_threads(msgs_for_split)

        # 逐线程发给 LLM 分析
        all_needs_reply = []
        for thread_msgs in threads:
            # 构建 LLM 输入（保留全局索引）
            analysis_input = []
            for msg in thread_msgs:
                analysis_input.append({
                    "index": msg["_global_index"] + 1,  # LLM 输出的 message_index 对应全局位置
                    "sender_name": msg.get("sender_name", ""),
                    "content": msg.get("content", ""),
                    "created_at": msg["created_at"].strftime("%H:%M") if isinstance(msg.get("created_at"), datetime) else "",
                })

            thread_results = analyze_messages(
                messages=analysis_input,
                chat_name=chat_name,
                target_user_id=settings.LARK_TARGET_USER_ID,
            )
            all_needs_reply.extend(thread_results)

        # 创建提醒
        if all_needs_reply:
            session = get_session()
            try:
                for item in all_needs_reply:
                    msg_idx = item.get("message_index", 1) - 1
                    if 0 <= msg_idx < len(saved_msg_ids) and saved_msg_ids[msg_idx] is not None:
                        reminder_data = {
                            "message_id": saved_msg_ids[msg_idx],
                            "platform": platform,
                            "chat_id": chat_id,
                            "chat_name": chat_name,
                            "sender_name": valid_messages[msg_idx].sender_name if msg_idx < len(valid_messages) else "",
                            "summary": item.get("summary", ""),
                            "urgency": item.get("urgency", "medium"),
                            "confidence": item.get("confidence", 0.0),
                            "deep_link": _build_deep_link(platform, chat_id, valid_messages[msg_idx].platform_msg_id if msg_idx < len(valid_messages) else ""),
                        }
                        create_reminder(session, reminder_data)
                        total_rems += 1
            finally:
                session.close()

        # 更新游标
        last_valid = valid_messages[-1]
        new_ts = int(last_valid.created_at.timestamp()) if last_valid.created_at else int(time.time())
        cursor_mgr.update_position(chat_id, last_valid.platform_msg_id, new_ts)

    return total_msgs, total_rems


def _build_deep_link(platform: str, chat_id: str, msg_id: str) -> str:
    """构建跳转到原消息的链接"""
    if platform == "lark":
        # 飞书消息链接格式
        return f"https://applink.feishu.cn/client/message/link?chatId={chat_id}&messageId={msg_id}"
    elif platform == "telegram":
        # Telegram 群组消息链接
        return f"https://t.me/c/{chat_id.lstrip('-100')}/{msg_id.split('_')[-1] if '_' in msg_id else msg_id}"
    return ""


# ---------------------------------------------------------------------------
# FastAPI 应用
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时初始化数据库
    init_db()
    logger.info("MSG Reminder Hub 启动完成")

    # 启动定时任务
    from apscheduler.schedulers.background import BackgroundScheduler
    scheduler = BackgroundScheduler()

    # 消息拉取任务
    scheduler.add_job(
        run_fetch_cycle,
        "interval",
        minutes=settings.FETCH_INTERVAL_MINUTES,
        id="fetch_cycle",
        next_run_time=datetime.now(),  # 立即执行一次
    )

    # 提醒发送任务
    scheduler.add_job(
        run_reminder_cycle,
        "interval",
        minutes=settings.REMINDER_INTERVAL_MINUTES,
        id="reminder_cycle",
    )

    scheduler.start()
    logger.info(
        "定时任务已启动: 拉取间隔=%d分钟, 提醒间隔=%d分钟",
        settings.FETCH_INTERVAL_MINUTES,
        settings.REMINDER_INTERVAL_MINUTES,
    )

    yield

    # 关闭时清理
    scheduler.shutdown()
    logger.info("MSG Reminder Hub 已关闭")


app = FastAPI(
    title="MSG Reminder Hub",
    description="跨平台消息整合提醒平台",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂载 Web Dashboard 路由
app.include_router(web_router)


# ---------------------------------------------------------------------------
# Lark Webhook 端点（实时接收消息）
# ---------------------------------------------------------------------------

@app.post("/lark/webhook")
async def lark_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    飞书事件订阅 Webhook 端点。
    接收实时消息推送，快速判定是否需要提醒。
    """
    payload = await request.json()

    # URL 验证挑战
    if "challenge" in payload:
        return JSONResponse({"challenge": payload["challenge"]})

    # 提取消息事件
    event_type = payload.get("header", {}).get("event_type", "")
    if event_type == "im.message.receive_v1":
        background_tasks.add_task(_handle_lark_webhook_message, payload)

    return JSONResponse({"code": 0, "msg": "ok"})


async def _handle_lark_webhook_message(payload: dict):
    """后台处理 Lark Webhook 消息"""
    try:
        event = payload.get("event", {})
        message = event.get("message", {})
        sender = event.get("sender", {})

        content_str = message.get("content", "{}")
        try:
            content_obj = json.loads(content_str)
            text = content_obj.get("text", "")
        except json.JSONDecodeError:
            text = content_str

        if not text:
            return

        chat_id = message.get("chat_id", "")
        sender_id = sender.get("sender_id", {}).get("open_id", "")
        message_id = message.get("message_id", "")

        # 快速判定
        from src.processing.llm_analyzer import quick_check_single
        result = quick_check_single(
            content=text,
            sender_name=sender_id,
            chat_name=chat_id,
        )

        if result:
            # 入库并创建提醒
            session = get_session()
            try:
                msg_record = save_message(session, {
                    "platform": "lark",
                    "platform_msg_id": message_id,
                    "chat_id": chat_id,
                    "sender_id": sender_id,
                    "content": text,
                    "msg_type": "text",
                    "created_at": datetime.now(timezone.utc),
                })
                if msg_record:
                    create_reminder(session, {
                        "message_id": msg_record.id,
                        "platform": "lark",
                        "chat_id": chat_id,
                        "sender_name": sender_id,
                        "summary": result.get("reason", text[:100]),
                        "urgency": result.get("urgency", "medium"),
                        "confidence": result.get("confidence", 0.0),
                    })
            finally:
                session.close()

    except Exception as e:
        logger.error("处理 Lark Webhook 消息异常: %s", e)


# ---------------------------------------------------------------------------
# 健康检查
# ---------------------------------------------------------------------------

@app.get("/health")
async def health_check():
    return {
        "status": "ok",
        "service": "msg-reminder-hub",
        "version": "1.0.0",
        "time": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/")
async def root():
    return {
        "service": "MSG Reminder Hub",
        "description": "跨平台消息整合提醒平台",
        "endpoints": {
            "GET /dashboard": "Web Dashboard",
            "GET /api/reminders": "提醒列表 API",
            "GET /api/stats": "统计数据",
            "POST /lark/webhook": "飞书 Webhook",
            "GET /health": "健康检查",
        },
        "docs": "/docs",
    }


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="MSG Reminder Hub")
    parser.add_argument("--fetch-only", action="store_true", help="仅执行一次消息拉取")
    parser.add_argument("--remind-only", action="store_true", help="仅执行一次提醒发送")
    parser.add_argument("--init-db", action="store_true", help="初始化数据库")
    args = parser.parse_args()

    if args.init_db:
        init_db()
        print("✅ 数据库初始化完成")
        return

    if args.fetch_only:
        init_db()
        result = run_fetch_cycle()
        print(f"✅ 拉取完成: {result}")
        return

    if args.remind_only:
        init_db()
        result = run_reminder_cycle()
        print(f"✅ 提醒完成: {result}")
        return

    # 启动完整服务
    import uvicorn
    uvicorn.run(
        "main:app",
        host=settings.WEB_HOST,
        port=settings.WEB_PORT,
        reload=False,
    )


if __name__ == "__main__":
    main()
