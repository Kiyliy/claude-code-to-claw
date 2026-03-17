#!/usr/bin/env python3
"""
Feishu MCP Server — 让 Claude Code 主动给飞书发消息/文件/图片

两种模式自动切换:
  1. Webhook 模式: 只需 CLAW_FEISHU_WEBHOOK，支持文字/富文本/卡片
  2. App API 模式: 需要 APP_ID + APP_SECRET + CHAT_ID，额外支持图片/文件

协议: MCP over stdio (JSON-RPC 2.0)
"""

import json
import sys
import os
import urllib.request
import mimetypes
import time as _time
import uuid as _uuid

# --- 配置 ---
# Webhook 模式
WEBHOOK_URL = os.environ.get("CLAW_FEISHU_WEBHOOK", "")
# App API 模式
APP_ID = os.environ.get("CLAW_FEISHU_APP_ID", "")
APP_SECRET = os.environ.get("CLAW_FEISHU_APP_SECRET", "")
CHAT_ID = os.environ.get("CLAW_FEISHU_CHAT_ID", "")
# 通用
SCHEDULE_FILE = os.environ.get("CLAW_SCHEDULE_FILE", os.path.expanduser("~/.claude/claw_schedules.json"))
SESSION_KEY = os.environ.get("CLAW_SESSION_KEY", "")

API_BASE = "https://open.feishu.cn/open-apis"
USE_APP_API = bool(APP_ID and APP_SECRET and CHAT_ID)

# token 缓存 (App API 模式)
_token_cache = {"token": "", "expires_at": 0}


# ============================================================
#  底层 API
# ============================================================

def _webhook_post(payload: dict) -> dict:
    """Webhook: POST JSON"""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        WEBHOOK_URL, data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    resp = urllib.request.urlopen(req, timeout=10)
    return json.loads(resp.read())


def _get_tenant_token() -> str:
    """App API: 获取 tenant_access_token（带缓存）"""
    now = _time.time()
    if _token_cache["token"] and _token_cache["expires_at"] > now + 60:
        return _token_cache["token"]

    url = f"{API_BASE}/auth/v3/tenant_access_token/internal"
    body = json.dumps({"app_id": APP_ID, "app_secret": APP_SECRET}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    resp = urllib.request.urlopen(req, timeout=10)
    data = json.loads(resp.read())

    if data.get("code") != 0:
        raise RuntimeError(f"Failed to get token: {data.get('msg', data)}")

    _token_cache["token"] = data["tenant_access_token"]
    _token_cache["expires_at"] = now + data.get("expire", 7200)
    return _token_cache["token"]


def _app_api(method: str, path: str, data: dict = None, files: dict = None) -> dict:
    """App API: 调用飞书开放 API"""
    url = f"{API_BASE}{path}"
    token = _get_tenant_token()
    headers = {"Authorization": f"Bearer {token}"}

    if files:
        import io
        boundary = "----ClawMCPBoundary"
        body = io.BytesIO()

        for key, val in (data or {}).items():
            body.write(f"--{boundary}\r\n".encode())
            body.write(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
            body.write(f"{val}\r\n".encode())

        for key, (filename, filedata, content_type) in files.items():
            body.write(f"--{boundary}\r\n".encode())
            body.write(f'Content-Disposition: form-data; name="{key}"; filename="{filename}"\r\n'.encode())
            body.write(f"Content-Type: {content_type}\r\n\r\n".encode())
            body.write(filedata)
            body.write(b"\r\n")

        body.write(f"--{boundary}--\r\n".encode())
        body_bytes = body.getvalue()
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    else:
        body_bytes = json.dumps(data or {}).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"

    req = urllib.request.Request(url, data=body_bytes, headers=headers, method=method)
    resp = urllib.request.urlopen(req, timeout=30)
    return json.loads(resp.read())


def _app_send_message(msg_type: str, content: dict) -> dict:
    """App API: 发送消息"""
    return _app_api("POST", "/im/v1/messages?receive_id_type=chat_id", {
        "receive_id": CHAT_ID,
        "msg_type": msg_type,
        "content": json.dumps(content),
    })


# ============================================================
#  Tool Handlers — 文字
# ============================================================

def handle_send_message(args: dict) -> str:
    text = args["text"]

    if USE_APP_API:
        result = _app_send_message("text", {"text": text})
        if result.get("code") != 0:
            return f"Error: {result.get('msg', result)}"
        return f"Message sent (id: {result.get('data', {}).get('message_id', '?')})"
    else:
        result = _webhook_post({"msg_type": "text", "content": {"text": text}})
        if result.get("code") != 0:
            return f"Error: {result.get('msg', result)}"
        return "Message sent"


# ============================================================
#  Tool Handlers — 富文本
# ============================================================

def handle_send_rich_message(args: dict) -> str:
    title = args.get("title", "")
    content = args["content"]

    lines = content.split("\n")
    elements = [[{"tag": "text", "text": line}] for line in lines]

    post_body = {
        "msg_type": "post",
        "content": {
            "post": {
                "zh_cn": {
                    "title": title,
                    "content": elements
                }
            }
        }
    }

    if USE_APP_API:
        result = _app_send_message("post", {
            "zh_cn": {"title": title, "content": elements}
        })
        if result.get("code") != 0:
            return f"Error: {result.get('msg', result)}"
        return f"Rich message sent (id: {result.get('data', {}).get('message_id', '?')})"
    else:
        result = _webhook_post(post_body)
        if result.get("code") != 0:
            return f"Error: {result.get('msg', result)}"
        return "Rich message sent"


# ============================================================
#  Tool Handlers — 卡片
# ============================================================

def handle_send_card(args: dict) -> str:
    title = args.get("title", "")
    content = args["content"]
    color = args.get("color", "blue")

    card_payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": color,
            },
            "elements": [
                {"tag": "markdown", "content": content}
            ]
        }
    }

    if USE_APP_API:
        result = _app_send_message("interactive", card_payload["card"])
        if result.get("code") != 0:
            return f"Error: {result.get('msg', result)}"
        return f"Card sent (id: {result.get('data', {}).get('message_id', '?')})"
    else:
        result = _webhook_post(card_payload)
        if result.get("code") != 0:
            return f"Error: {result.get('msg', result)}"
        return "Card sent"


# ============================================================
#  Tool Handlers — 图片 (App API only)
# ============================================================

def handle_send_image(args: dict) -> str:
    if not USE_APP_API:
        return "Error: feishu_send_image requires App API mode (set CLAW_FEISHU_APP_ID, CLAW_FEISHU_APP_SECRET, CLAW_FEISHU_CHAT_ID). Webhook mode does not support image upload."

    file_path = args["file_path"]
    caption = args.get("caption", "")

    if not os.path.isfile(file_path):
        return f"Error: file not found: {file_path}"

    # 上传图片
    with open(file_path, "rb") as f:
        file_data = f.read()
    filename = os.path.basename(file_path)
    content_type = mimetypes.guess_type(file_path)[0] or "image/png"

    upload_result = _app_api("POST", "/im/v1/images",
        data={"image_type": "message"},
        files={"image": (filename, file_data, content_type)},
    )
    if upload_result.get("code") != 0:
        return f"Error uploading image: {upload_result.get('msg', upload_result)}"

    image_key = upload_result["data"]["image_key"]

    # 发送图片消息
    result = _app_send_message("image", {"image_key": image_key})
    if result.get("code") != 0:
        return f"Error: {result.get('msg', result)}"

    msg_id = result.get("data", {}).get("message_id", "?")
    if caption:
        _app_send_message("text", {"text": caption})
    return f"Image sent: {filename} (id: {msg_id})"


# ============================================================
#  Tool Handlers — 文件 (App API only)
# ============================================================

def handle_send_file(args: dict) -> str:
    if not USE_APP_API:
        return "Error: feishu_send_file requires App API mode (set CLAW_FEISHU_APP_ID, CLAW_FEISHU_APP_SECRET, CLAW_FEISHU_CHAT_ID). Webhook mode does not support file upload."

    file_path = args["file_path"]
    caption = args.get("caption", "")

    if not os.path.isfile(file_path):
        return f"Error: file not found: {file_path}"

    with open(file_path, "rb") as f:
        file_data = f.read()
    filename = os.path.basename(file_path)
    content_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"

    ext = os.path.splitext(file_path)[1].lower()
    type_map = {".xls": "xls", ".xlsx": "xls", ".ppt": "ppt", ".pptx": "ppt", ".pdf": "pdf"}
    file_type = type_map.get(ext, "stream")

    upload_result = _app_api("POST", "/im/v1/files",
        data={"file_type": file_type, "file_name": filename},
        files={"file": (filename, file_data, content_type)},
    )
    if upload_result.get("code") != 0:
        return f"Error uploading file: {upload_result.get('msg', upload_result)}"

    file_key = upload_result["data"]["file_key"]

    result = _app_send_message("file", {"file_key": file_key})
    if result.get("code") != 0:
        return f"Error: {result.get('msg', result)}"

    msg_id = result.get("data", {}).get("message_id", "?")
    if caption:
        _app_send_message("text", {"text": caption})
    return f"File sent: {filename} (id: {msg_id})"


# ============================================================
#  Schedule
# ============================================================

def _parse_delay(s: str) -> int | None:
    import re
    from datetime import datetime
    s = s.strip().lower()
    tm = re.match(r'^(\d{1,2}):(\d{2})$', s)
    if tm:
        h, m = int(tm.group(1)), int(tm.group(2))
        now = datetime.now()
        target = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if target <= now:
            from datetime import timedelta
            target += timedelta(days=1)
        return int((target - now).total_seconds())
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
        "topic_id": "",
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


# ============================================================
#  MCP Tools Definition (动态生成，根据模式决定是否暴露图片/文件工具)
# ============================================================

def _build_tools() -> list:
    tools = [
        {
            "name": "feishu_send_message",
            "description": "Send a text message to the current Feishu (飞书) chat where you received the user's message. No need to specify a recipient — it automatically goes to the same chat. Use this to proactively notify the user about results, errors, or status updates.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The message text to send"}
                },
                "required": ["text"]
            }
        },
        {
            "name": "feishu_send_rich_message",
            "description": "Send a rich text message to the current Feishu (飞书) chat with a title and multi-line content. Automatically sent to the same chat where you received the user's message.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Message title", "default": ""},
                    "content": {"type": "string", "description": "Multi-line text content"}
                },
                "required": ["content"]
            }
        },
        {
            "name": "feishu_send_card",
            "description": "Send an interactive card message to the current Feishu (飞书) chat. Supports markdown content with a colored header. Best for structured reports, status updates, or formatted notifications. Automatically sent to the same chat.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Card header title", "default": ""},
                    "content": {"type": "string", "description": "Card body (supports markdown: **bold**, [link](url), etc.)"},
                    "color": {"type": "string", "description": "Header color: blue, green, red, orange, yellow, purple, indigo, grey, turquoise, violet, wathet, carmine", "default": "blue"}
                },
                "required": ["content"]
            }
        },
    ]

    if USE_APP_API:
        tools.extend([
            {
                "name": "feishu_send_image",
                "description": "Send an image to the current Feishu (飞书) chat. The image is uploaded and delivered to the same chat where you received the user's message. Use this to send screenshots, diagrams, etc.",
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
                "name": "feishu_send_file",
                "description": "Send a file to the current Feishu (飞书) chat. Automatically sent to the same chat where you received the user's message.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "Absolute path to the file"},
                        "caption": {"type": "string", "description": "Optional caption", "default": ""}
                    },
                    "required": ["file_path"]
                }
            },
        ])

    tools.extend([
        {
            "name": "schedule_task",
            "description": "Schedule a task to be executed later. Supports: delay strings (10m, 2h, 1d, 1h30m) or clock times (15:30, 09:00).",
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
            "description": "List all pending scheduled tasks.",
            "inputSchema": {"type": "object", "properties": {}}
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
    ])

    return tools


HANDLERS = {
    "feishu_send_message": handle_send_message,
    "feishu_send_rich_message": handle_send_rich_message,
    "feishu_send_card": handle_send_card,
    "feishu_send_image": handle_send_image,
    "feishu_send_file": handle_send_file,
    "schedule_task": handle_schedule_task,
    "list_tasks": handle_list_tasks,
    "cancel_task": handle_cancel_task,
}


# ============================================================
#  MCP Server
# ============================================================

def write_response(obj: dict):
    line = json.dumps(obj) + "\n"
    sys.stdout.write(line)
    sys.stdout.flush()


def main():
    mode = "App API" if USE_APP_API else "Webhook"
    tools = _build_tools()

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
                    "serverInfo": {"name": f"claw-feishu ({mode})", "version": "1.0.0"}
                }
            })
        elif method == "notifications/initialized":
            pass
        elif method == "tools/list":
            write_response({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"tools": tools}
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
