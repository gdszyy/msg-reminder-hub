#!/usr/bin/env python3
"""
手动触发消息拉取
================
用于调试和测试，手动执行一次消息拉取周期。

用法：
  python scripts/run_fetch.py                    # 拉取所有平台
  python scripts/run_fetch.py --platform lark    # 仅拉取飞书
  python scripts/run_fetch.py --platform telegram # 仅拉取 Telegram
  python scripts/run_fetch.py --dry-run          # 预览模式（不写入数据库）
"""

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import settings
from src.storage.db import init_db
from main import run_fetch_cycle

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def main():
    parser = argparse.ArgumentParser(description="手动触发消息拉取")
    parser.add_argument("--platform", choices=["lark", "telegram", "all"], default="all")
    parser.add_argument("--dry-run", action="store_true", help="预览模式")
    args = parser.parse_args()

    print("=" * 60)
    print("MSG Reminder Hub - 手动消息拉取")
    print("=" * 60)

    # 初始化数据库
    init_db()

    if args.dry_run:
        print("⚠️  DRY RUN 模式：不写入数据库")
        # TODO: 实现 dry-run 逻辑
        return

    result = run_fetch_cycle()
    print(f"\n✅ 拉取完成:")
    print(f"   新消息: {result.get('messages', 0)} 条")
    print(f"   新提醒: {result.get('reminders', 0)} 条")


if __name__ == "__main__":
    main()
