import asyncio
import json
import shutil
import sqlite3
import time
import uuid
from contextlib import closing
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


class SQLiteJobStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    submitted_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    updated_at REAL NOT NULL,
                    result_json TEXT,
                    error_code TEXT,
                    error_message TEXT
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_status_updated ON jobs(status, updated_at)"
            )

    def create(self, record: JobRecord) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    job_id, filename, file_path, request_id, status,
                    submitted_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.job_id,
                    record.filename,
                    str(record.file_path),
                    record.request_id,
                    record.status.value,
                    record.submitted_at.isoformat(),
                    record.updated_at,
                ),
            )

    def get(self, job_id: str) -> JobRecord | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return self._row_to_record(row) if row else None

    def recover_queued(self) -> list[JobRecord]:
        now = time.time()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = ?, started_at = NULL, updated_at = ?
                WHERE status = ?
                """,
                (JobStatus.QUEUED.value, now, JobStatus.PROCESSING.value),
            )
            rows = connection.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY submitted_at",
                (JobStatus.QUEUED.value,),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def mark_processing(self, job_id: str) -> JobRecord | None:
        now = datetime.now(timezone.utc)
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET status = ?, started_at = ?, updated_at = ?
                WHERE job_id = ? AND status = ?
                """,
                (
                    JobStatus.PROCESSING.value,
                    now.isoformat(),
                    time.time(),
                    job_id,
                    JobStatus.QUEUED.value,
                ),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return self._row_to_record(row)

    def mark_succeeded(self, job_id: str, result: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = ?, result_json = ?, error_code = NULL,
                    error_message = NULL, completed_at = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (
                    JobStatus.SUCCEEDED.value,
                    json.dumps(result, ensure_ascii=False),
                    now.isoformat(),
                    time.time(),
                    job_id,
                ),
            )

    def mark_failed(self, job_id: str, code: str, message: str) -> None:
        now = datetime.now(timezone.utc)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = ?, result_json = NULL, error_code = ?,
                    error_message = ?, completed_at = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (
                    JobStatus.FAILED.value,
                    code,
                    message,
                    now.isoformat(),
                    time.time(),
                    job_id,
                ),
            )

    def return_to_queue(self, job_id: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = ?, started_at = NULL, updated_at = ?
                WHERE job_id = ? AND status = ?
                """,
                (
                    JobStatus.QUEUED.value,
                    time.time(),
                    job_id,
                    JobStatus.PROCESSING.value,
                ),
            )

    def delete(self, job_id: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))

    def delete_expired(self, cutoff: float) -> list[Path]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT file_path FROM jobs
                WHERE status IN (?, ?) AND updated_at < ?
                """,
                (JobStatus.SUCCEEDED.value, JobStatus.FAILED.value, cutoff),
            ).fetchall()
            connection.execute(
                """
                DELETE FROM jobs
                WHERE status IN (?, ?) AND updated_at < ?
                """,
                (JobStatus.SUCCEEDED.value, JobStatus.FAILED.value, cutoff),
            )
        return [Path(row["file_path"]) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> JobRecord:
        result = json.loads(row["result_json"]) if row["result_json"] else None
        error = None
        if row["error_code"]:
            error = {"code": row["error_code"], "message": row["error_message"]}
        return JobRecord(
            job_id=row["job_id"],
            filename=row["filename"],
            file_path=Path(row["file_path"]),
            request_id=row["request_id"],
            status=JobStatus(row["status"]),
            submitted_at=datetime.fromisoformat(row["submitted_at"]),
            started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
            completed_at=(
                datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None
            ),
            updated_at=row["updated_at"],
            result=result,
            error=error,
        )


class AsyncJobManager:
    def __init__(self, settings: Settings, service: DraftReasonService) -> None:
        self.settings = settings
        self.service = service
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=settings.async_queue_max_size)
        self.workers: list[asyncio.Task] = []
        self.cleanup_task: asyncio.Task | None = None
        self.data_dir = settings.async_data_dir
        self.uploads_dir = self.data_dir / "uploads"
        self.store = SQLiteJobStore(self.data_dir / "jobs.db")

    async def start(self) -> None:
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self.store.initialize)
        recovered = await asyncio.to_thread(self.store.recover_queued)
        self.queue = asyncio.Queue(maxsize=self.settings.async_queue_max_size)
        self.workers = [
            asyncio.create_task(self._worker(index + 1), name=f"draft-reason-worker-{index + 1}")
            for index in range(self.settings.async_worker_count)
        ]
        for record in recovered:
            if record.file_path.exists():
                await self.queue.put(record.job_id)
            else:
                await asyncio.to_thread(
                    self.store.mark_failed,
                    record.job_id,
                    "SOURCE_FILE_MISSING",
                    "任务源文件不存在，无法恢复",
                )
        self.cleanup_task = asyncio.create_task(self._cleanup_loop(), name="job-cleanup")
        logger.info(
            "⚙️ 异步任务服务已启动 | worker数量={} | 队列上限={} | 恢复任务={} | 数据目录={}",
            self.settings.async_worker_count,
            self.settings.async_queue_max_size,
            len(recovered),
            self.data_dir,
        )

    async def stop(self) -> None:
        tasks = [*self.workers]
        if self.cleanup_task:
            tasks.append(self.cleanup_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
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
        job_dir = self.uploads_dir / job_id
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

        record = JobRecord(
            job_id=job_id,
            filename=filename,
            file_path=file_path,
            request_id=request_id,
            status=JobStatus.QUEUED,
            submitted_at=datetime.now(timezone.utc),
            updated_at=time.time(),
        )
        try:
            await asyncio.to_thread(self.store.create, record)
            self.queue.put_nowait(job_id)
        except (asyncio.QueueFull, sqlite3.Error) as exc:
            await asyncio.to_thread(self.store.delete, job_id)
            await asyncio.to_thread(shutil.rmtree, job_dir, True)
            if isinstance(exc, asyncio.QueueFull):
                raise ServiceError(
                    503, "ASYNC_QUEUE_FULL", "异步任务队列已满，请稍后重试"
                ) from exc
            raise ServiceError(500, "JOB_PERSIST_FAILED", "异步任务持久化失败") from exc

        logger.info(
            "🕒 异步任务已持久化 | 任务={} | 文件名={} | 大小={} | 排队任务={}",
            job_id,
            filename,
            self._format_size(size_bytes),
            self.queue.qsize(),
        )
        return record

    async def get(self, job_id: str) -> dict[str, Any]:
        record = await asyncio.to_thread(self.store.get, job_id)
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
        record = await asyncio.to_thread(self.store.mark_processing, job_id)
        if record is None:
            return

        started_at = time.perf_counter()
        delete_source = False
        with logger.contextualize(request_id=record.request_id):
            logger.info(
                "▶️ 异步任务开始 | 任务={} | worker={} | 文件名={}",
                job_id,
                worker_number,
                record.filename,
            )
            try:
                with record.file_path.open("rb") as stream:
                    upload = UploadFile(filename=record.filename, file=stream)
                    reason, result_filename, chars = await self.service.extract_from_upload(upload)
                result = {
                    "draft_reason": reason,
                    "filename": result_filename,
                    "chars_processed": chars,
                }
                await asyncio.to_thread(self.store.mark_succeeded, job_id, result)
                delete_source = True
                logger.info(
                    "✅ 异步任务完成 | 任务={} | 耗时={:.2f}s | 文本长度={}字符 | 结果长度={}字符",
                    job_id,
                    time.perf_counter() - started_at,
                    chars,
                    len(reason),
                )
            except asyncio.CancelledError:
                await asyncio.to_thread(self.store.return_to_queue, job_id)
                logger.info("⏸️ 异步任务中断，已退回队列 | 任务={}", job_id)
                raise
            except ServiceError as exc:
                await asyncio.to_thread(self.store.mark_failed, job_id, exc.code, exc.message)
                delete_source = True
                logger.warning(
                    "❌ 异步任务失败 | 任务={} | 错误码={} | 耗时={:.2f}s",
                    job_id,
                    exc.code,
                    time.perf_counter() - started_at,
                )
            except Exception:
                await asyncio.to_thread(
                    self.store.mark_failed, job_id, "INTERNAL_ERROR", "任务处理失败"
                )
                delete_source = True
                logger.exception(
                    "❌ 异步任务异常 | 任务={} | 耗时={:.2f}s",
                    job_id,
                    time.perf_counter() - started_at,
                )
            finally:
                if delete_source:
                    await asyncio.to_thread(shutil.rmtree, record.file_path.parent, True)

    async def _cleanup_loop(self) -> None:
        interval = min(60, max(10, self.settings.async_job_ttl_seconds // 2))
        while True:
            await asyncio.sleep(interval)
            cutoff = time.time() - self.settings.async_job_ttl_seconds
            paths = await asyncio.to_thread(self.store.delete_expired, cutoff)
            for path in paths:
                await asyncio.to_thread(shutil.rmtree, path.parent, True)
            if paths:
                logger.debug("expired_async_jobs_removed count={}", len(paths))

    @staticmethod
    def _format_size(size_bytes: int) -> str:
        if size_bytes < 1024:
            return f"{size_bytes}B"
        if size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f}KB"
        return f"{size_bytes / (1024 * 1024):.1f}MB"
