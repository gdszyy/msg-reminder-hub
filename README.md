# MSG Reminder Hub — 跨平台消息整合提醒平台

> 自动扫描 Lark / Telegram / WhatsApp 等多平台对话，通过 LLM 智能识别需要你回复的消息，集中提醒并提供 Web 界面查看完整上下文。

## 核心能力

| 能力 | 说明 | 状态 |
|------|------|------|
| Lark 消息拉取 | 增量拉取飞书群聊/私聊消息，支持 Webhook 实时接收 | ✅ 已实现 |
| Telegram 消息拉取 | 通过 User API (MTProto/Telethon) 扫描你账号的所有对话 | ✅ 已实现 |
| WhatsApp 消息拉取 | 基于 WhatsApp Business API | ⏸ 暂缓 |
| LLM 智能识别 | 自动判断消息是否需要用户回复/处理 | ✅ 已实现 |
| 统一待办池 | 将多平台消息整合为统一的待回复记录 | ✅ 已实现 |
| 定时提醒 | 通过 Lark 机器人发送聚合提醒 | ✅ 已实现 |
| Web Dashboard | 查看完整消息上下文，快速标记已处理 | ✅ 已实现 |

## 架构概览

```
┌─────────────────────────────────────────────────────────────────┐
│                     Ingestion Layer                              │
│  ┌─────────────┐  ┌──────────────┐  ┌───────────────────────┐  │
│  │ Lark Fetcher│  │ TG Fetcher   │  │ WhatsApp Fetcher (TBD)│  │
│  └──────┬──────┘  └──────┬───────┘  └───────────┬───────────┘  │
└─────────┼────────────────┼───────────────────────┼──────────────┘
          │                │                       │
          ▼                ▼                       ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Processing Layer                              │
│  ┌──────────────┐  ┌────────────────┐  ┌─────────────────────┐ │
│  │  Normalizer  │→ │  LLM Analyzer  │→ │  Reply Detector     │ │
│  └──────────────┘  └────────────────┘  └─────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
          │
          ▼
┌─────────────────────────────────────────────────────────────────┐
│                     Storage Layer                                │
│  ┌──────────────┐  ┌────────────────┐  ┌─────────────────────┐ │
│  │  Message DB  │  │ Reminder Queue │  │   Cursor Store      │ │
│  └──────────────┘  └────────────────┘  └─────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
          │
          ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Delivery Layer                                │
│  ┌──────────────────┐  ┌────────────────────────────────────┐  │
│  │ Reminder Scheduler│  │  Notifier (Lark Bot / TG Bot)     │  │
│  └──────────────────┘  └────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
          │
          ▼
┌─────────────────────────────────────────────────────────────────┐
│                   Presentation Layer                             │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │              Web Dashboard (FastAPI + HTML)                 │ │
│  └────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
```

## 快速开始

### 1. 环境准备

```bash
# 克隆仓库
git clone https://github.com/gdszyy/msg-reminder-hub.git
cd msg-reminder-hub

# 安装依赖
pip install -r requirements.txt

# 配置环境变量
cp .env.example .env
# 编辑 .env 填入你的 API Key 和凭证
```

### 2. 初始化数据库

```bash
python -m src.storage.db --init
```

### 3. 启动服务

```bash
# 启动 Web API + 定时任务
python main.py

# 或仅运行一次消息拉取（调试用）
python scripts/run_fetch.py --platform lark --dry-run
```

### 4. 访问 Web Dashboard

打开浏览器访问 `http://localhost:8000/dashboard`

## 环境变量说明

详见 [.env.example](.env.example)

| 变量 | 必填 | 说明 |
|------|------|------|
| `LLM_API_KEY` | ✅ | 大模型 API Key（支持 OpenAI 兼容接口） |
| `LLM_BASE_URL` | ✅ | 大模型 API Base URL |
| `LLM_MODEL` | ❌ | 模型名称，默认 `gpt-4.1-mini` |
| `LARK_APP_ID` | ✅ | 飞书应用 App ID |
| `LARK_APP_SECRET` | ✅ | 飞书应用 App Secret |
| `LARK_TARGET_USER_ID` | ✅ | 被监控用户的 open_id（识别与谁相关的消息） |
| `TG_API_ID` | ❤️ | Telegram API ID（从 my.telegram.org 获取） |
| `TG_API_HASH` | ❤️ | Telegram API Hash |
| `TG_PHONE` | ❤️ | 你的手机号（如 +8613800138000） |
| `DATABASE_URL` | ❌ | 数据库连接串，默认 SQLite |
| `REMINDER_INTERVAL_MINUTES` | ❌ | 提醒间隔（分钟），默认 30 |
| `FETCH_INTERVAL_MINUTES` | ❌ | 消息拉取间隔（分钟），默认 15 |

## 项目结构

```
msg-reminder-hub/
├── main.py                    # 主入口：Web API + 调度器
├── config/
│   └── settings.py            # 统一配置管理
├── src/
│   ├── ingestion/             # 接入层
│   │   ├── base.py            # 消息拉取抽象基类
│   │   ├── lark_fetcher.py    # Lark 消息拉取
│   │   └── tg_fetcher.py      # Telegram 消息拉取
│   ├── processing/            # 处理层
│   │   ├── normalizer.py      # 消息格式标准化
│   │   └── llm_analyzer.py    # LLM 意图识别
│   ├── storage/               # 存储层
│   │   ├── db.py              # 数据库模型与操作
│   │   └── cursor.py          # 游标管理
│   ├── delivery/              # 触达层
│   │   ├── scheduler.py       # 提醒调度器
│   │   └── notifier.py        # 通知发送
│   └── web/                   # 展示层
│       └── api.py             # Web Dashboard API
├── scripts/
│   ├── run_fetch.py           # 手动触发消息拉取
│   └── run_remind.py          # 手动触发提醒
├── tests/
└── requirements.txt
```

## 开发路线图

- [x] Phase 1: Lark 消息拉取 + LLM 识别 + 提醒
- [x] Phase 2: Telegram 接入
- [ ] Phase 3: WhatsApp 接入
- [ ] Phase 4: 多用户支持
- [ ] Phase 5: 移动端推送

## License

MIT
