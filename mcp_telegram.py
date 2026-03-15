#!/usr/bin/env python3
"""
Telegram MCP Server — 让 Claude Code 主动给用户发消息/文件/图片

通过 --mcp-config 注入到 Claude Code 进程中。
Claude Code 可以调用 telegram_send_message / telegram_send_file / telegram_send_image
主动向 Telegram 用户推送内容。

协议: MCP over stdio (JSON-RPC 2.0)
"""

import json
import sys
import os
import urllib.request
import urllib.parse
import mimetypes

import time as _time
import uuid as _uuid
import threading as _threading

BOT_TOKEN = os.environ.get("CLAW_BOT_TOKEN", "")
CHAT_ID = os.environ.get("CLAW_CHAT_ID", "")
TOPIC_ID = os.environ.get("CLAW_TOPIC_ID", "")
SCHEDULE_FILE = os.environ.get("CLAW_SCHEDULE_FILE", os.path.expanduser("~/.claude/claw_schedules.json"))
SESSION_KEY = os.environ.get("CLAW_SESSION_KEY", "")

TOOLS = [
    {
        "name": "telegram_send_message",
        "description": "Send a text message to the user on Telegram. Use this to proactively notify the user about results, errors, or status updates.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The message text to send"}
            },
            "required": ["text"]
        }
    },
    {
        "name": "telegram_send_file",
        "description": "Send a file to the user on Telegram.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Absolute path to the file"},
                "caption": {"type": "string", "description": "Optional caption", "default": ""}
            },
            "required": ["file_path"]
        }
    },
    {
        "name": "telegram_send_image",
        "description": "Send an image to the user on Telegram.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Absolute path to the image (png/jpg/gif/webp)"},
                "caption": {"type": "string", "description": "Optional caption", "default": ""}
            },
            "required": ["file_path"]
        }
    },
    {
        "name": "schedule_task",
        "description": "Schedule a task to be executed later. The prompt will be sent to you (Claude) at the specified time, and your response will be sent to the user on Telegram. Use this when the user says things like '10分钟后提醒我', '2小时后检查一下', '明天早上看看CI'. Supports: delay strings (10m, 2h, 1d, 1h30m) or clock times (15:30, 09:00).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "delay": {"type": "string", "description": "When to trigger: '10m', '2h', '1d', '1h30m', '15:30', '09:00'"},
                "prompt": {"type": "string", "description": "The task/prompt to execute at that time"}
            },
            "required": ["delay", "prompt"]
        }
    },
    {
        "name": "list_tasks",
        "description": "List all pending scheduled tasks for this chat.",
        "inputSchema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "cancel_task",
        "description": "Cancel a scheduled task by its ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The task ID to cancel"}
            },
            "required": ["task_id"]
        }
    },
]


def _tg_api(method: str, data: dict = None, files: dict = None) -> dict:
    """调用 Telegram Bot API"""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"

    if files:
        # multipart upload
        import io
        boundary = "----ClawMCPBoundary"
        body = io.BytesIO()

        for key, val in (data or {}).items():
            body.write(f"--{boundary}\r\n".encode())
            body.write(f"Content-Disposition: form-data; name=\"{key}\"\r\n\r\n".encode())
            body.write(f"{val}\r\n".encode())

        for key, (filename, filedata, content_type) in files.items():
            body.write(f"--{boundary}\r\n".encode())
            body.write(f"Content-Disposition: form-data; name=\"{key}\"; filename=\"{filename}\"\r\n".encode())
            body.write(f"Content-Type: {content_type}\r\n\r\n".encode())
            body.write(filedata)
            body.write(b"\r\n")

        body.write(f"--{boundary}--\r\n".encode())
        body_bytes = body.getvalue()

        req = urllib.request.Request(
            url, data=body_bytes,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
        )
    else:
        body_bytes = json.dumps(data or {}).encode("utf-8")
        req = urllib.request.Request(
            url, data=body_bytes,
            headers={"Content-Type": "application/json"}
        )

    resp = urllib.request.urlopen(req, timeout=30)
    return json.loads(resp.read())


def _base_params() -> dict:
    params = {"chat_id": CHAT_ID}
    if TOPIC_ID:
        params["message_thread_id"] = TOPIC_ID
    return params


def handle_send_message(args: dict) -> str:
    text = args["text"]
    params = _base_params()
    params["text"] = text
    result = _tg_api("sendMessage", params)
    return f"Message sent (id: {result.get('result', {}).get('message_id', '?')})"


def handle_send_file(args: dict) -> str:
    file_path = args["file_path"]
    caption = args.get("caption", "")

    if not os.path.isfile(file_path):
        return f"Error: file not found: {file_path}"

    with open(file_path, "rb") as f:
        file_data = f.read()

    filename = os.path.basename(file_path)
    content_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"

    params = _base_params()
    if caption:
        params["caption"] = caption

    result = _tg_api("sendDocument", params, files={
        "document": (filename, file_data, content_type)
    })
    return f"File sent: {filename} (id: {result.get('result', {}).get('message_id', '?')})"


def handle_send_image(args: dict) -> str:
    file_path = args["file_path"]
    caption = args.get("caption", "")

    if not os.path.isfile(file_path):
        return f"Error: file not found: {file_path}"

    with open(file_path, "rb") as f:
        file_data = f.read()

    filename = os.path.basename(file_path)
    content_type = mimetypes.guess_type(file_path)[0] or "image/png"

    params = _base_params()
    if caption:
        params["caption"] = caption

    result = _tg_api("sendPhoto", params, files={
        "photo": (filename, file_data, content_type)
    })
    return f"Image sent: {filename} (id: {result.get('result', {}).get('message_id', '?')})"


def _parse_delay(s: str) -> int | None:
    """Parse delay: 10m, 2h, 1d, 1h30m, 15:30, 09:00"""
    import re
    from datetime import datetime
    s = s.strip().lower()
    # Clock time HH:MM
    tm = re.match(r'^(\d{1,2}):(\d{2})$', s)
    if tm:
        h, m = int(tm.group(1)), int(tm.group(2))
        now = datetime.now()
        target = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if target <= now:
            from datetime import timedelta
            target += timedelta(days=1)
        return int((target - now).total_seconds())
    # Duration
    p = re.compile(r'^(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$')
    m = p.match(s)
    if m and any(m.groups()):
        d, h, mi, sec = (int(x or 0) for x in m.groups())
        total = d * 86400 + h * 3600 + mi * 60 + sec
        return total if total > 0 else None
    if s.isdigit():
        return int(s) * 60
    return None


def _load_schedules() -> list:
    if not os.path.isfile(SCHEDULE_FILE):
        return []
    try:
        with open(SCHEDULE_FILE) as f:
            return json.load(f)
    except:
        return []


def _save_schedules(data: list):
    os.makedirs(os.path.dirname(SCHEDULE_FILE), exist_ok=True)
    with open(SCHEDULE_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def handle_schedule_task(args: dict) -> str:
    delay_str = args["delay"]
    prompt = args["prompt"]

    delay = _parse_delay(delay_str)
    if not delay:
        return f"Error: cannot parse delay '{delay_str}'. Use: 10m, 2h, 1d, 1h30m, 15:30"

    trigger_at = _time.time() + delay
    job_id = _uuid.uuid4().hex[:6]

    jobs = _load_schedules()
    jobs.append({
        "id": job_id,
        "session_key": SESSION_KEY,
        "chat_id": CHAT_ID,
        "topic_id": TOPIC_ID,
        "prompt": prompt,
        "trigger_at": trigger_at,
    })
    _save_schedules(jobs)

    trigger_time = _time.strftime("%H:%M", _time.localtime(trigger_at))
    mins = delay // 60
    return f"Scheduled: [{job_id}] in {mins}min (at {trigger_time}) → {prompt}"


def handle_list_tasks(args: dict) -> str:
    jobs = _load_schedules()
    now = _time.time()
    # Filter to this session and not expired
    mine = [j for j in jobs if j.get("session_key") == SESSION_KEY and j["trigger_at"] > now]
    if not mine:
        return "No pending tasks."
    lines = []
    for j in mine:
        remaining = int(j["trigger_at"] - now) // 60
        t = _time.strftime("%H:%M", _time.localtime(j["trigger_at"]))
        lines.append(f"[{j['id']}] {t} (in {remaining}min) → {j['prompt'][:50]}")
    return "\n".join(lines)


def handle_cancel_task(args: dict) -> str:
    task_id = args["task_id"]
    jobs = _load_schedules()
    new_jobs = [j for j in jobs if j.get("id") != task_id]
    if len(new_jobs) == len(jobs):
        return f"Task {task_id} not found."
    _save_schedules(new_jobs)
    return f"Task {task_id} cancelled."


HANDLERS = {
    "telegram_send_message": handle_send_message,
    "telegram_send_file": handle_send_file,
    "telegram_send_image": handle_send_image,
    "schedule_task": handle_schedule_task,
    "list_tasks": handle_list_tasks,
    "cancel_task": handle_cancel_task,
}


def write_response(obj: dict):
    line = json.dumps(obj) + "\n"
    sys.stdout.write(line)
    sys.stdout.flush()


def main():
    """MCP stdio server — JSON-RPC 2.0 over stdin/stdout"""
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue

        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = req.get("method", "")
        req_id = req.get("id")

        if method == "initialize":
            write_response({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "claw-telegram", "version": "1.0.0"}
                }
            })
        elif method == "notifications/initialized":
            pass  # no response needed
        elif method == "tools/list":
            write_response({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"tools": TOOLS}
            })
        elif method == "tools/call":
            tool_name = req.get("params", {}).get("name", "")
            arguments = req.get("params", {}).get("arguments", {})
            handler = HANDLERS.get(tool_name)
            if handler:
                try:
                    result_text = handler(arguments)
                    write_response({
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "content": [{"type": "text", "text": result_text}]
                        }
                    })
                except Exception as e:
                    write_response({
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "content": [{"type": "text", "text": f"Error: {e}"}],
                            "isError": True
                        }
                    })
            else:
                write_response({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"}
                })
        elif req_id is not None:
            write_response({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {}
            })


if __name__ == "__main__":
    main()
