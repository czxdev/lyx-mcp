from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from .errors import LyXMCPError


@dataclass
class Job:
    id: str
    kind: str
    stage: str = "queue"
    status: str = "running"
    transaction: str = "not_started"
    result: dict[str, Any] | None = None
    task: asyncio.Task[None] | None = None
    started: float = 0
    cancel_requested: bool = False
    deadline: float = 0


CURRENT_JOB: ContextVar[Job | None] = ContextVar("lyx_job", default=None)


def stage(name: str) -> None:
    job = CURRENT_JOB.get()
    if job:
        if job.deadline and time.monotonic() >= job.deadline:
            raise TimeoutError("operation deadline exceeded")
        job.stage = name


def transaction_status(value: str) -> None:
    job = CURRENT_JOB.get()
    if job:
        job.transaction = value


class Jobs:
    def __init__(self, recover: Callable[[], Awaitable[dict[str, Any]]]) -> None:
        self.jobs: dict[str, Job] = {}
        self.recover = recover

    async def submit(
        self,
        kind: str,
        operation: Callable[[], Awaitable[dict[str, Any]]],
        timeout_sec: float,
        wait_sec: float,
    ) -> dict[str, Any]:
        if not 0 < timeout_sec <= 3600 or not 0 <= wait_sec <= 30:
            raise LyXMCPError("INVALID_ARGUMENT", "timeout_sec must be in (0, 3600]; wait_sec in [0, 30]")
        job = Job(uuid.uuid4().hex, kind, started=time.monotonic())
        job.deadline = job.started + timeout_sec
        self.jobs[job.id] = job
        job.task = asyncio.create_task(self._run(job, operation, timeout_sec))
        job.task.add_done_callback(lambda task: self._finished(job, task))
        return await self.inspect(job.id, "wait", wait_sec)

    @staticmethod
    def _finished(job: Job, task: asyncio.Task[None]) -> None:
        if task.cancelled() and job.result is None:
            job.status = "cancelled"
            job.result = {
                "ok": False,
                "error_code": "CANCELLED",
                "stage": job.stage,
                "transaction_status": job.transaction,
                "retry_safe": job.transaction == "not_started",
            }

    async def _run(
        self,
        job: Job,
        operation: Callable[[], Awaitable[dict[str, Any]]],
        timeout_sec: float,
    ) -> None:
        context = CURRENT_JOB.set(job)
        try:
            async with asyncio.timeout_at(job.deadline):
                job.result = await operation()
                if time.monotonic() >= job.deadline:
                    raise TimeoutError("operation deadline exceeded")
            job.status = "completed" if job.result.get("ok", True) else "failed"
        except (TimeoutError, asyncio.CancelledError, LyXMCPError, OSError) as exc:
            timeout = isinstance(exc, TimeoutError)
            cancelled = isinstance(exc, asyncio.CancelledError)
            terminal_status = "timed_out" if timeout else "cancelled" if cancelled else "failed"
            job.status = "recovering"
            failing_stage = job.stage
            recovery: dict[str, Any] = {"status": "not_needed"}
            if job.stage != "queue":
                recovery = await self.recover()
            job.result = {
                "ok": False,
                "stage": failing_stage,
                "error_code": "DEADLINE_EXCEEDED"
                if timeout
                else "CANCELLED"
                if cancelled
                else exc.code
                if isinstance(exc, LyXMCPError)
                else type(exc).__name__,
                "diagnostic": str(exc) or ("operation deadline exceeded" if timeout else "operation cancelled"),
                "recovery": recovery,
                "transaction_status": job.transaction,
                "retry_safe": job.transaction in ("not_started", "not_committed", "rolled_back")
                and recovery.get("status") != "failed",
            }
            job.status = terminal_status
        except Exception as exc:
            job.status = "failed"
            job.result = {
                "ok": False,
                "stage": job.stage,
                "error_code": type(exc).__name__,
                "diagnostic": str(exc),
                "transaction_status": job.transaction,
            }
            raise
        finally:
            CURRENT_JOB.reset(context)

    async def inspect(self, job_id: str, action: str = "status", wait_sec: float = 0) -> dict[str, Any]:
        if job_id not in self.jobs:
            raise LyXMCPError("UNKNOWN_JOB", "job ID is absent from this server session")
        if action not in ("status", "wait", "cancel") or not 0 <= wait_sec <= 30:
            raise LyXMCPError("INVALID_ARGUMENT", "action must be status/wait/cancel; wait_sec in [0, 30]")
        job = self.jobs[job_id]
        assert job.task is not None
        if action == "cancel" and not job.task.done() and not job.cancel_requested:
            job.cancel_requested = True
            job.task.cancel()
        if action in ("wait", "cancel") and not job.task.done() and wait_sec:
            await asyncio.wait({job.task}, timeout=wait_sec)
        result: dict[str, Any] = {
            "job_id": job.id,
            "status": job.status,
            "stage": job.stage,
            "transaction_status": job.transaction,
            "elapsed_sec": round(time.monotonic() - job.started, 3),
        }
        if job.result is not None:
            result.update(job.result)
        return result

    async def close(self) -> None:
        pending = [job.task for job in self.jobs.values() if job.task and not job.task.done()]
        for job in self.jobs.values():
            if job.task and not job.task.done() and not job.cancel_requested and job.status != "recovering":
                job.cancel_requested = True
                job.task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
