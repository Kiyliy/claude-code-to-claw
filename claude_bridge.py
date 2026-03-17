"""
Claude Code Bridge — 通过 stream-json 协议与 Claude Code 进程通信
支持消息队列：Claude 在忙时，新消息入队，turn 完成后合并投递
"""

import subprocess
import json
import threading
import queue
import uuid
import logging
import os
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

CLAUDE_SETTINGS_FILE = os.path.expanduser("~/.claude/settings.json")
RELOAD_SIGNAL_FILE = os.path.expanduser("~/.claude/claw_reload_signal")


def _make_msg(text: str) -> bytes:
    return (json.dumps({
        "type": "user",
        "message": {"role": "user", "content": text}
    }, ensure_ascii=False) + "\n").encode("utf-8")


class ClaudeBridge:
    """
    管理一个 Claude Code 子进程的生命周期和消息队列。

    核心行为：
    - Claude 空闲时，消息直接发送
    - Claude 在忙时，消息入 pending 队列
    - 当前 turn 完成后，所有 pending 消息合并为一条发送
    """

    def __init__(
        self,
        session_id: str,
        on_response: Callable[[str], None],
        on_turn_complete: Optional[Callable[[], None]] = None,
        on_busy_changed: Optional[Callable[[bool], None]] = None,
        on_tool_use: Optional[Callable[[str, dict], None]] = None,
        cwd: Optional[str] = None,
        resume: bool = False,
        mcp_env: Optional[dict] = None,
    ):
        self.session_id = session_id
        self.on_response = on_response
        self.on_turn_complete = on_turn_complete
        self.on_busy_changed = on_busy_changed  # (is_busy) → typing indicator
        self.on_tool_use = on_tool_use  # (tool_name, input) → 工具反馈
        self.cwd = cwd or os.getcwd()

        self._pending = queue.Queue()
        self._is_busy = False
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._alive = False
        self._resume = resume
        self._current_response_parts: list[str] = []

        # MCP 配置 (平台无关)
        self._mcp_env = mcp_env  # {"platform": "telegram"|"feishu", ...平台参数}

        # settings.json MCP 热加载
        self._settings_mtime = self._get_settings_mtime()
        self._needs_reload = False

    @staticmethod
    def _get_settings_mtime() -> float:
        """获取 settings.json 的修改时间"""
        try:
            return os.path.getmtime(CLAUDE_SETTINGS_FILE)
        except OSError:
            return 0

    def _check_mcp_changed(self) -> bool:
        """检查是否需要重启：settings.json 变了，或 Claude 主动请求了 reload"""
        # 信号文件 (Claude 调用 request_reload 产生)
        if os.path.isfile(RELOAD_SIGNAL_FILE):
            return True
        # settings.json 修改时间变了
        current = self._get_settings_mtime()
        return current != self._settings_mtime

    def _reload(self):
        """重启 Claude Code 进程以加载新 MCP"""
        # 读取 reload 原因
        reason = ""
        if os.path.isfile(RELOAD_SIGNAL_FILE):
            try:
                with open(RELOAD_SIGNAL_FILE) as f:
                    reason = f.read().strip()
                os.remove(RELOAD_SIGNAL_FILE)
            except OSError:
                pass
        logger.info(f"[{self.session_id[:8]}] 重启进程加载新 MCP{f' ({reason})' if reason else ''}...")
        self.stop()
        self._resume = True  # 重启后 resume 保持上下文
        self._settings_mtime = self._get_settings_mtime()
        self._needs_reload = False
        self.start()
        time.sleep(2)
        if self.is_alive:
            logger.info(f"[{self.session_id[:8]}] 重启成功，新 MCP 已加载")
        else:
            logger.error(f"[{self.session_id[:8]}] 重启失败")

    def _build_mcp_config(self) -> Optional[str]:
        """生成 MCP config JSON (reload MCP + 平台 MCP)"""
        config = {"mcpServers": {}}
        base_dir = os.path.dirname(os.path.abspath(__file__))

        # --- Reload MCP (始终注入) ---
        reload_path = os.path.join(base_dir, "mcp_reload.py")
        if os.path.isfile(reload_path):
            config["mcpServers"]["reload"] = {
                "command": "python3",
                "args": [reload_path],
            }

        # --- 平台 MCP ---
        if self._mcp_env:
            platform = self._mcp_env.get("platform", "")
            session_key = self._mcp_env.get("session_key", "")

            if platform == "telegram":
                mcp_server_path = os.path.join(base_dir, "mcp_telegram.py")
                if os.path.isfile(mcp_server_path):
                    config["mcpServers"]["telegram"] = {
                        "command": "python3",
                        "args": [mcp_server_path],
                        "env": {
                            "CLAW_BOT_TOKEN": self._mcp_env.get("bot_token", ""),
                            "CLAW_CHAT_ID": self._mcp_env.get("chat_id", ""),
                            "CLAW_TOPIC_ID": self._mcp_env.get("topic_id", ""),
                            "CLAW_SESSION_KEY": session_key,
                        }
                    }
            elif platform == "feishu":
                mcp_server_path = os.path.join(base_dir, "mcp_feishu.py")
                if os.path.isfile(mcp_server_path):
                    env = {"CLAW_SESSION_KEY": session_key}
                    if self._mcp_env.get("webhook_url"):
                        env["CLAW_FEISHU_WEBHOOK"] = self._mcp_env["webhook_url"]
                    if self._mcp_env.get("app_id"):
                        env["CLAW_FEISHU_APP_ID"] = self._mcp_env["app_id"]
                        env["CLAW_FEISHU_APP_SECRET"] = self._mcp_env.get("app_secret", "")
                        env["CLAW_FEISHU_CHAT_ID"] = self._mcp_env.get("chat_id", "")
                    config["mcpServers"]["feishu"] = {
                        "command": "python3",
                        "args": [mcp_server_path],
                        "env": env,
                    }

        if not config["mcpServers"]:
            return None
        return json.dumps(config)

    def start(self):
        cmd = [
            "claude", "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
        ]

        if self._resume:
            cmd += ["--resume", self.session_id]
        else:
            cmd += ["--session-id", self.session_id]

        # 注入 MCP (平台 + 自定义)
        mcp_config = self._build_mcp_config()
        if mcp_config:
            cmd += ["--mcp-config", mcp_config]

        logger.info(f"Starting Claude Code: {' '.join(cmd[:10])}...")
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.cwd,
        )
        self._alive = True

        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def stop(self):
        self._alive = False
        if self._proc:
            try:
                self._proc.stdin.close()
            except:
                pass
            try:
                self._proc.wait(timeout=10)
            except:
                self._proc.kill()
            self._proc = None

    @property
    def is_alive(self) -> bool:
        if not self._proc:
            return False
        return self._proc.poll() is None

    def send(self, text: str):
        """
        发送消息。如果 Claude 在忙，自动入队。
        """
        with self._lock:
            if self._is_busy:
                self._pending.put(text)
                logger.info(f"[{self.session_id[:8]}] 消息入队 (队列: {self._pending.qsize()})")
            else:
                self._send_direct(text)

    def _set_busy(self, busy: bool):
        self._is_busy = busy
        if self.on_busy_changed:
            try:
                self.on_busy_changed(busy)
            except Exception as e:
                logger.debug(f"on_busy_changed error: {e}")

    def _send_direct(self, text: str):
        if not self._proc or not self.is_alive:
            logger.error(f"[{self.session_id[:8]}] 进程未运行，无法发送")
            return
        self._set_busy(True)
        self._current_response_parts = []
        try:
            self._proc.stdin.write(_make_msg(text))
            self._proc.stdin.flush()
            logger.info(f"[{self.session_id[:8]}] → Claude: {text[:80]}")
        except (BrokenPipeError, OSError) as e:
            logger.error(f"[{self.session_id[:8]}] 写入失败: {e}")
            self._set_busy(False)

    def _on_turn_done(self):
        """turn 完成后，检查 MCP 热加载，合并投递 pending 消息"""
        # 先发回复
        full_response = "\n".join(self._current_response_parts)
        if full_response:
            try:
                self.on_response(full_response)
            except Exception as e:
                logger.error(f"on_response callback error: {e}")

        if self.on_turn_complete:
            try:
                self.on_turn_complete()
            except:
                pass

        # 检查自定义 MCP 是否有变化
        if self._check_mcp_changed():
            self._needs_reload = True

        with self._lock:
            self._set_busy(False)

            # 需要重启加载新 MCP → 在发下一条消息前重启
            if self._needs_reload:
                self._reload()

            pending_msgs = []
            while not self._pending.empty():
                pending_msgs.append(self._pending.get())

            if pending_msgs:
                if len(pending_msgs) == 1:
                    merged = pending_msgs[0]
                else:
                    merged = "以下是用户在你处理期间发的多条消息，请一并处理：\n\n"
                    for i, msg in enumerate(pending_msgs, 1):
                        merged += f"{i}. {msg}\n"
                logger.info(f"[{self.session_id[:8]}] 合并 {len(pending_msgs)} 条 pending")
                self._send_direct(merged)

    def _read_stdout(self):
        try:
            for raw_line in self._proc.stdout:
                if not self._alive:
                    break
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg_type = msg.get("type", "")
                if msg_type == "assistant":
                    for block in msg.get("message", {}).get("content", []):
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "text":
                            self._current_response_parts.append(block["text"])
                        elif block.get("type") == "tool_use" and self.on_tool_use:
                            try:
                                self.on_tool_use(block.get("name", ""), block.get("input", {}))
                            except Exception as e:
                                logger.debug(f"on_tool_use error: {e}")
                elif msg_type == "result":
                    self._on_turn_done()
        except Exception as e:
            logger.error(f"stdout reader error: {e}")
        finally:
            logger.info(f"[{self.session_id[:8]}] stdout reader 退出")

    def _read_stderr(self):
        try:
            for raw_line in self._proc.stderr:
                if not self._alive:
                    break
                line = raw_line.decode("utf-8", errors="replace").strip()
                if line:
                    logger.debug(f"[{self.session_id[:8]}] stderr: {line[:200]}")
        except:
            pass


class SessionManager:
    """
    管理多个 ClaudeBridge 实例（每个用户/topic 一个 session）
    """

    def __init__(self, base_cwd: str = None, mcp_env_factory: Callable = None):
        self.base_cwd = base_cwd or os.getcwd()
        self._mcp_env_factory = mcp_env_factory  # (key, chat_id, ...) → mcp_env dict
        self._sessions: dict[str, ClaudeBridge] = {}
        self._lock = threading.Lock()

    def get_or_create(
        self,
        key: str,
        on_response: Callable[[str], None],
        on_turn_complete: Optional[Callable[[], None]] = None,
        mcp_env: Optional[dict] = None,
    ) -> ClaudeBridge:
        with self._lock:
            if key in self._sessions:
                bridge = self._sessions[key]
                if bridge.is_alive:
                    bridge.on_response = on_response
                    bridge.on_turn_complete = on_turn_complete
                    return bridge
                else:
                    logger.info(f"Session {key} 进程已退出，resume...")
                    bridge.stop()
                    del self._sessions[key]

            session_id = self._key_to_session_id(key)
            resume = self._session_exists(session_id)

            bridge = ClaudeBridge(
                session_id=session_id,
                on_response=on_response,
                on_turn_complete=on_turn_complete,
                cwd=self.base_cwd,
                resume=resume,
                mcp_env=mcp_env,
            )
            bridge.start()
            time.sleep(2)

            if not bridge.is_alive:
                logger.error(f"Claude Code 进程启动失败 (session {key})")
                raise RuntimeError("Claude Code 进程启动失败")

            self._sessions[key] = bridge
            return bridge

    def _key_to_session_id(self, key: str) -> str:
        """稳定映射 key → UUID（同一个 key 永远得到同一个 session_id）"""
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"claude-claw.{key}"))

    def attach(
        self,
        key: str,
        session_id: str,
        on_response: Callable[[str], None],
        on_turn_complete: Optional[Callable[[], None]] = None,
        cwd: Optional[str] = None,
        mcp_env: Optional[dict] = None,
    ) -> ClaudeBridge:
        """
        接管一个已存在的 CLI session（通过 session_id resume）。
        """
        with self._lock:
            if key in self._sessions:
                self._sessions[key].stop()
                del self._sessions[key]

            bridge = ClaudeBridge(
                session_id=session_id,
                on_response=on_response,
                on_turn_complete=on_turn_complete,
                cwd=cwd or self.base_cwd,
                resume=True,
                mcp_env=mcp_env,
            )
            bridge.start()
            time.sleep(2)

            if not bridge.is_alive:
                raise RuntimeError(f"无法 resume session {session_id}")

            self._sessions[key] = bridge
            return bridge

    def detach(self, key: str) -> Optional[str]:
        """
        分离 session（停止进程但保留 session 数据，可以在 CLI 里 resume）。
        返回 session_id。
        """
        with self._lock:
            bridge = self._sessions.pop(key, None)
            if not bridge:
                return None
            sid = bridge.session_id
            bridge.stop()
            return sid

    def list_sessions(self) -> list[dict]:
        """列出所有活跃的 session"""
        with self._lock:
            result = []
            for key, bridge in self._sessions.items():
                result.append({
                    "key": key,
                    "session_id": bridge.session_id,
                    "alive": bridge.is_alive,
                    "busy": bridge._is_busy,
                    "pending": bridge._pending.qsize(),
                    "cwd": bridge.cwd,
                })
            return result

    def _session_exists(self, session_id: str) -> bool:
        """检查 session transcript 是否存在"""
        home = os.path.expanduser("~")
        projects_dir = os.path.join(home, ".claude", "projects")
        if not os.path.isdir(projects_dir):
            return False
        for root, dirs, files in os.walk(projects_dir):
            for f in files:
                if session_id in f and f.endswith(".jsonl"):
                    return True
        return False

    @staticmethod
    def list_cli_sessions(limit: int = 20) -> list[dict]:
        """
        列出本机所有 Claude Code session（从 ~/.claude/projects/ 扫描）。
        用于让用户选择要 attach 哪个 session。
        """
        home = os.path.expanduser("~")
        projects_dir = os.path.join(home, ".claude", "projects")
        if not os.path.isdir(projects_dir):
            return []

        sessions = []
        for root, dirs, files in os.walk(projects_dir):
            for f in files:
                if not f.endswith(".jsonl"):
                    continue
                filepath = os.path.join(root, f)
                try:
                    stat = os.stat(filepath)
                    # session id 就是文件名去掉 .jsonl
                    sid = f.replace(".jsonl", "")
                    # 从路径提取项目目录
                    # 路径格式: ~/.claude/projects/<encoded-path>/<session>.jsonl
                    project_dir = os.path.basename(root)
                    # 读最后几行看看有没有有用信息
                    last_line = ""
                    with open(filepath, "rb") as fh:
                        # 读最后 2KB
                        fh.seek(0, 2)
                        size = fh.tell()
                        fh.seek(max(0, size - 2048))
                        tail = fh.read().decode("utf-8", errors="replace")
                        lines = tail.strip().split("\n")
                        last_line = lines[-1] if lines else ""

                    # 尝试提取最后一条消息的摘要
                    summary = ""
                    try:
                        msg = json.loads(last_line)
                        if msg.get("type") == "assistant":
                            for block in msg.get("message", {}).get("content", []):
                                if isinstance(block, dict) and block.get("type") == "text":
                                    summary = block["text"][:60]
                                    break
                    except:
                        pass

                    sessions.append({
                        "session_id": sid,
                        "project": project_dir,
                        "modified": stat.st_mtime,
                        "size_kb": stat.st_size // 1024,
                        "summary": summary,
                    })
                except:
                    continue

        sessions.sort(key=lambda x: x["modified"], reverse=True)
        return sessions[:limit]

    def close_all(self):
        with self._lock:
            for bridge in self._sessions.values():
                bridge.stop()
            self._sessions.clear()
