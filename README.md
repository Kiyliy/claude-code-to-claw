# 🦀 Claude Code to Claw

> Turn Claude Code into a 24/7 autonomous AI assistant on Telegram.

Bridge your local [Claude Code](https://claude.ai/code) to Telegram with **pending message queue**, **cron jobs**, and **proactive messaging** via MCP.

---

## ✨ Features

### 🔄 Message Queue (Pending Messages)
Just like Claude Code's terminal — send messages anytime, even while Claude is processing. Messages queue up and get **merged into a single turn** when the current task completes.

### ⏰ Cron Jobs
Schedule tasks that run automatically. Claude Code works while you sleep.
```
/cron add 0 9 * * * 检查 staging 日志有没有报错
/cron add */30 * * * * CI 跑完了吗？看看结果
```

### 📤 Proactive Messaging (MCP)
Claude Code gets a Telegram MCP server — it can **proactively** send you messages, files, and images:
- `telegram_send_message` — push text notifications
- `telegram_send_file` — send code, logs, documents
- `telegram_send_image` — send screenshots, diagrams

### 🔗 Session Attach/Detach
Start a session in CLI, continue on Telegram. Or vice versa.
```
/attach e0ed6238       # resume a CLI session by ID
/detach                # release back to CLI
```

### 💬 Multi-Mode
| Mode | Trigger | Session |
|------|---------|---------|
| Private chat | Direct message | Per user |
| Private topic | Direct message | Per topic |
| Group forum topic | Direct message | Per topic |
| Regular group | @mention or reply | Per group |

### 🔧 Tool Feedback
Toggle real-time tool notifications:
```
/verbose   →  🔧 $ git status
               🔧 读取 src/main.py
               🔧 编辑 db/database.py
```

---

## 🚀 Quick Start

```bash
git clone https://github.com/Kiyliy/claude-code-to-claw.git
cd claude-code-to-claw
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env: set TELEGRAM_BOT_TOKEN

# Run (in your project directory)
CLAUDE_WORK_DIR=/path/to/your/project python bot.py
```

---

## 📋 Commands

| Command | Description |
|---------|-------------|
| `/start` | Show help |
| `/status` | Current session info |
| `/reset` | Reset session |
| `/attach [id] [cwd]` | Resume a CLI session |
| `/detach` | Release session to CLI |
| `/sessions` | List active sessions |
| `/verbose` | Toggle tool feedback |
| `/cron add\|list\|del` | Manage scheduled tasks |

---

## 🏗 Architecture

```
Telegram ←→ Bot Server ←→ Claude Code Process
               │                  │
          pending queue      MCP Server (telegram)
          (merge on turn)    ↓ send_message
               │             ↓ send_file
          cron manager       ↓ send_image
          (auto trigger)
```

**Protocol:** Claude Code's `--input-format stream-json` — JSONL over stdin/stdout:
```json
{"type":"user","message":{"role":"user","content":"your message"}}\n
```

**Message Queue:** When Claude is busy, incoming messages buffer. On turn complete (`type: "result"`), all pending messages are merged into one and sent as the next turn — identical to Claude Code's terminal typing behavior.

---

## 🔧 Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Yes | Telegram Bot API token |
| `CLAUDE_WORK_DIR` | No | Working directory for Claude Code (default: cwd) |
| `CLAW_CRON_FILE` | No | Cron jobs persistence file (default: `~/.claude/claw_crons.json`) |

---

## 📜 License

MIT
