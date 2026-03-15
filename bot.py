#!/usr/bin/env python3
"""
Claude Code to Claw — Telegram Bot
支持 Topic 模式（Forum）和普通私聊
每个 topic/chat 一个独立的 Claude Code session
"""

import asyncio
import logging
import os
import time

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
            "/status - 查看当前 session\n"
            "/reset - 重置当前 session\n"
            "/attach [session_id] [cwd] - 接管 CLI session\n"
            "/detach - 分离 session（可在 CLI resume）\n"
            "/sessions - 列出所有活跃 session"
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
            f"Full ID: {bridge.session_id}\n"
            f"状态: {'处理中' if bridge._is_busy else '空闲'}\n"
            f"队列: {bridge._pending.qsize()} 条待处理\n"
            f"工作目录: {bridge.cwd}"
        )
    else:
        status = "无活跃 session。发送消息以创建。"

    kwargs = {"chat_id": chat_id, "text": status}
    if topic_id:
        kwargs["message_thread_id"] = topic_id
    await context.bot.send_message(**kwargs)


async def cmd_attach(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    接管一个 CLI session。
    用法:
      /attach <session_id>           — resume 指定 session
      /attach <session_id> /path/to  — 指定工作目录
    """
    chat_id = update.effective_chat.id
    topic_id = getattr(update.message, "message_thread_id", None)
    key = _session_key(update)
    loop = asyncio.get_event_loop()

    args = context.args or []
    if not args:
        # 没参数，列出可用 sessions
        cli_sessions = sessions.list_cli_sessions(limit=10)
        if not cli_sessions:
            text = "没有找到可用的 CLI session。"
        else:
            lines = ["可用的 CLI sessions:\n"]
            for s in cli_sessions:
                ts = time.strftime("%m-%d %H:%M", time.localtime(s["modified"]))
                summary = s["summary"][:40] if s["summary"] else "(空)"
                lines.append(
                    f"`{s['session_id'][:8]}` {ts} {s['size_kb']}KB\n"
                    f"  项目: {s['project']}\n"
                    f"  最后: {summary}"
                )
            lines.append("\n用法: /attach <session_id> [工作目录]")
            text = "\n".join(lines)

        kwargs = {"chat_id": chat_id, "text": text}
        if topic_id:
            kwargs["message_thread_id"] = topic_id
        await context.bot.send_message(**kwargs)
        return

    session_id = args[0]
    # 支持短 ID — 前缀匹配
    if len(session_id) < 36:
        cli_sessions = sessions.list_cli_sessions(limit=50)
        matches = [s for s in cli_sessions if s["session_id"].startswith(session_id)]
        if len(matches) == 1:
            session_id = matches[0]["session_id"]
        elif len(matches) > 1:
            text = f"多个 session 匹配 '{session_id}':\n"
            for s in matches[:5]:
                text += f"  {s['session_id'][:12]}... ({s['project']})\n"
            text += "\n请提供更长的 ID。"
            kwargs = {"chat_id": chat_id, "text": text}
            if topic_id:
                kwargs["message_thread_id"] = topic_id
            await context.bot.send_message(**kwargs)
            return
        elif not matches:
            kwargs = {"chat_id": chat_id, "text": f"未找到匹配 '{session_id}' 的 session。"}
            if topic_id:
                kwargs["message_thread_id"] = topic_id
            await context.bot.send_message(**kwargs)
            return

    cwd = args[1] if len(args) > 1 else None

    def on_response(response_text: str):
        async def _send():
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
        bridge = sessions.attach(key, session_id, on_response=on_response, cwd=cwd)
        text = (
            f"已接管 session: {bridge.session_id[:8]}...\n"
            f"Full ID: {bridge.session_id}\n"
            f"工作目录: {bridge.cwd}\n"
            f"现在可以直接发消息继续对话。"
        )
    except RuntimeError as e:
        text = f"接管失败: {e}"

    kwargs = {"chat_id": chat_id, "text": text}
    if topic_id:
        kwargs["message_thread_id"] = topic_id
    await context.bot.send_message(**kwargs)


async def cmd_detach(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """分离当前 session（进程停止，但 session 数据保留，可以在 CLI 里 resume）"""
    key = _session_key(update)
    chat_id = update.effective_chat.id
    topic_id = getattr(update.message, "message_thread_id", None)

    sid = sessions.detach(key)
    if sid:
        text = (
            f"Session 已分离: {sid[:8]}...\n"
            f"Full ID: {sid}\n"
            f"你可以在 CLI 里 resume:\n"
            f"  claude --resume {sid}"
        )
    else:
        text = "当前没有活跃的 session。"

    kwargs = {"chat_id": chat_id, "text": text}
    if topic_id:
        kwargs["message_thread_id"] = topic_id
    await context.bot.send_message(**kwargs)


async def cmd_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """列出所有活跃的 bot session"""
    chat_id = update.effective_chat.id
    topic_id = getattr(update.message, "message_thread_id", None)

    active = sessions.list_sessions()
    if not active:
        text = "没有活跃的 session。"
    else:
        lines = [f"活跃 sessions ({len(active)}):\n"]
        for s in active:
            status = "处理中" if s["busy"] else "空闲"
            pending = f" +{s['pending']}待处理" if s["pending"] else ""
            lines.append(
                f"• {s['key']}\n"
                f"  ID: {s['session_id'][:8]}... "
                f"[{status}{pending}]"
            )
        text = "\n".join(lines)

    kwargs = {"chat_id": chat_id, "text": text}
    if topic_id:
        kwargs["message_thread_id"] = topic_id
    await context.bot.send_message(**kwargs)


def main():
    logger.info(f"工作目录: {WORK_DIR}")
    logger.info("启动 Telegram Bot...")

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("attach", cmd_attach))
    app.add_handler(CommandHandler("detach", cmd_detach))
    app.add_handler(CommandHandler("sessions", cmd_sessions))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot 已就绪，开始 polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
