import asyncio
import shutil
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from fastapi import UploadFile
from loguru import logger

from app.core.config import Settings
from app.core.errors import ServiceError
from app.services.draft_reason_service import DraftReasonService


class JobStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass
class JobRecord:
    job_id: str
    filename: str
    file_path: Path
    request_id: str
    status: JobStatus
    submitted_at: datetime
    updated_at: float
    started_at: datetime | None = None
    completed_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: dict[str, str] | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "submitted_at": self.submitted_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "result": self.result,
            "error": self.error,
        }


class AsyncJobManager:
    def __init__(self, settings: Settings, service: DraftReasonService) -> None:
        self.settings = settings
        self.service = service
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=settings.async_queue_max_size)
        self.jobs: dict[str, JobRecord] = {}
        self.lock = asyncio.Lock()
        self.workers: list[asyncio.Task] = []
        self.cleanup_task: asyncio.Task | None = None
        self.queue_dir = settings.temp_dir / "async-queue"

    async def start(self) -> None:
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        self.workers = [
            asyncio.create_task(self._worker(index + 1), name=f"draft-reason-worker-{index + 1}")
            for index in range(self.settings.async_worker_count)
        ]
        self.cleanup_task = asyncio.create_task(self._cleanup_loop(), name="job-cleanup")
        logger.info(
            "⚙️ 异步任务服务已启动 | worker数量={} | 队列上限={} | 任务保留={}秒",
            self.settings.async_worker_count,
            self.settings.async_queue_max_size,
            self.settings.async_job_ttl_seconds,
        )

    async def stop(self) -> None:
        tasks = [*self.workers]
        if self.cleanup_task:
            tasks.append(self.cleanup_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.to_thread(shutil.rmtree, self.queue_dir, True)
        self.workers.clear()
        self.cleanup_task = None

    async def submit(self, upload: UploadFile, request_id: str) -> JobRecord:
        filename = Path(upload.filename or "").name
        suffix = Path(filename).suffix.lower().lstrip(".")
        if not filename or not suffix:
            await upload.close()
            raise ServiceError(400, "INVALID_FILENAME", "文件名或扩展名无效")
        if suffix not in self.settings.allowed_extension_set or suffix not in {"docx", "doc", "txt"}:
            await upload.close()
            raise ServiceError(415, "UNSUPPORTED_FILE_TYPE", f"不支持的文件类型: .{suffix}")
        if self.queue.full():
            await upload.close()
            raise ServiceError(503, "ASYNC_QUEUE_FULL", "异步任务队列已满，请稍后重试")

        job_id = uuid.uuid4().hex
        job_dir = self.queue_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=False)
        file_path = job_dir / f"input.{suffix}"
        size_bytes = 0
        try:
            with file_path.open("wb") as output:
                while chunk := await upload.read(1024 * 1024):
                    size_bytes += len(chunk)
                    if size_bytes > self.settings.upload_max_bytes:
                        raise ServiceError(
                            413,
                            "FILE_TOO_LARGE",
                            f"文件不能超过 {self.settings.upload_max_mb} MB",
                        )
                    output.write(chunk)
            if size_bytes == 0:
                raise ServiceError(400, "EMPTY_FILE", "上传文件为空")
        except Exception:
            await asyncio.to_thread(shutil.rmtree, job_dir, True)
            raise
        finally:
            await upload.close()

        now = datetime.now(timezone.utc)
        record = JobRecord(
            job_id=job_id,
            filename=filename,
            file_path=file_path,
            request_id=request_id,
            status=JobStatus.QUEUED,
            submitted_at=now,
            updated_at=time.time(),
        )
        async with self.lock:
            self.jobs[job_id] = record
        try:
            self.queue.put_nowait(job_id)
        except asyncio.QueueFull as exc:
            async with self.lock:
                self.jobs.pop(job_id, None)
            await asyncio.to_thread(shutil.rmtree, job_dir, True)
            raise ServiceError(503, "ASYNC_QUEUE_FULL", "异步任务队列已满，请稍后重试") from exc

        logger.info(
            "🕒 异步任务已提交 | 任务={} | 文件名={} | 大小={} | 排队任务={}",
            job_id,
            filename,
            self._format_size(size_bytes),
            self.queue.qsize(),
        )
        return record

    async def get(self, job_id: str) -> dict[str, Any]:
        async with self.lock:
            record = self.jobs.get(job_id)
            if record is None:
                raise ServiceError(404, "JOB_NOT_FOUND", "异步任务不存在或已过期")
            return record.snapshot()

    async def _worker(self, worker_number: int) -> None:
        while True:
            job_id = await self.queue.get()
            try:
                await self._process(job_id, worker_number)
            finally:
                self.queue.task_done()

    async def _process(self, job_id: str, worker_number: int) -> None:
        async with self.lock:
            record = self.jobs.get(job_id)
            if record is None:
                return
            record.status = JobStatus.PROCESSING
            record.started_at = datetime.now(timezone.utc)
            record.updated_at = time.time()
            filename = record.filename
            file_path = record.file_path
            request_id = record.request_id

        started_at = time.perf_counter()
        with logger.contextualize(request_id=request_id):
            logger.info(
                "▶️ 异步任务开始 | 任务={} | worker={} | 文件名={}",
                job_id,
                worker_number,
                filename,
            )
            try:
                with file_path.open("rb") as stream:
                    upload = UploadFile(filename=filename, file=stream)
                    reason, result_filename, chars = await self.service.extract_from_upload(upload)
                result = {
                    "draft_reason": reason,
                    "filename": result_filename,
                    "chars_processed": chars,
                }
                async with self.lock:
                    record = self.jobs.get(job_id)
                    if record:
                        record.status = JobStatus.SUCCEEDED
                        record.result = result
                        record.completed_at = datetime.now(timezone.utc)
                        record.updated_at = time.time()
                logger.info(
                    "✅ 异步任务完成 | 任务={} | 耗时={:.2f}s | 文本长度={}字符 | 结果长度={}字符",
                    job_id,
                    time.perf_counter() - started_at,
                    chars,
                    len(reason),
                )
            except ServiceError as exc:
                await self._mark_failed(job_id, exc.code, exc.message)
                logger.warning(
                    "❌ 异步任务失败 | 任务={} | 错误码={} | 耗时={:.2f}s",
                    job_id,
                    exc.code,
                    time.perf_counter() - started_at,
                )
            except Exception:
                await self._mark_failed(job_id, "INTERNAL_ERROR", "任务处理失败")
                logger.exception(
                    "❌ 异步任务异常 | 任务={} | 耗时={:.2f}s",
                    job_id,
                    time.perf_counter() - started_at,
                )
            finally:
                await asyncio.to_thread(shutil.rmtree, file_path.parent, True)

    async def _mark_failed(self, job_id: str, code: str, message: str) -> None:
        async with self.lock:
            record = self.jobs.get(job_id)
            if record:
                record.status = JobStatus.FAILED
                record.error = {"code": code, "message": message}
                record.completed_at = datetime.now(timezone.utc)
                record.updated_at = time.time()

    async def _cleanup_loop(self) -> None:
        interval = min(60, max(10, self.settings.async_job_ttl_seconds // 2))
        while True:
            await asyncio.sleep(interval)
            cutoff = time.time() - self.settings.async_job_ttl_seconds
            async with self.lock:
                expired = [
                    job_id
                    for job_id, record in self.jobs.items()
                    if record.status in {JobStatus.SUCCEEDED, JobStatus.FAILED}
                    and record.updated_at < cutoff
                ]
                for job_id in expired:
                    self.jobs.pop(job_id, None)
            if expired:
                logger.debug("expired_async_jobs_removed count={}", len(expired))

    @staticmethod
    def _format_size(size_bytes: int) -> str:
        if size_bytes < 1024:
            return f"{size_bytes}B"
        if size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f}KB"
        return f"{size_bytes / (1024 * 1024):.1f}MB"
