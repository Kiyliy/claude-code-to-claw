"""
Scheduler — 一次性延时任务

/at 10m 提醒我开会
/at 2h 检查部署结果
/at 15:30 下午三点半看看CI

重复任务不在这里做 — Claude Code 自带 cron，配合 Telegram MCP 自己就能搞。
"""

import json
import os
import re
import logging
import threading
import time
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Callable, Optional

logger = logging.getLogger(__name__)

SCHEDULE_FILE = os.environ.get("CLAW_SCHEDULE_FILE", os.path.expanduser("~/.claude/claw_schedules.json"))


def parse_delay(s: str) -> Optional[int]:
    """
    解析延时或时间点，返回秒数。

    延时: 10s, 5m, 2h, 1d, 1h30m, 90(默认分钟)
    时间点: 15:30, 09:00 → 到今天/明天该时间的秒数
    """
    s = s.strip().lower()

    # 时间点: HH:MM
    time_match = re.match(r'^(\d{1,2}):(\d{2})$', s)
    if time_match:
        h, m = int(time_match.group(1)), int(time_match.group(2))
        now = datetime.now()
        target = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if target <= now:
            # 已过 → 明天
            target = target.replace(day=target.day + 1)
        return int((target - now).total_seconds())

    # 延时: 1h30m, 10m, 2h, 30s, etc.
    pattern = re.compile(r'^(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$')
    m = pattern.match(s)
    if m and any(m.groups()):
        d, h, mi, sec = (int(x or 0) for x in m.groups())
        total = d * 86400 + h * 3600 + mi * 60 + sec
        return total if total > 0 else None

    # 纯数字 → 分钟
    if s.isdigit():
        return int(s) * 60

    return None


def format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}秒"
    if seconds < 3600:
        return f"{seconds // 60}分钟"
    if seconds < 86400:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        return f"{h}小时{m}分" if m else f"{h}小时"
    d = seconds // 86400
    h = (seconds % 86400) // 3600
    return f"{d}天{h}小时" if h else f"{d}天"


def format_time(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


@dataclass
class Job:
    id: str
    session_key: str
    chat_id: str
    topic_id: str
    prompt: str
    trigger_at: float
    cwd: str = ""


class Scheduler:
    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._running = False
        self._on_trigger: Optional[Callable[[Job], None]] = None
        self._load()

    def set_trigger(self, callback: Callable[[Job], None]):
        self._on_trigger = callback

    def add(self, session_key: str, chat_id: str, topic_id: str,
            delay_seconds: int, prompt: str, cwd: str = "") -> Job:
        job = Job(
            id=uuid.uuid4().hex[:6],
            session_key=session_key,
            chat_id=chat_id,
            topic_id=topic_id,
            prompt=prompt,
            trigger_at=time.time() + delay_seconds,
            cwd=cwd,
        )
        with self._lock:
            self._jobs[job.id] = job
        self._save()
        logger.info(f"Job [{job.id}] in {delay_seconds}s → {prompt[:50]}")
        return job

    def remove(self, job_id: str) -> Optional[Job]:
        with self._lock:
            job = self._jobs.pop(job_id, None)
        if job:
            self._save()
        return job

    def list_jobs(self, session_key: str = None) -> list[Job]:
        with self._lock:
            jobs = list(self._jobs.values())
        if session_key:
            jobs = [j for j in jobs if j.session_key == session_key]
        jobs.sort(key=lambda j: j.trigger_at)
        return jobs

    def start(self):
        if self._running:
            return
        self._running = True
        threading.Thread(target=self._loop, daemon=True).start()
        logger.info(f"Scheduler started ({len(self._jobs)} pending)")

    def stop(self):
        self._running = False

    def _loop(self):
        while self._running:
            now = time.time()
            fired = []

            with self._lock:
                for jid, job in list(self._jobs.items()):
                    if job.trigger_at <= now:
                        fired.append(job)
                        del self._jobs[jid]

            if fired:
                self._save()
                for job in fired:
                    if self._on_trigger:
                        try:
                            self._on_trigger(job)
                        except Exception as e:
                            logger.error(f"Trigger error [{job.id}]: {e}")

            time.sleep(5)

    def _save(self):
        try:
            os.makedirs(os.path.dirname(SCHEDULE_FILE), exist_ok=True)
            with self._lock:
                data = [asdict(j) for j in self._jobs.values()]
            with open(SCHEDULE_FILE, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Save error: {e}")

    def _load(self):
        if not os.path.isfile(SCHEDULE_FILE):
            return
        try:
            with open(SCHEDULE_FILE) as f:
                data = json.load(f)
            now = time.time()
            for item in data:
                job = Job(**item)
                if job.trigger_at > now:  # 跳过过期的
                    self._jobs[job.id] = job
            logger.info(f"Loaded {len(self._jobs)} pending jobs")
        except Exception as e:
            logger.error(f"Load error: {e}")
