"""Long operations, run in the background and watchable while they run.

Vendoring a project, building a fab package or refreshing offers takes seconds
to minutes. A request that blocks for that long looks broken, and a UI that only
learns the outcome at the end cannot say *what* it is waiting on.

So every long operation is a **job**: started by a POST that returns immediately
with an id, watched over Server-Sent Events, and inspectable afterwards. The
model is deliberately small — a dict of jobs in memory, one thread each. klm is
a single-user desktop application, and a task queue with a broker would be
infrastructure standing in for a feature.

The one rule worth stating: **a job runs the same service function the CLI
calls.** No endpoint reimplements anything, because the moment one does, the GUI
and the CLI start disagreeing about what klm does.
"""

from __future__ import annotations

import queue
import threading
import traceback
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

__all__ = ["Job", "JobRunner", "JobState"]


class JobState:
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Job:
    id: str
    kind: str
    state: str = JobState.PENDING
    progress: list[str] = field(default_factory=list)
    result: Any = None
    error: str | None = None
    started_at: str = field(default_factory=_now)
    finished_at: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "state": self.state,
            "progress": list(self.progress),
            "result": self.result,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class JobRunner:
    """Runs work on background threads and streams what it says.

    Each job gets its own queue of events; a watcher drains it. A job nobody is
    watching still runs and still records its progress, so opening the screen
    late shows the whole story rather than the tail of it.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._queues: dict[str, list[queue.Queue[str | None]]] = {}
        self._lock = threading.Lock()

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def all(self) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.started_at, reverse=True)

    def start(self, kind: str, work: Callable[[Callable[[str], None]], Any]) -> Job:
        """Run ``work`` on a thread, handing it a ``report`` callable."""
        job = Job(id=uuid.uuid4().hex[:12], kind=kind)
        with self._lock:
            self._jobs[job.id] = job
            self._queues[job.id] = []

        def report(message: str) -> None:
            job.progress.append(message)
            self._publish(job.id, message)

        def run() -> None:
            job.state = JobState.RUNNING
            self._publish(job.id, f"started {kind}")
            outcome = JobState.DONE
            try:
                job.result = work(report)
            except Exception as exc:
                outcome = JobState.FAILED
                # The message is what a person reads; the traceback goes to the
                # progress log, where it is available without being shouted.
                job.error = f"{type(exc).__name__}: {exc}"
                job.progress.append(traceback.format_exc().strip())
            finally:
                # The terminal state is assigned *last*, so "state is terminal"
                # means "everything is recorded". A watcher stops as soon as it
                # sees one, and the earlier ordering let it stop between the
                # failure and the traceback being written down.
                job.finished_at = _now()
                job.state = outcome
                self._publish(job.id, None)

        threading.Thread(target=run, name=f"klm-job-{job.id}", daemon=True).start()
        return job

    def watch(self, job_id: str) -> Iterator[str]:
        """Yield progress lines until the job ends. Blocks; used by the SSE route."""
        job = self._jobs.get(job_id)
        if job is None:
            return
        channel: queue.Queue[str | None] = queue.Queue()
        with self._lock:
            self._queues.setdefault(job_id, []).append(channel)
        # Anything that happened before the watcher arrived is replayed, so a
        # late viewer sees the whole story rather than the tail of it.
        yield from list(job.progress)
        if job.state in (JobState.DONE, JobState.FAILED):
            return
        while True:
            message = channel.get()
            if message is None:
                return
            yield message

    def _publish(self, job_id: str, message: str | None) -> None:
        with self._lock:
            channels = list(self._queues.get(job_id, ()))
        for channel in channels:
            channel.put(message)
