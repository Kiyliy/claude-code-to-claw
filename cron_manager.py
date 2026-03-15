"""
Cron Manager — 定时任务，让 Claude Code 全天候自动工作

持久化到 JSON 文件，bot 重启后自动恢复。
"""

import json
import os
import logging
import threading
import time
import uuid
from dataclasses import dataclass, asdict
from typing import Callable, Optional

from croniter import croniter

logger = logging.getLogger(__name__)

CRON_FILE = os.environ.get("CLAW_CRON_FILE", os.path.expanduser("~/.claude/claw_crons.json"))


@dataclass
class CronJob:
    id: str
    session_key: str       # tg:{chat_id} or tg:{chat_id}:{topic_id}
    chat_id: str
    topic_id: str
    cron_expr: str          # "0 9 * * *"
    prompt: str             # "检查 staging 日志"
    enabled: bool = True
    last_run: float = 0.0
    cwd: str = ""


class CronManager:
    def __init__(self):
        self._jobs: dict[str, CronJob] = {}
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._on_trigger: Optional[Callable[[CronJob], None]] = None
        self._load()

    def set_trigger(self, callback: Callable[[CronJob], None]):
        """设置触发回调：cron 到点时调用"""
        self._on_trigger = callback

    def add(self, session_key: str, chat_id: str, topic_id: str,
            cron_expr: str, prompt: str, cwd: str = "") -> CronJob:
        """添加定时任务"""
        if not croniter.is_valid(cron_expr):
            raise ValueError(f"无效的 cron 表达式: {cron_expr}")

        job = CronJob(
            id=uuid.uuid4().hex[:8],
            session_key=session_key,
            chat_id=chat_id,
            topic_id=topic_id,
            cron_expr=cron_expr,
            prompt=prompt,
            cwd=cwd,
        )
        with self._lock:
            self._jobs[job.id] = job
        self._save()
        logger.info(f"Cron added: [{job.id}] {cron_expr} → {prompt[:50]}")
        return job

    def remove(self, job_id: str) -> Optional[CronJob]:
        with self._lock:
            job = self._jobs.pop(job_id, None)
        if job:
            self._save()
            logger.info(f"Cron removed: [{job_id}]")
        return job

    def list_jobs(self, session_key: str = None) -> list[CronJob]:
        with self._lock:
            jobs = list(self._jobs.values())
        if session_key:
            jobs = [j for j in jobs if j.session_key == session_key]
        return jobs

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info(f"Cron manager started ({len(self._jobs)} jobs)")

    def stop(self):
        self._running = False

    def _loop(self):
        while self._running:
            now = time.time()
            with self._lock:
                jobs = list(self._jobs.values())

            for job in jobs:
                if not job.enabled:
                    continue
                try:
                    cron = croniter(job.cron_expr, job.last_run or now - 60)
                    next_time = cron.get_next(float)
                    if next_time <= now:
                        job.last_run = now
                        self._save()
                        if self._on_trigger:
                            try:
                                self._on_trigger(job)
                            except Exception as e:
                                logger.error(f"Cron trigger error [{job.id}]: {e}")
                except Exception as e:
                    logger.error(f"Cron check error [{job.id}]: {e}")

            time.sleep(30)  # 每 30 秒检查一次

    def _save(self):
        try:
            os.makedirs(os.path.dirname(CRON_FILE), exist_ok=True)
            with self._lock:
                data = [asdict(j) for j in self._jobs.values()]
            with open(CRON_FILE, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save crons: {e}")

    def _load(self):
        if not os.path.isfile(CRON_FILE):
            return
        try:
            with open(CRON_FILE) as f:
                data = json.load(f)
            for item in data:
                job = CronJob(**item)
                self._jobs[job.id] = job
            logger.info(f"Loaded {len(self._jobs)} cron jobs from {CRON_FILE}")
        except Exception as e:
            logger.error(f"Failed to load crons: {e}")
