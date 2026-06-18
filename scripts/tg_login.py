#!/usr/bin/env python3
"""
Telegram User API 首次登录
===========================
首次使用 Telethon 需要输入验证码完成登录。
运行此脚本完成交互式登录，之后 session 文件会保存登录状态，
主服务启动时无需再次验证。

用法：
  python scripts/tg_login.py

前置条件：
  1. 前往 https://my.telegram.org 创建应用，获取 api_id 和 api_hash
  2. 在 .env 中配置：
     TG_API_ID=你的api_id
     TG_API_HASH=你的api_hash
     TG_PHONE=+你的手机号

登录流程：
  1. 脚本会向你的 Telegram 客户端发送验证码
  2. 输入验证码完成登录
  3. 如果开启了两步验证，还需要输入密码
  4. 登录成功后 session 文件保存在 data/ 目录下
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
except ImportError:
    pass

from telethon import TelegramClient

# 配置
API_ID = os.environ.get("TG_API_ID", "")
API_HASH = os.environ.get("TG_API_HASH", "")
PHONE = os.environ.get("TG_PHONE", "")
SESSION_NAME = os.environ.get("TG_SESSION_NAME", "tg_user_session")
SESSION_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


async def main():
    print("=" * 60)
    print("Telegram User API 登录")
    print("=" * 60)

    if not API_ID or not API_HASH:
        print("\n❌ 错误: TG_API_ID 和 TG_API_HASH 未配置")
        print("   请前往 https://my.telegram.org 创建应用并获取凭证")
        print("   然后在 .env 文件中配置:")
        print("     TG_API_ID=你的api_id")
        print("     TG_API_HASH=你的api_hash")
        return

    if not PHONE:
        print("\n❌ 错误: TG_PHONE 未配置")
        print("   请在 .env 文件中配置你的手机号:")
        print("     TG_PHONE=+8613800138000")
        return

    print(f"\n📱 手机号: {PHONE}")
    print(f"📂 Session 保存位置: {SESSION_DIR}/{SESSION_NAME}.session")

    os.makedirs(SESSION_DIR, exist_ok=True)
    session_path = os.path.join(SESSION_DIR, SESSION_NAME)

    client = TelegramClient(session_path, int(API_ID), API_HASH)

    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"\n✅ 已登录: @{me.username} ({me.first_name} {me.last_name or ''})")
        print("   Session 文件有效，无需重新登录。")
    else:
        print("\n📤 正在发送验证码...")
        await client.send_code_request(PHONE)

        code = input("\n🔑 请输入收到的验证码: ").strip()

        try:
            await client.sign_in(PHONE, code)
        except Exception as e:
            if "Two-steps verification" in str(e) or "password" in str(e).lower():
                password = input("🔐 请输入两步验证密码: ").strip()
                await client.sign_in(password=password)
            else:
                raise

        me = await client.get_me()
        print(f"\n✅ 登录成功!")
        print(f"   用户: @{me.username} ({me.first_name} {me.last_name or ''})")
        print(f"   User ID: {me.id}")

    # 显示最近的对话
    print("\n📋 最近的对话:")
    print("-" * 40)
    count = 0
    async for dialog in client.iter_dialogs(limit=20):
        count += 1
        unread = f" ({dialog.unread_count} 未读)" if dialog.unread_count else ""
        print(f"  {count:2d}. [{dialog.id}] {dialog.name}{unread}")

    print(f"\n💡 提示: 将需要监控的对话 ID 添加到 .env 的 TG_MONITORED_CHATS 中")
    print(f"   例如: TG_MONITORED_CHATS={','.join(str(d.id) for i, d in zip(range(3), client.iter_dialogs()))}")

    await client.disconnect()
    print("\n✅ 登录完成，session 已保存。主服务启动时将自动使用此 session。")


if __name__ == "__main__":
    asyncio.run(main())
