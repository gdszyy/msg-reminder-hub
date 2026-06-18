"""
统一配置管理
============
从环境变量加载所有配置项，提供类型安全的访问方式。
"""

import os
from pathlib import Path
from typing import List, Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ---------------------------------------------------------------------------
# 路径配置
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# LLM 配置
# ---------------------------------------------------------------------------

LLM_API_KEY: str = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL: str = os.environ.get("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
LLM_MODEL: str = os.environ.get("LLM_MODEL", "qwen-plus")

# ---------------------------------------------------------------------------
# Lark 配置
# ---------------------------------------------------------------------------

LARK_APP_ID: str = os.environ.get("LARK_APP_ID", "")
LARK_APP_SECRET: str = os.environ.get("LARK_APP_SECRET", "")
LARK_DOMAIN_TYPE: str = os.environ.get("LARK_DOMAIN_TYPE", "lark")
LARK_TARGET_USER_ID: str = os.environ.get("LARK_TARGET_USER_ID", "")

def _parse_list(val: str) -> List[str]:
    """解析逗号分隔的列表"""
    return [x.strip() for x in val.split(",") if x.strip()]

LARK_MONITORED_CHATS: List[str] = _parse_list(os.environ.get("LARK_MONITORED_CHATS", ""))

@property
def lark_base_url() -> str:
    if LARK_DOMAIN_TYPE == "feishu":
        return "https://open.feishu.cn"
    return "https://open.larksuite.com"

LARK_BASE_URL: str = (
    "https://open.feishu.cn" if LARK_DOMAIN_TYPE == "feishu"
    else "https://open.larksuite.com"
)

# ---------------------------------------------------------------------------
# Telegram 配置
# ---------------------------------------------------------------------------

TG_BOT_TOKEN: str = os.environ.get("TG_BOT_TOKEN", "")
TG_TARGET_USER_ID: str = os.environ.get("TG_TARGET_USER_ID", "")
TG_MONITORED_CHATS: List[str] = _parse_list(os.environ.get("TG_MONITORED_CHATS", ""))

# ---------------------------------------------------------------------------
# 数据库配置
# ---------------------------------------------------------------------------

DATABASE_URL: str = os.environ.get("DATABASE_URL", f"sqlite:///{DATA_DIR / 'msg_reminder.db'}")

# ---------------------------------------------------------------------------
# 调度配置
# ---------------------------------------------------------------------------

FETCH_INTERVAL_MINUTES: int = int(os.environ.get("FETCH_INTERVAL_MINUTES", "15"))
REMINDER_INTERVAL_MINUTES: int = int(os.environ.get("REMINDER_INTERVAL_MINUTES", "30"))
REMINDER_MAX_PER_BATCH: int = int(os.environ.get("REMINDER_MAX_PER_BATCH", "10"))
REMINDER_COOLDOWN_HOURS: int = int(os.environ.get("REMINDER_COOLDOWN_HOURS", "4"))

# ---------------------------------------------------------------------------
# Web 配置
# ---------------------------------------------------------------------------

WEB_HOST: str = os.environ.get("WEB_HOST", "0.0.0.0")
WEB_PORT: int = int(os.environ.get("WEB_PORT", "8000"))
WEB_API_KEY: str = os.environ.get("WEB_API_KEY", "")

# ---------------------------------------------------------------------------
# 日志配置
# ---------------------------------------------------------------------------

LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")
