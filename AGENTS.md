# MSG Reminder Hub — AI Agent 索引

## 项目概述

跨平台消息整合提醒平台。自动扫描 Lark / Telegram / WhatsApp 等多平台对话，通过 LLM 智能识别需要用户回复的消息，集中提醒并提供 Web 界面查看完整上下文。

## 架构分层

| 层级 | 目录 | 职责 |
|------|------|------|
| 接入层 | `src/ingestion/` | 各平台消息拉取，转换为统一 `RawMessage` 格式 |
| 处理层 | `src/processing/` | 消息标准化、LLM 意图分析、回复检测 |
| 存储层 | `src/storage/` | 数据库模型、游标管理 |
| 触达层 | `src/delivery/` | 提醒调度、通知发送（Lark 卡片/TG 消息） |
| 展示层 | `src/web/` | Web Dashboard API + HTML 页面 |
| 配置 | `config/` | 统一配置管理 |
| 入口 | `main.py` | FastAPI + APScheduler 整合入口 |

## 平台接入状态

| 平台 | 文件 | 状态 | 说明 |
|------|------|------|------|
| Lark (飞书) | `src/ingestion/lark_fetcher.py` | ✅ 已实现 | 增量拉取 + Webhook 实时 |
| Telegram | `src/ingestion/tg_fetcher.py` | ✅ 已实现 | User API (MTProto/Telethon)，手机号登录，扫描所有对话 |
| WhatsApp | — | ⏸ 暂缓 | 待接入 Business API |

## 关键文件索引

### 核心业务流程
- `main.py` — 主入口，整合调度、Web 服务、Webhook
- `src/processing/llm_analyzer.py` — LLM 意图分析核心（Prompt + 解析）
- `src/delivery/scheduler.py` — 提醒调度策略（冷却、聚合、优先级）

### 数据模型
- `src/storage/db.py` — SQLAlchemy 模型定义（Message / Reminder / Cursor）

### 平台适配
- `src/ingestion/base.py` — 抽象基类 `BaseFetcher` + `RawMessage` 数据结构
- `src/ingestion/lark_fetcher.py` — 飞书实现（token 缓存、消息解析、群组列表）
- `src/ingestion/tg_fetcher.py` — Telegram 实现（Bot API、getUpdates）

### 配置
- `config/settings.py` — 环境变量统一管理
- `.env.example` — 完整环境变量说明

## 代码来源

本项目的 Lark 相关能力提取自 `ai-secretary-architecture` 仓库：

| 原始文件 | 提取能力 | 新位置 |
|----------|----------|--------|
| `scripts/daily_progress_updater.py` | 增量拉取、游标机制、消息解析 | `src/ingestion/lark_fetcher.py` |
| `scripts/lark_sdk_client.py` | SDK 封装、Token 管理 | `src/ingestion/lark_fetcher.py` |
| `scripts/thread_separator.py` | LLM 调用、JSON 解析 | `src/processing/llm_analyzer.py` |
| `scripts/requirement_followup.py` | 提醒调度、冷却机制 | `src/delivery/scheduler.py` |
| `scripts/lark_bitable_client.py` | 数据持久化 | `src/storage/db.py` (改为 SQLAlchemy) |
| `main.py` | Webhook 处理、消息路由 | `main.py` |

## 扩展新平台指南

1. 在 `src/ingestion/` 下创建新的 Fetcher 类，继承 `BaseFetcher`
2. 实现 `authenticate()`、`list_chats()`、`fetch_messages()` 三个方法
3. 在 `config/settings.py` 中添加新平台的配置项
4. 在 `main.py` 的 `run_fetch_cycle()` 中注册新 Fetcher
5. 在 `.env.example` 中添加说明
