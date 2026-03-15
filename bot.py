#!/usr/bin/env python3
"""
Claude Code to Claw — Telegram Bot
支持 Topic 模式（Forum）和普通私聊
每个 topic/chat 一个独立的 Claude Code session
"""

import asyncio
import logging
import os
import html

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    filters,
)

from claude_bridge import SessionManager

load_dotenv()

logging.basicConfig(
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("claw")

# --- 全局 ---
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
WORK_DIR = os.environ.get("CLAUDE_WORK_DIR", os.getcwd())
sessions = SessionManager(base_cwd=WORK_DIR)


def _session_key(update: Update) -> str:
    """
    生成 session key：
    - Topic 模式: chat_id + topic_id → 每个 topic 独立 session
    - 普通聊天: chat_id → 每个聊天独立 session
    """
    chat_id = update.effective_chat.id
    topic_id = getattr(update.message, "message_thread_id", None)
    if topic_id:
        return f"tg:{chat_id}:{topic_id}"
    return f"tg:{chat_id}"


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理用户消息"""
    text = update.message.text
    if not text:
        return

    chat_id = update.effective_chat.id
    topic_id = getattr(update.message, "message_thread_id", None)
    key = _session_key(update)
    loop = asyncio.get_event_loop()

    logger.info(f"[{key}] 收到: {text[:80]}")

    # 回调：Claude 回复时发到 Telegram
    def on_response(response_text: str):
        async def _send():
            # 分段发送（Telegram 单条消息限制 4096 字符）
            for i in range(0, len(response_text), 4000):
                chunk = response_text[i:i + 4000]
                kwargs = {"chat_id": chat_id, "text": chunk}
                if topic_id:
                    kwargs["message_thread_id"] = topic_id
                try:
                    await context.bot.send_message(**kwargs)
                except Exception as e:
                    logger.error(f"发送失败: {e}")
        asyncio.run_coroutine_threadsafe(_send(), loop)

    try:
        bridge = sessions.get_or_create(key, on_response=on_response)
        bridge.send(text)
    except RuntimeError as e:
        kwargs = {"chat_id": chat_id, "text": f"Claude Code 启动失败: {e}"}
        if topic_id:
            kwargs["message_thread_id"] = topic_id
        await context.bot.send_message(**kwargs)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    topic_id = getattr(update.message, "message_thread_id", None)
    kwargs = {
        "chat_id": chat_id,
        "text": (
            "Claude Code to Claw\n\n"
            "直接发消息即可与 Claude Code 对话。\n"
            "支持 Topic 模式 — 每个 topic 独立 session。\n\n"
            "命令:\n"
            "/start - 显示帮助\n"
            "/reset - 重置当前 session\n"
            "/status - 查看 session 状态"
        ),
    }
    if topic_id:
        kwargs["message_thread_id"] = topic_id
    await context.bot.send_message(**kwargs)


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """重置当前 session（杀掉进程，下次发消息会新建）"""
    key = _session_key(update)
    chat_id = update.effective_chat.id
    topic_id = getattr(update.message, "message_thread_id", None)

    with sessions._lock:
        if key in sessions._sessions:
            sessions._sessions[key].stop()
            del sessions._sessions[key]

    kwargs = {"chat_id": chat_id, "text": "Session 已重置。下条消息将创建新 session。"}
    if topic_id:
        kwargs["message_thread_id"] = topic_id
    await context.bot.send_message(**kwargs)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = _session_key(update)
    chat_id = update.effective_chat.id
    topic_id = getattr(update.message, "message_thread_id", None)

    with sessions._lock:
        bridge = sessions._sessions.get(key)

    if bridge and bridge.is_alive:
        status = (
            f"Session: {bridge.session_id[:8]}...\n"
            f"状态: {'处理中' if bridge._is_busy else '空闲'}\n"
            f"队列: {bridge._pending.qsize()} 条待处理"
        )
    else:
        status = "无活跃 session。发送消息以创建。"

    kwargs = {"chat_id": chat_id, "text": status}
    if topic_id:
        kwargs["message_thread_id"] = topic_id
    await context.bot.send_message(**kwargs)


def main():
    logger.info(f"工作目录: {WORK_DIR}")
    logger.info("启动 Telegram Bot...")

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot 已就绪，开始 polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
