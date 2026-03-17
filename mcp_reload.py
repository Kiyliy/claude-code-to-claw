#!/usr/bin/env python3
"""
Reload MCP Server — 让 Claude Code 请求重启自己以加载新 MCP 配置

Claude 调用 request_reload() 后，写一个信号文件，
bridge 在 turn 结束时检测到信号文件就重启进程（--resume 保持上下文）。

协议: MCP over stdio (JSON-RPC 2.0)
"""

import json
import sys
import os

RELOAD_SIGNAL_FILE = os.environ.get(
    "CLAW_RELOAD_SIGNAL",
    os.path.expanduser("~/.claude/claw_reload_signal"),
)

TOOLS = [
    {
        "name": "request_reload",
        "description": (
            "Request the host to restart this Claude Code process so that "
            "new MCP servers or updated settings take effect. "
            "The restart happens gracefully after the current turn completes, "
            "and conversation context is preserved via --resume. "
            "Use this after you have modified ~/.claude/settings.json, "
            "~/.claude.json, or project .mcp.json to add/remove/update MCP servers."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Why the reload is needed (e.g. 'added weather MCP')",
                    "default": "",
                }
            },
        },
    },
]


def handle_request_reload(args: dict) -> str:
    reason = args.get("reason", "")
    # 写信号文件
    os.makedirs(os.path.dirname(RELOAD_SIGNAL_FILE), exist_ok=True)
    with open(RELOAD_SIGNAL_FILE, "w") as f:
        f.write(reason or "reload requested")
    return (
        "Reload scheduled. The process will restart after this turn completes. "
        "Your conversation context will be preserved."
    )


HANDLERS = {
    "request_reload": handle_request_reload,
}


def write_response(obj: dict):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main():
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
                    "serverInfo": {"name": "claw-reload", "version": "1.0.0"},
                },
            })
        elif method == "notifications/initialized":
            pass
        elif method == "tools/list":
            write_response({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"tools": TOOLS},
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
                        "result": {"content": [{"type": "text", "text": result_text}]},
                    })
                except Exception as e:
                    write_response({
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "content": [{"type": "text", "text": f"Error: {e}"}],
                            "isError": True,
                        },
                    })
            else:
                write_response({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
                })
        elif req_id is not None:
            write_response({"jsonrpc": "2.0", "id": req_id, "result": {}})


if __name__ == "__main__":
    main()
