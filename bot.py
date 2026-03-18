#!/usr/bin/env python3
"""
Claude Code to Claw — Telegram Bot
支持私聊、私聊 Topic、群组、群组 Forum Topic
每个 chat/topic 一个独立的 Claude Code session
"""

import asyncio
import logging
import os
import time

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegramify_markdown import markdownify
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    filters,
)

from claude_bridge import SessionManager
from scheduler import Scheduler

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
scheduler = Scheduler()
BOT_USERNAME: str = ""  # 启动时从 getMe 获取
_bot_instance = None  # 存 bot 实例，给 cron 回调用

# 每个 session key 的 verbose 开关（工具反馈）
_verbose_keys: set[str] = set()

# Typing indicator — 持续发送直到 turn 完成
_typing_tasks: dict[str, asyncio.Task] = {}


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


def _extract_text(update: Update) -> str | None:
    """
    提取用户消息文本。
    - 私聊: 直接返回
    - 群组 forum topic: 直接返回（每个 topic 独立，不需要 @mention）
    - 群组非 topic: 需要 @mention 或 reply bot，剥离 @username
    """
    text = update.message.text
    if not text:
        return None

    chat_type = update.effective_chat.type

    # 私聊（包括私聊 topic）: 直接用
    if chat_type == "private":
        return text

    # 群组/超级群组
    topic_id = getattr(update.message, "message_thread_id", None)
    is_forum = getattr(update.effective_chat, "is_forum", False)

    if is_forum and topic_id:
        # Forum topic 模式: 每个 topic 是独立对话，不需要 @mention
        # 剥离可能存在的 @mention（有些用户习惯性 @）
        return _strip_mention(text)

    # 普通群组: 需要 @mention 或 reply 才响应
    # 检查是否 reply 了 bot 的消息
    reply = update.message.reply_to_message
    if reply and reply.from_user and reply.from_user.username == BOT_USERNAME:
        return _strip_mention(text)

    # 检查 @mention
    if BOT_USERNAME and f"@{BOT_USERNAME}" in text:
        return _strip_mention(text)

    # 普通群消息且没有 @mention → 忽略
    return None


def _strip_mention(text: str) -> str:
    """去掉消息中的 @bot_username"""
    if BOT_USERNAME:
        text = text.replace(f"@{BOT_USERNAME}", "").strip()
    return text


def _make_reply_kwargs(update: Update) -> dict:
    """构造发送消息的 kwargs（自动处理 topic）"""
    kwargs = {"chat_id": update.effective_chat.id}
    topic_id = getattr(update.message, "message_thread_id", None)
    if topic_id:
        kwargs["message_thread_id"] = topic_id
    return kwargs


async def _typing_loop(bot, chat_id: int, topic_id: int | None):
    """持续发送 typing action 直到被 cancel"""
    try:
        while True:
            kwargs = {"chat_id": chat_id, "action": ChatAction.TYPING}
            if topic_id:
                kwargs["message_thread_id"] = topic_id
            await bot.send_chat_action(**kwargs)
            await asyncio.sleep(4)  # Telegram typing 持续 5 秒，4 秒续一次
    except asyncio.CancelledError:
        pass


def _tool_summary(name: str, input_data: dict) -> str:
    """生成工具使用的简短摘要"""
    if name == "Bash":
        cmd = input_data.get("command", "")
        return f"$ {cmd[:80]}"
    elif name == "Read":
        return f"读取 {input_data.get('file_path', '?')}"
    elif name in ("Edit", "FileEdit"):
        return f"编辑 {input_data.get('file_path', '?')}"
    elif name in ("Write", "FileWrite"):
        return f"写入 {input_data.get('file_path', '?')}"
    elif name == "Glob":
        return f"搜索 {input_data.get('pattern', '?')}"
    elif name == "Grep":
        return f"搜索内容 {input_data.get('pattern', '?')[:40]}"
    elif name == "Agent":
        return f"子任务: {input_data.get('description', '?')[:40]}"
    else:
        return name


async def _download_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str | None:
    """如果消息包含文件/图片/语音，下载到 workspace/ 并返回路径"""
    msg = update.message
    file_obj = None
    filename = None

    if msg.document:
        file_obj = msg.document
        filename = msg.document.file_name or f"doc_{msg.document.file_unique_id}"
    elif msg.photo:
        file_obj = msg.photo[-1]  # 最大尺寸
        filename = f"photo_{file_obj.file_unique_id}.jpg"
    elif msg.voice:
        file_obj = msg.voice
        filename = f"voice_{file_obj.file_unique_id}.ogg"
    elif msg.audio:
        file_obj = msg.audio
        filename = msg.audio.file_name or f"audio_{file_obj.file_unique_id}"
    elif msg.video:
        file_obj = msg.video
        filename = msg.video.file_name or f"video_{file_obj.file_unique_id}.mp4"

    if not file_obj:
        return None

    workspace = os.path.join(WORK_DIR, "workspace")
    os.makedirs(workspace, exist_ok=True)
    filepath = os.path.join(workspace, filename)

    tg_file = await context.bot.get_file(file_obj.file_id)
    await tg_file.download_to_drive(filepath)
    logger.info(f"文件已下载: {filepath}")
    return filepath


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理用户消息（文本 + 文件）"""
    text = _extract_text(update) or ""

    # 下载附件
    file_path = await _download_file(update, context)
    if file_path:
        file_notice = f"[用户发送了文件，已保存到: {file_path}]"
        text = f"{file_notice}\n{text}" if text else file_notice

    if not text:
        return

    key = _session_key(update)
    reply_kwargs = _make_reply_kwargs(update)
    chat_id = update.effective_chat.id
    topic_id = getattr(update.message, "message_thread_id", None)
    loop = asyncio.get_event_loop()
    bot = context.bot
    verbose = key in _verbose_keys

    # 群组里标注发送者
    sender = ""
    chat_type = update.effective_chat.type
    if chat_type in ("group", "supergroup"):
        user = update.effective_user
        sender = f"[{user.first_name or user.username or user.id}] "

    logger.info(f"[{key}] {sender}收到: {text[:80]}")

    # --- 回调 ---
    def on_response(response_text: str):
        async def _send():
            # 停止 typing
            task = _typing_tasks.pop(key, None)
            if task:
                task.cancel()
            for i in range(0, len(response_text), 4000):
                chunk = response_text[i:i + 4000]
                try:
                    md_text = markdownify(chunk)
                    await bot.send_message(**reply_kwargs, text=md_text, parse_mode=ParseMode.MARKDOWN_V2)
                except Exception:
                    # MarkdownV2 解析失败 → fallback 纯文本
                    try:
                        await bot.send_message(**reply_kwargs, text=chunk)
                    except Exception as e:
                        logger.error(f"发送失败: {e}")
        asyncio.run_coroutine_threadsafe(_send(), loop)

    def on_busy_changed(is_busy: bool):
        async def _update():
            if is_busy:
                task = asyncio.create_task(_typing_loop(bot, chat_id, topic_id))
                _typing_tasks[key] = task
            else:
                task = _typing_tasks.pop(key, None)
                if task:
                    task.cancel()
        asyncio.run_coroutine_threadsafe(_update(), loop)

    def on_tool_use(tool_name: str, input_data: dict):
        if not verbose:
            return
        summary = _tool_summary(tool_name, input_data)
        async def _notify():
            try:
                await bot.send_message(**reply_kwargs, text=f"🔧 {summary}")
            except:
                pass
        asyncio.run_coroutine_threadsafe(_notify(), loop)

    # 群组里带上发送者信息
    if sender:
        text = f"{sender}{text}"

    mcp_env = {
        "platform": "telegram",
        "bot_token": TOKEN,
        "chat_id": str(chat_id),
        "topic_id": str(topic_id or ""),
        "session_key": key,
    }

    try:
        bridge = sessions.get_or_create(
            key,
            on_response=on_response,
            on_turn_complete=None,
            mcp_env=mcp_env,
        )
        bridge.on_busy_changed = on_busy_changed
        bridge.on_tool_use = on_tool_use
        bridge.send(text)
    except RuntimeError as e:
        await bot.send_message(**reply_kwargs, text=f"Claude Code 启动失败: {e}")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply_kwargs = _make_reply_kwargs(update)
    await context.bot.send_message(**reply_kwargs, text=(
        "Claude Code to Claw\n\n"
        "直接发消息即可与 Claude Code 对话。\n\n"
        "模式:\n"
        "• 私聊 — 直接发消息\n"
        "• 私聊 Topic — 每个 topic 独立 session\n"
        "• 群组 Forum Topic — 每个 topic 独立 session\n"
        "• 普通群组 — @mention 或 reply bot\n\n"
        "命令:\n"
        "/start - 显示帮助\n"
        "/status - 查看当前 session\n"
        "/reset - 重置当前 session\n"
        "/attach [session_id] [cwd] - 接管 CLI session\n"
        "/detach - 分离 session（可在 CLI resume）\n"
        "/sessions - 列出所有活跃 session\n"
        "/verbose - 切换工具反馈（显示 Read/Edit/Bash 等操作）\n\n"
        "定时任务直接说就行:\n"
        "  \"10分钟后提醒我开会\"\n"
        "  \"2小时后检查部署结果\""
    ))


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """重置当前 session（杀掉进程，下次发消息会新建）"""
    key = _session_key(update)
    reply_kwargs = _make_reply_kwargs(update)

    with sessions._lock:
        if key in sessions._sessions:
            sessions._sessions[key].stop()
            del sessions._sessions[key]

    await context.bot.send_message(**reply_kwargs, text="Session 已重置。下条消息将创建新 session。")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = _session_key(update)
    reply_kwargs = _make_reply_kwargs(update)

    with sessions._lock:
        bridge = sessions._sessions.get(key)

    if bridge and bridge.is_alive:
        status = (
            f"Session: {bridge.session_id[:8]}...\n"
            f"Full ID: {bridge.session_id}\n"
            f"状态: {'处理中' if bridge._is_busy else '空闲'}\n"
            f"工作目录: {bridge.cwd}"
        )
    else:
        status = "无活跃 session。发送消息以创建。"

    await context.bot.send_message(**reply_kwargs, text=status)


async def cmd_attach(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    接管一个 CLI session。
    用法:
      /attach                        — 列出可用 sessions
      /attach <session_id>           — resume 指定 session
      /attach <session_id> /path/to  — 指定工作目录
    """
    key = _session_key(update)
    reply_kwargs = _make_reply_kwargs(update)
    loop = asyncio.get_event_loop()

    args = context.args or []
    if not args:
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
        await context.bot.send_message(**reply_kwargs, text=text)
        return

    session_id = args[0]
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
            await context.bot.send_message(**reply_kwargs, text=text)
            return
        elif not matches:
            await context.bot.send_message(**reply_kwargs, text=f"未找到匹配 '{session_id}' 的 session。")
            return

    cwd = args[1] if len(args) > 1 else None

    def on_response(response_text: str):
        async def _send():
            for i in range(0, len(response_text), 4000):
                chunk = response_text[i:i + 4000]
                try:
                    md_text = markdownify(chunk)
                    await context.bot.send_message(**reply_kwargs, text=md_text, parse_mode=ParseMode.MARKDOWN_V2)
                except Exception:
                    try:
                        await context.bot.send_message(**reply_kwargs, text=chunk)
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
    await context.bot.send_message(**reply_kwargs, text=text)


async def cmd_detach(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """分离当前 session（进程停止，但 session 数据保留，可以在 CLI 里 resume）"""
    key = _session_key(update)
    reply_kwargs = _make_reply_kwargs(update)

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
    await context.bot.send_message(**reply_kwargs, text=text)


async def cmd_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """列出所有活跃的 bot session"""
    reply_kwargs = _make_reply_kwargs(update)

    active = sessions.list_sessions()
    if not active:
        text = "没有活跃的 session。"
    else:
        lines = [f"活跃 sessions ({len(active)}):\n"]
        for s in active:
            status = "处理中" if s["busy"] else "空闲"
            lines.append(
                f"• {s['key']}\n"
                f"  ID: {s['session_id'][:8]}... "
                f"[{status}]"
            )
        text = "\n".join(lines)
    await context.bot.send_message(**reply_kwargs, text=text)


async def cmd_verbose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """切换工具反馈开关"""
    key = _session_key(update)
    reply_kwargs = _make_reply_kwargs(update)

    if key in _verbose_keys:
        _verbose_keys.discard(key)
        await context.bot.send_message(**reply_kwargs, text="工具反馈已关闭。")
    else:
        _verbose_keys.add(key)
        await context.bot.send_message(**reply_kwargs, text="工具反馈已开启。Claude 使用工具时会推送通知。")


async def post_init(application):
    """启动时获取 bot username + 启动 cron"""
    global BOT_USERNAME, _bot_instance
    bot = await application.bot.get_me()
    BOT_USERNAME = bot.username or ""
    _bot_instance = application.bot
    logger.info(f"Bot username: @{BOT_USERNAME}")

    # 启动 scheduler
    loop = asyncio.get_event_loop()

    def on_job_trigger(job):
        logger.info(f"Job triggered: [{job.id}] {job.prompt[:50]}")

        def on_response(response_text: str):
            async def _send():
                text = f"⏰ {response_text[:3900]}"
                kwargs = {"chat_id": int(job.chat_id)}
                if job.topic_id:
                    kwargs["message_thread_id"] = int(job.topic_id)
                try:
                    md_text = markdownify(text)
                    await _bot_instance.send_message(**kwargs, text=md_text, parse_mode=ParseMode.MARKDOWN_V2)
                except Exception:
                    try:
                        await _bot_instance.send_message(**kwargs, text=text)
                    except Exception as e:
                        logger.error(f"Job response send failed: {e}")
            asyncio.run_coroutine_threadsafe(_send(), loop)

        try:
            job_mcp_env = {
                "platform": "telegram",
                "bot_token": TOKEN,
                "chat_id": job.chat_id,
                "topic_id": job.topic_id,
                "session_key": job.session_key,
            }
            bridge = sessions.get_or_create(
                job.session_key,
                on_response=on_response,
                mcp_env=job_mcp_env,
            )
            bridge.send(job.prompt)
        except Exception as e:
            logger.error(f"Job execution failed [{job.id}]: {e}")

    scheduler.set_trigger(on_job_trigger)
    scheduler.start()


def main():
    logger.info(f"工作目录: {WORK_DIR}")
    logger.info("启动 Telegram Bot...")

    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("attach", cmd_attach))
    app.add_handler(CommandHandler("detach", cmd_detach))
    app.add_handler(CommandHandler("sessions", cmd_sessions))
    app.add_handler(CommandHandler("verbose", cmd_verbose))
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.Document.ALL | filters.PHOTO | filters.VOICE | filters.AUDIO | filters.VIDEO)
        & ~filters.COMMAND,
        handle_message,
    ))

    logger.info("Bot 已就绪，开始 polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
