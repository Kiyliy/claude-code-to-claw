# Claude Code to Claw

Bridge [Claude Code](https://claude.ai/code) to Telegram with pending message queue support.

## Features

- **Message Queue**: Messages sent while Claude is processing are queued and merged into a single turn when the current turn completes — just like typing in Claude Code's terminal.
- **Topic Support**: Telegram Forum/Topic mode — each topic gets an independent Claude Code session.
- **Session Persistence**: Sessions survive process restarts via `--resume`.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your Telegram bot token
```

## Usage

```bash
# Run in the directory you want Claude Code to operate on
CLAUDE_WORK_DIR=/path/to/your/project python bot.py
```

## How It Works

```
Telegram User → Bot Server → ClaudeBridge (stdin JSONL) → Claude Code Process
                                    ↑
                          pending message queue
                          (merge on turn complete)
```

Protocol: Claude Code's `--input-format stream-json` accepts JSONL on stdin:
```json
{"type":"user","message":{"role":"user","content":"your message"}}\n
```
