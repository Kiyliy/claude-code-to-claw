#!/usr/bin/env python3
"""
Claude Code to Claw — Feishu Bot
通过飞书 WebSocket 长连接接收群消息，转发给 Claude Code 处理，回复发回飞书。
"""

import asyncio
import json
import logging
import os
import threading
import urllib.request

from dotenv import load_dotenv

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

from claude_bridge import SessionManager
from scheduler import Scheduler

load_dotenv()

logging.basicConfig(
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("claw-feishu")

# --- 配置 ---
APP_ID = os.environ.get("FEISHU_APP_ID", "")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
WEBHOOK_URL = os.environ.get("FEISHU_WEBHOOK_URL", "")
WORK_DIR = os.environ.get("CLAUDE_WORK_DIR", os.getcwd())

# --- 全局 ---
sessions = SessionManager(base_cwd=WORK_DIR)
scheduler = Scheduler()
_bot_open_id = ""  # 机器人自己的 open_id，过滤自己的消息

# token 缓存
_token_cache = {"token": "", "expires_at": 0}


def _get_token() -> str:
    """获取 tenant_access_token"""
    import time
    now = time.time()
    if _token_cache["token"] and _token_cache["expires_at"] > now + 60:
        return _token_cache["token"]

    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    body = json.dumps({"app_id": APP_ID, "app_secret": APP_SECRET}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    resp = urllib.request.urlopen(req, timeout=10)
    data = json.loads(resp.read())
    _token_cache["token"] = data["tenant_access_token"]
    _token_cache["expires_at"] = now + data.get("expire", 7200)
    return _token_cache["token"]


def _feishu_send_sync(chat_id: str, text: str):
    """通过 App API 发送文字消息到飞书 (同步)"""
    token = _get_token()
    url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
    body = json.dumps({
        "receive_id": chat_id,
        "msg_type": "text",
        "content": json.dumps({"text": text}),
    }).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    })
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        result = json.loads(resp.read())
        if result.get("code") != 0:
            logger.error(f"发送失败: {result.get('msg')}")
    except Exception as e:
        logger.error(f"发送失败: {e}")


def _feishu_send(chat_id: str, text: str):
    """在独立线程中发送，避免阻塞 asyncio event loop"""
    threading.Thread(target=_feishu_send_sync, args=(chat_id, text), daemon=True).start()


def _build_mcp_env(chat_id: str, session_key: str) -> dict:
    """构建 MCP 环境变量"""
    env = {
        "platform": "feishu",
        "session_key": session_key,
        "chat_id": chat_id,
    }
    # 优先 App API，fallback Webhook
    if APP_ID and APP_SECRET:
        env["app_id"] = APP_ID
        env["app_secret"] = APP_SECRET
    if WEBHOOK_URL:
        env["webhook_url"] = WEBHOOK_URL
    return env


# --- 命令处理 ---

def _handle_command(cmd: str, args: str, chat_id: str, session_key: str):
    """处理 /command 命令，直接匹配消息文本"""

    if cmd == "start" or cmd == "help":
        _feishu_send(chat_id, (
            "Claude Code to Claw (Feishu)\n\n"
            "直接发消息即可与 Claude Code 对话。\n\n"
            "命令:\n"
            "/start - 显示帮助\n"
            "/status - 查看当前 session\n"
            "/reset - 重置当前 session\n"
            "/sessions - 列出所有活跃 session\n"
            "/verbose - 切换工具反馈\n\n"
            "定时任务直接说就行:\n"
            '  "10分钟后提醒我开会"\n'
            '  "2小时后检查部署结果"'
        ))
        return True

    if cmd == "reset":
        with sessions._lock:
            if session_key in sessions._sessions:
                sessions._sessions[session_key].stop()
                del sessions._sessions[session_key]
        _feishu_send(chat_id, "Session 已重置。下条消息将创建新 session。")
        return True

    if cmd == "status":
        with sessions._lock:
            bridge = sessions._sessions.get(session_key)
        if bridge and bridge.is_alive:
            status = (
                f"Session: {bridge.session_id[:8]}...\n"
                f"Full ID: {bridge.session_id}\n"
                f"状态: {'处理中' if bridge._is_busy else '空闲'}\n"
                f"工作目录: {bridge.cwd}"
            )
        else:
            status = "无活跃 session。发送消息以创建。"
        _feishu_send(chat_id, status)
        return True

    if cmd == "sessions":
        active = sessions.list_sessions()
        if not active:
            _feishu_send(chat_id, "没有活跃的 session。")
        else:
            lines = [f"活跃 sessions ({len(active)}):"]
            for s in active:
                status = "处理中" if s["busy"] else "空闲"
                lines.append(f"\n• {s['key']}\n  ID: {s['session_id'][:8]}... [{status}]")
            _feishu_send(chat_id, "\n".join(lines))
        return True

    if cmd == "verbose":
        if session_key in _verbose_keys:
            _verbose_keys.discard(session_key)
            _feishu_send(chat_id, "工具反馈已关闭。")
        else:
            _verbose_keys.add(session_key)
            _feishu_send(chat_id, "工具反馈已开启。Claude 使用工具时会推送通知。")
        return True

    return False  # 未匹配命令


# 每个 session key 的 verbose 开关
_verbose_keys: set[str] = set()


def _tool_summary(name: str, input_data: dict) -> str:
    if name == "Bash":
        return f"$ {input_data.get('command', '')[:80]}"
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
    return name


def on_message(event: P2ImMessageReceiveV1):
    """处理飞书消息事件"""
    msg = event.event.message
    sender = event.event.sender

    # 过滤机器人自己的消息
    sender_open_id = sender.sender_id.open_id if sender.sender_id else ""
    if sender_open_id == _bot_open_id:
        return
    if sender.sender_type != "user":
        return

    # 解析消息内容
    if msg.message_type != "text":
        logger.info(f"忽略非文本消息: type={msg.message_type}")
        return

    content = json.loads(msg.content) if msg.content else {}
    text = content.get("text", "").strip()
    if not text:
        return

    # 去掉 @机器人 的占位符
    if msg.mentions:
        for mention in msg.mentions:
            text = text.replace(mention.key, "").strip()
    if not text:
        return

    chat_id = msg.chat_id
    session_key = f"feishu:{chat_id}"

    logger.info(f"[{session_key}] 收到: {text[:80]}")

    # 命令匹配: /xxx
    if text.startswith("/"):
        parts = text[1:].split(None, 1)
        cmd = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""
        if _handle_command(cmd, args, chat_id, session_key):
            return

    mcp_env = _build_mcp_env(chat_id, session_key)
    verbose = session_key in _verbose_keys

    def on_response(response_text: str):
        for i in range(0, len(response_text), 4000):
            chunk = response_text[i:i + 4000]
            _feishu_send(chat_id, chunk)

    def on_tool_use(tool_name: str, input_data: dict):
        if not verbose:
            return
        summary = _tool_summary(tool_name, input_data)
        _feishu_send(chat_id, f"🔧 {summary}")

    try:
        bridge = sessions.get_or_create(
            session_key,
            on_response=on_response,
            on_turn_complete=None,
            mcp_env=mcp_env,
        )
        bridge.on_tool_use = on_tool_use
        bridge.send(text)
    except RuntimeError as e:
        _feishu_send(chat_id, f"Claude Code 启动失败: {e}")


def _init_bot_info():
    """获取机器人自己的 open_id"""
    global _bot_open_id
    try:
        token = _get_token()
        req = urllib.request.Request(
            "https://open.feishu.cn/open-apis/bot/v3/info",
            method="GET",
            headers={"Authorization": f"Bearer {token}"},
        )
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        _bot_open_id = data.get("bot", {}).get("open_id", "")
        bot_name = data.get("bot", {}).get("app_name", "?")
        logger.info(f"Bot: {bot_name} (open_id: {_bot_open_id})")
    except Exception as e:
        logger.warning(f"获取 bot info 失败: {e}")


def _start_scheduler():
    """启动定时任务"""
    def on_job_trigger(job):
        logger.info(f"Job triggered: [{job.id}] {job.prompt[:50]}")

        def on_response(response_text: str):
            chat_id = job.chat_id
            if chat_id:
                _feishu_send(chat_id, f"⏰ {response_text[:3900]}")

        try:
            mcp_env = _build_mcp_env(job.chat_id, job.session_key)
            bridge = sessions.get_or_create(
                job.session_key,
                on_response=on_response,
                mcp_env=mcp_env,
            )
            bridge.send(job.prompt)
        except Exception as e:
            logger.error(f"Job execution failed [{job.id}]: {e}")

    scheduler.set_trigger(on_job_trigger)
    scheduler.start()


def main():
    import sys
    debug = "--debug" in sys.argv
    if debug:
        logging.getLogger().setLevel(logging.DEBUG)
        logger.setLevel(logging.DEBUG)
        logging.getLogger("claude_bridge").setLevel(logging.DEBUG)

    logger.info(f"工作目录: {WORK_DIR}")
    logger.info(f"启动 Feishu Bot... (debug={debug})")

    if not APP_ID or not APP_SECRET:
        logger.error("请设置 FEISHU_APP_ID 和 FEISHU_APP_SECRET")
        return

    _init_bot_info()
    _start_scheduler()

    # 注册消息事件处理
    handler = lark.EventDispatcherHandler.builder("", "") \
        .register_p2_im_message_receive_v1(on_message) \
        .build()

    lark_log_level = lark.LogLevel.DEBUG if debug else lark.LogLevel.INFO
    client = lark.ws.Client(
        app_id=APP_ID,
        app_secret=APP_SECRET,
        event_handler=handler,
        log_level=lark_log_level,
    )

    logger.info("WebSocket 长连接启动中...")
    client.start()


if __name__ == "__main__":
    main()
