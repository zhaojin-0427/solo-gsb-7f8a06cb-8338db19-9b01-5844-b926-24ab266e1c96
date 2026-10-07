"""内存会话存储：每次上传生成新 job，换书即清空旧裁决。"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field

JOB_TTL_SECONDS = 60 * 60  # 1 小时
MAX_JOBS = 20


@dataclass
class Job:
    job_id: str
    filename: str
    epub: bytes
    analysis: dict
    created: float
    decisions: dict = field(default_factory=dict)


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def create(self, filename: str, epub: bytes, analysis: dict) -> Job:
        with self._lock:
            self._prune_locked()
            job_id = uuid.uuid4().hex
            job = Job(
                job_id=job_id,
                filename=filename,
                epub=epub,
                analysis=analysis,
                created=time.time(),
            )
            self._jobs[job_id] = job
            while len(self._jobs) > MAX_JOBS:
                oldest = min(self._jobs, key=lambda k: self._jobs[k].created)
                del self._jobs[oldest]
            return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job.created = time.time()
            return job

    def save_decisions(self, job_id: str, decisions: dict) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            # 整体替换：换书/重新扫描后旧裁决不会残留
            job.decisions = dict(decisions)
            job.created = time.time()
            return job

    def delete(self, job_id: str) -> None:
        with self._lock:
            self._jobs.pop(job_id, None)

    def _prune_locked(self) -> None:
        now = time.time()
        expired = [k for k, j in self._jobs.items() if now - j.created > JOB_TTL_SECONDS]
        for k in expired:
            del self._jobs[k]
