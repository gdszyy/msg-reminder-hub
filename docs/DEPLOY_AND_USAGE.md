# MSG Reminder Hub — 部署指南 & 功能范围 & 使用说明

---

## 一、功能范围

### 1.1 系统定位

自动扫描你散落在多个平台的对话消息，通过大语言模型识别"需要你回复"的问题，集中提醒你去处理。同时提供"近期动态"视图，让你快速了解各群在聊什么。

### 1.2 当前已实现功能

| 模块 | 功能 | 说明 |
|------|------|------|
| **Lark 消息拉取** | 游标驱动增量拉取 | 基于上次截止时间戳，从游标拉到当前，不重不漏 |
| | Webhook 实时接收 | 飞书事件订阅，消息到达即刻处理 |
| | 用户名解析 | open_id → 真实姓名（带缓存 + 批量查询） |
| | 群组名缓存 | chat_id → 群名（减少 API 调用） |
| | @提及检测 | 识别消息中是否 @了你 |
| | 消息去重 | Webhook 重复推送 + DB 层 platform+msg_id 双重去重 |
| **AI 识别** | 规则预筛选 | 直接 @你 的消息无需 LLM 即判定为 high |
| | LLM 深度分析 | 批量分析消息，判断是否需要你回复 |
| | 上下文窗口 | 带入前后消息提高判断准确度 |
| | 紧急程度分级 | high / medium / low 三级 |
| **提醒服务** | 定时聚合提醒 | 每 N 分钟汇总一次，发送飞书卡片到你的私聊 |
| | 冷却机制 | 同一条消息不会反复提醒 |
| | 优先级排序 | 高优先级消息排在前面 |
| **Web Dashboard** | 提醒列表 | 查看所有待回复/已处理的消息 |
| | 近期动态 | 按群组查看所有最新消息（不限于与你相关的） |
| | 消息上下文 | 点击查看完整对话上下文 |
| | 手动操作 | 立即拉取、立即提醒、标记已回复/忽略 |
| | 统计面板 | 待回复数、回复率等 |
| **Telegram** | User API 拉取 | Telethon MTProto，扫描你账号所有对话（已实现，暂缓启用） |

### 1.3 工作流程

```
定时任务（每 FETCH_INTERVAL_MINUTES 分钟触发一次）:
  ┌─────────────────────────────────────────────────────┐
  │ 1. 遍历所有监控群组                                    │
  │ 2. 读取该群的游标（上次截止时间戳）                      │
  │ 3. 从游标 → 当前时间，拉完所有新消息（分页循环）          │
  │ 4. 标准化 + 过滤系统消息                               │
  │ 5. 批量解析发送者姓名                                  │
  │ 6. 入库（messages 表，platform+msg_id 去重）           │
  │ 7. 规则预筛选（@你 → 直接 high）                       │
  │ 8. LLM 深度分析剩余消息                                │
  │ 9. 创建提醒记录（reminders 表）                        │
  │ 10. 更新游标为最后一条消息的时间戳                       │
  └─────────────────────────────────────────────────────┘

定时任务（每 REMINDER_INTERVAL_MINUTES 分钟触发一次）:
  ┌─────────────────────────────────────────────────────┐
  │ 1. 扫描 pending 状态的提醒                             │
  │ 2. 过滤冷却期内的（避免重复打扰）                        │
  │ 3. 按紧急程度排序                                      │
  │ 4. 聚合为一张飞书卡片发送到你的私聊                      │
  │ 5. 更新状态为 reminded                                │
  └─────────────────────────────────────────────────────┘
```

### 1.4 数据源说明

"需要你回复"的提醒和"近期动态"共用同一份数据源（messages 表），区别只是视图：
- **提醒视图**：经过 AI 筛选，只展示需要你回复的
- **近期动态**：展示所有拉到的消息，按群组分组

---

## 二、Railway 部署指南

### 2.1 前置准备

1. **飞书应用**：在 [Lark Developer](https://open.larksuite.com) 或 [飞书开放平台](https://open.feishu.cn) 创建企业自建应用
2. **飞书权限**：开通以下权限并发布应用版本
   - `im:message:readonly` — 读取消息
   - `im:chat:readonly` — 读取群组信息
   - `im:message` — 发送消息
   - `contact:user.base:readonly` — 读取用户基本信息
3. **LLM API Key**：准备一个 DeepSeek API Key（或其他 OpenAI 兼容服务）
4. **Railway 账号**：注册 [Railway](https://railway.app)

### 2.2 Railway 部署步骤

1. Railway Dashboard → **New Project** → **Deploy from GitHub repo** → 选择 `gdszyy/msg-reminder-hub`
2. 新建 MySQL 服务（见 2.3）
3. 配置环境变量（见 2.4）
4. 等待部署完成，获取公网域名
5. （可选）配置飞书 Webhook 回调地址：`https://你的域名/lark/webhook`

### 2.3 Railway 新建 MySQL 数据库（必做）

1. 打开你的 Railway Project Dashboard
2. 点击右上角 **+ New** 按钮
3. 选择 **Database** → **MySQL**
4. 等待 10~20 秒，MySQL 服务自动创建完成
5. 点击新建的 MySQL 服务 → **Variables** 页签
6. 你会看到这些自动生成的变量：
   - `MYSQLUSER` = root
   - `MYSQLPASSWORD` = xxxxxx
   - `MYSQLHOST` = mysql.railway.internal
   - `MYSQLPORT` = 3306
   - `MYSQLDATABASE` = railway
   - `MYSQL_URL` = mysql://root:xxxxxx@mysql.railway.internal:3306/railway
7. 回到你的应用服务，在 Variables 中配置 `DATABASE_URL`（见下方）

应用启动时会自动创建表结构（`messages` / `reminders` / `cursors`），无需手动建表。

### 2.4 Railway Variables 全量配置（Raw 格式）

直接复制到 Railway 的 **Variables → Raw Editor**：

```env
# ===== LLM 配置 (DeepSeek) =====
LLM_API_KEY=sk-你的deepseek密钥
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat

# ===== Lark 应用凭证 =====
LARK_APP_ID=cli_你的app_id
LARK_APP_SECRET=你的app_secret
LARK_DOMAIN_TYPE=lark
LARK_TARGET_USER_ID=ou_你的open_id

# ===== 监控范围（留空=监控所有已加入的群） =====
LARK_MONITORED_CHATS=

# ===== 数据库（必须用 MySQL） =====
DATABASE_URL=${{MySQL-4d_X.MYSQL_URL}}

# ===== 调度配置 =====
FETCH_INTERVAL_MINUTES=15
REMINDER_INTERVAL_MINUTES=30
FETCH_COLD_START_HOURS=24
REMINDER_MAX_PER_BATCH=10
REMINDER_COOLDOWN_HOURS=4

# ===== Web 服务 =====
WEB_HOST=0.0.0.0
WEB_PORT=8000

# ===== 日志 =====
LOG_LEVEL=INFO
```

### 2.5 变量说明

| 变量 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `LLM_API_KEY` | 是 | — | DeepSeek / OpenAI 兼容的 API Key |
| `LLM_BASE_URL` | 是 | — | LLM 服务地址 |
| `LLM_MODEL` | 否 | `qwen-plus` | 模型名称 |
| `LARK_APP_ID` | 是 | — | 飞书应用 App ID |
| `LARK_APP_SECRET` | 是 | — | 飞书应用 App Secret |
| `LARK_DOMAIN_TYPE` | 否 | `lark` | `lark`(海外) 或 `feishu`(国内) |
| `LARK_TARGET_USER_ID` | 是 | — | 你的 open_id，用于识别与你相关的消息 |
| `LARK_MONITORED_CHATS` | 否 | 空(全部) | 监控的群 chat_id 列表，逗号分隔 |
| `DATABASE_URL` | 是 | SQLite | MySQL 连接串，支持 `mysql://` 和 `mysql+pymysql://` |
| `FETCH_INTERVAL_MINUTES` | 否 | `15` | 定时拉取任务的调用间隔（分钟） |
| `REMINDER_INTERVAL_MINUTES` | 否 | `30` | 定时提醒任务的调用间隔（分钟） |
| `FETCH_COLD_START_HOURS` | 否 | `24` | 首次运行时往前追溯多久（小时） |
| `REMINDER_MAX_PER_BATCH` | 否 | `10` | 单次提醒最大条数 |
| `REMINDER_COOLDOWN_HOURS` | 否 | `4` | 同一条消息两次提醒的最小间隔（小时） |
| `WEB_HOST` | 否 | `0.0.0.0` | Web 服务监听地址 |
| `WEB_PORT` | 否 | `8000` | Web 服务端口 |
| `LOG_LEVEL` | 否 | `INFO` | 日志级别 |

### 2.6 DATABASE_URL 配置注意事项

| 写法 | 是否正确 | 说明 |
|------|----------|------|
| `${{MySQL-4d_X.MYSQL_URL}}` | ✅ | Railway 变量引用，值为完整连接串 |
| `mysql://root:xxx@host:3306/railway` | ✅ | 直接填写，代码会自动转为 pymysql 驱动 |
| `mysql+pymysql://root:xxx@host:3306/railway` | ✅ | 直接填写，已带驱动前缀 |
| `${{MySQL-4d_X.MYSQL_DATABASE}}` | ❌ | 这只是数据库名(如"railway")，不是连接串 |
| `${{MySQL-4d_X.MYSQLDATABASE}}` | ❌ | 同上 |

代码会自动处理：`mysql://` → `mysql+pymysql://`，并追加 `charset=utf8mb4`。

### 2.7 各 LLM 服务的 Base URL 参考

| 服务商 | LLM_BASE_URL | 推荐模型 |
|--------|--------------|----------|
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4.1-mini` |
| 智谱 AI | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-flash` |
| Moonshot | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` |

### 2.8 获取 LARK_TARGET_USER_ID

这是你自己的飞书 open_id（以 `ou_` 开头的字符串），系统用它来判断"哪些消息是跟你相关的"。

**获取方式（任选其一）：**

**方法 1：通过飞书开放平台 API 调试器**
1. 登录 [Lark 开放平台](https://open.larksuite.com) 或 [飞书开放平台](https://open.feishu.cn)
2. 进入你的应用 → 左侧菜单「API 调试器」
3. 搜索并调用 `GET /open-apis/authen/v1/user_info`（需先授权登录）
4. 返回结果中的 `open_id` 字段就是你的 ID

**方法 2：通过机器人日志获取**
1. 部署服务后，在任意群里 @机器人 发一条消息
2. 查看 Railway Logs，找到日志中的 `sender_id=ou_xxxxxxxx`
3. 那个 `ou_` 开头的字符串就是你的 open_id

**方法 3：通过管理后台**
1. 飞书管理后台 → 组织架构 → 搜索你的名字
2. 点击你的名字进入详情页
3. URL 中或详情页中可以看到 open_id

**方法 4：curl 命令行**
```bash
# 先获取 token
TOKEN=$(curl -s -X POST 'https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal' \
  -H 'Content-Type: application/json' \
  -d '{"app_id":"你的APP_ID","app_secret":"你的APP_SECRET"}' | jq -r '.tenant_access_token')

# 用 token 查询你的信息（通过手机号或邮箱）
curl -s 'https://open.larksuite.com/open-apis/contact/v3/users/batch_get_id' \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"mobiles":["+你的手机号"]}' | jq '.data.user_list[0].user_id'
```

> open_id 格式示例：`ou_d06d8df64bc40ed44f8e8df3f4be3403`

### 2.9 配置飞书 Webhook（可选，启用实时模式）

定时拉取已经能覆盖所有消息。如果你还想要"实时提醒"（消息到达秒级响应），需要配置 Webhook：

1. Railway 部署成功后，获取公网域名（如 `msg-reminder-hub-production.up.railway.app`）
2. 飞书开放平台 → 你的应用 → 事件与回调 → 事件订阅
3. 请求地址填入：`https://你的域名/lark/webhook`
4. 添加事件：`im.message.receive_v1`（接收消息）
5. 保存并验证

---

## 三、使用说明

### 3.1 部署后的日常使用

部署完成后，系统全自动运行：

1. **每 15 分钟**（可配置）自动从游标位置拉取所有新消息
2. **AI 自动判断**哪些消息需要你回复
3. **每 30 分钟**（可配置）通过飞书私聊给你发一张聚合提醒卡片

你收到提醒后：
- 点击卡片中的链接跳转到原消息回复
- 或打开 Web Dashboard 查看完整上下文

### 3.2 Web Dashboard

访问 `https://你的域名/dashboard`

**功能区域：**

| 区域 | 说明 |
|------|------|
| 统计面板 | 待回复数、已提醒数、已处理数、回复率 |
| 筛选栏 | 按状态/平台筛选 + 手动操作按钮 |
| 提醒列表 | 按时间倒序展示需要你回复的消息 |
| 近期动态 | 按群组展示所有最新消息（不限于与你相关的） |

**操作按钮：**

| 按钮 | 功能 |
|------|------|
| ⚡ 立即拉取 | 手动触发一次消息拉取（不等定时任务） |
| 🔔 立即提醒 | 手动触发一次提醒发送 |
| 📰 近期动态 | 切换到近期动态视图，按群组查看所有消息 |
| 已回复 | 标记该提醒为已处理 |
| 忽略 | 标记该提醒为已忽略 |

### 3.3 API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/dashboard` | Web Dashboard 页面 |
| GET | `/api/reminders?status=pending&platform=lark&limit=50` | 获取提醒列表 |
| POST | `/api/reminders/{id}/reply` | 标记为已回复 |
| POST | `/api/reminders/{id}/ignore` | 标记为已忽略 |
| GET | `/api/messages/{id}/context?window=10` | 获取消息上下文 |
| GET | `/api/feed?hours=24&limit=100` | 获取近期动态（按群组分组） |
| GET | `/api/stats` | 统计数据 |
| POST | `/api/fetch` | 手动触发消息拉取 |
| POST | `/api/remind` | 手动触发提醒发送 |
| GET | `/health` | 健康检查 |
| POST | `/lark/webhook` | 飞书事件回调 |
| GET | `/docs` | Swagger API 文档（自动生成） |

### 3.4 提醒卡片示例

你会在飞书私聊中收到类似这样的卡片：

```
┌─────────────────────────────────────────┐
│  📋 消息待回复提醒                        │
├─────────────────────────────────────────┤
│  📬 你有 3 条消息待回复                   │
│─────────────────────────────────────────│
│  🔴 1. [飞书] 前端技术群                  │
│     来自 张三: 支付页面的设计稿你看了吗？    │
│                                         │
│  🟡 2. [飞书] 产品讨论群                  │
│     来自 李四: 这个需求的优先级你怎么看？    │
│                                         │
│  🟢 3. [飞书] 后端架构群                  │
│     来自 王五: 数据库迁移方案已出，请过目    │
│─────────────────────────────────────────│
│  💡 回复「已处理」可标记消息为已读          │
└─────────────────────────────────────────┘
```

### 3.5 调参建议

| 场景 | 建议配置 |
|------|----------|
| 消息量大，不想被频繁打扰 | `FETCH_INTERVAL_MINUTES=30`, `REMINDER_INTERVAL_MINUTES=60`, `REMINDER_COOLDOWN_HOURS=8` |
| 消息量小，想及时响应 | `FETCH_INTERVAL_MINUTES=5`, `REMINDER_INTERVAL_MINUTES=15`, `REMINDER_COOLDOWN_HOURS=2` |
| 只关注直接 @我 的消息 | 配合 Webhook 使用，规则预筛选会自动捕获 |
| 想省 LLM 费用 | 用 `deepseek-chat`，比 GPT-4 便宜 10 倍以上 |

### 3.6 游标机制说明

| 概念 | 说明 |
|------|------|
| 游标是什么 | 每个群组的"上次拉取截止时间戳"，存在 `cursors` 表中 |
| 取数逻辑 | 每次从游标 → 当前时间，拉完所有新消息 |
| 首次运行 | 游标为空，从 `FETCH_COLD_START_HOURS` 小时前开始（冷启动） |
| 后续运行 | 从上次游标继续，不管中间隔了多久 |
| 去重保障 | DB 层按 platform+msg_id 唯一索引去重 |
| 间隔配置 | 只控制"多久跑一次任务"，不影响取数范围 |

---

## 四、故障排查

| 问题 | 可能原因 | 解决方案 |
|------|----------|----------|
| 拉取到 0 条消息 | 游标之后确实没有新消息 | 点「⚡ 立即拉取」重试；检查 `FETCH_COLD_START_HOURS` |
| SQLAlchemy URL 解析失败 | `DATABASE_URL` 配置了 `MYSQL_DATABASE`（纯数据库名） | 改为 `MYSQL_URL`（完整连接串） |
| 拉取到消息但无提醒 | LLM 判定不需要回复 | 检查 `LLM_API_KEY` 是否正确 |
| 提醒发不出去 | `LARK_TARGET_USER_ID` 错误 | 确认 open_id 正确；检查 `im:message` 发送权限 |
| Webhook 验证失败 | URL 不正确 | 确认 Railway 域名 + `/lark/webhook` 路径 |
| 群消息权限受限 (code=230050) | 飞书应用未加入该群 | 在群设置中添加机器人 |
| LLM 调用超时 | 网络问题 | Railway 部署在海外，用 DeepSeek/OpenAI 延迟较低 |

---

## 五、安全说明

1. **所有凭证通过环境变量注入**，不会出现在代码中
2. **飞书 Token 自动刷新**，无需手动维护
3. **消息数据存储在你自己的 MySQL 中**，不会外传
4. **LLM 调用仅发送消息文本**，不发送文件/图片等附件
5. **Web Dashboard 建议配置 `WEB_API_KEY`** 做基础认证
