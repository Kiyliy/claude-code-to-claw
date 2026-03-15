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

BOT_TOKEN = os.environ.get("CLAW_BOT_TOKEN", "")
CHAT_ID = os.environ.get("CLAW_CHAT_ID", "")
TOPIC_ID = os.environ.get("CLAW_TOPIC_ID", "")

TOOLS = [
    {
        "name": "telegram_send_message",
        "description": "Send a text message to the user on Telegram. Use this to proactively notify the user about results, errors, or status updates.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The message text to send"
                }
            },
            "required": ["text"]
        }
    },
    {
        "name": "telegram_send_file",
        "description": "Send a file to the user on Telegram. Use this to share code files, logs, or any document.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute path to the file to send"
                },
                "caption": {
                    "type": "string",
                    "description": "Optional caption for the file",
                    "default": ""
                }
            },
            "required": ["file_path"]
        }
    },
    {
        "name": "telegram_send_image",
        "description": "Send an image to the user on Telegram. Use this to share screenshots, diagrams, or generated images.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute path to the image file (png, jpg, gif, webp)"
                },
                "caption": {
                    "type": "string",
                    "description": "Optional caption for the image",
                    "default": ""
                }
            },
            "required": ["file_path"]
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


HANDLERS = {
    "telegram_send_message": handle_send_message,
    "telegram_send_file": handle_send_file,
    "telegram_send_image": handle_send_image,
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
