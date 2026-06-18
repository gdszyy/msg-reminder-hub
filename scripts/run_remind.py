#!/usr/bin/env python3
"""
手动触发提醒发送
================
用于调试和测试，手动执行一次提醒发送周期。

用法：
  python scripts/run_remind.py              # 执行提醒
  python scripts/run_remind.py --dry-run    # 预览模式（不发送消息）
"""

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import settings
from src.storage.db import init_db
from src.delivery.scheduler import run_reminder_cycle

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def main():
    parser = argparse.ArgumentParser(description="手动触发提醒发送")
    parser.add_argument("--dry-run", action="store_true", help="预览模式")
    args = parser.parse_args()

    print("=" * 60)
    print("MSG Reminder Hub - 手动提醒发送")
    print("=" * 60)

    init_db()

    if args.dry_run:
        print("⚠️  DRY RUN 模式：不发送消息")
        # TODO: 实现 dry-run 逻辑
        return

    result = run_reminder_cycle()
    print(f"\n✅ 提醒完成:")
    print(f"   总待提醒: {result.get('total', 0)} 条")
    print(f"   已发送: {result.get('sent', 0)} 条")
    print(f"   已跳过: {result.get('skipped', 0)} 条")


if __name__ == "__main__":
    main()
