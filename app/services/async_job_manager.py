import asyncio
import json
import os
import re
import shutil
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import UploadFile
from loguru import logger

from app.core.config import Settings
from app.core.errors import ServiceError
from app.services.draft_reason_service import DraftReasonService
from app.services.document_summary_service import (
    SUMMARY_PARSEABLE_EXTENSIONS,
    DocumentSummaryService,
)


DEFAULT_TIMEZONE = "Asia/Shanghai"


def current_datetime() -> datetime:
    timezone_name = os.getenv("TZ", DEFAULT_TIMEZONE)
    try:
        return datetime.now(ZoneInfo(timezone_name))
    except ZoneInfoNotFoundError:
        return datetime.now().astimezone()


class JobStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class JobType(StrEnum):
    DRAFT_REASON = "draft_reason"
    DOCUMENT_SUMMARY = "document_summary"


@dataclass
class JobRecord:
    job_id: str
    job_type: JobType
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
                    job_type TEXT NOT NULL DEFAULT 'draft_reason',
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
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
            }
            if "job_type" not in columns:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN job_type TEXT NOT NULL DEFAULT 'draft_reason'"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_status_updated ON jobs(status, updated_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_type_status ON jobs(job_type, status)"
            )

    def create(self, record: JobRecord) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    job_id, job_type, filename, file_path, request_id, status,
                    submitted_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.job_id,
                    record.job_type.value,
                    record.filename,
                    str(record.file_path),
                    record.request_id,
                    record.status.value,
                    record.submitted_at.isoformat(),
                    record.updated_at,
                ),
            )

    def get(self, job_id: str, job_type: JobType | None = None) -> JobRecord | None:
        with closing(self._connect()) as connection, connection:
            if job_type is None:
                row = connection.execute(
                    "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM jobs WHERE job_id = ? AND job_type = ?",
                    (job_id, job_type.value),
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
        now = current_datetime()
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
        now = current_datetime()
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
        now = current_datetime()
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

    def delete_expired(self, cutoff: float) -> list[tuple[Path, JobType]]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT file_path, job_type FROM jobs
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
        return [(Path(row["file_path"]), JobType(row["job_type"])) for row in rows]

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
            job_type=JobType(row["job_type"]),
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
    def __init__(
        self,
        settings: Settings,
        draft_reason_service: DraftReasonService,
        document_summary_service: DocumentSummaryService,
    ) -> None:
        self.settings = settings
        self.draft_reason_service = draft_reason_service
        self.document_summary_service = document_summary_service
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=settings.async_queue_max_size)
        self.workers: list[asyncio.Task] = []
        self.cleanup_task: asyncio.Task | None = None
        self.data_dir = settings.async_data_dir
        self.uploads_dir = self.data_dir / "uploads"
        self.summary_uploads_dir = self.data_dir / "summary-uploads"
        self.store = SQLiteJobStore(self.data_dir / "jobs.db")

    async def start(self) -> None:
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        self.summary_uploads_dir.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self.store.initialize)
        recovered = await asyncio.to_thread(self.store.recover_queued)
        self.queue = asyncio.Queue(maxsize=self.settings.async_queue_max_size)
        self.workers = [
            asyncio.create_task(self._worker(index + 1), name=f"async-worker-{index + 1}")
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
            job_type=JobType.DRAFT_REASON,
            filename=filename,
            file_path=file_path,
            request_id=request_id,
            status=JobStatus.QUEUED,
            submitted_at=current_datetime(),
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

    async def submit_document_summary(
        self, uploads: list[UploadFile], request_id: str
    ) -> JobRecord:
        if not uploads:
            raise ServiceError(400, "NO_FILES", "请至少上传一个文件")
        if len(uploads) > self.settings.summary_max_files:
            for upload in uploads:
                await upload.close()
            raise ServiceError(
                413,
                "TOO_MANY_FILES",
                f"一次最多上传 {self.settings.summary_max_files} 个文件",
            )
        if self.queue.full():
            for upload in uploads:
                await upload.close()
            raise ServiceError(503, "ASYNC_QUEUE_FULL", "异步任务队列已满，请稍后重试")

        job_id = uuid.uuid4().hex
        job_dir = self.summary_uploads_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=False)
        files_metadata: list[dict[str, str]] = []
        total_size = 0
        try:
            for index, upload in enumerate(uploads):
                filename = Path(upload.filename or "").name
                suffix = Path(filename).suffix.lower().lstrip(".")
                if not filename or not suffix:
                    raise ServiceError(400, "INVALID_FILENAME", "文件名或扩展名无效")
                if suffix not in self.settings.summary_allowed_extension_set:
                    raise ServiceError(415, "UNSUPPORTED_FILE_TYPE", f"不支持的文件类型: .{suffix}")
                if suffix != "xlsx" and suffix not in SUMMARY_PARSEABLE_EXTENSIONS:
                    raise ServiceError(415, "UNSUPPORTED_FILE_TYPE", f"不支持的文件类型: .{suffix}")

                safe_stem = re.sub(r"[^A-Za-z0-9_-]", "_", Path(filename).stem)[:80] or "input"
                stored_name = f"{index + 1:03d}-{safe_stem}.{suffix}"
                target = job_dir / stored_name
                size_bytes = await self._save_upload(upload, target)
                total_size += size_bytes
                files_metadata.append({"filename": filename, "path": stored_name})
        except Exception:
            await asyncio.to_thread(shutil.rmtree, job_dir, True)
            raise
        finally:
            for upload in uploads:
                await upload.close()

        await asyncio.to_thread(self._write_manifest, job_dir, files_metadata)
        filenames = ",".join(file["filename"] for file in files_metadata)
        record = JobRecord(
            job_id=job_id,
            job_type=JobType.DOCUMENT_SUMMARY,
            filename=filenames,
            file_path=job_dir,
            request_id=request_id,
            status=JobStatus.QUEUED,
            submitted_at=current_datetime(),
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
            "🕒 文档摘要异步任务已持久化 | 任务={} | 文件数={} | 大小={} | 排队任务={}",
            job_id,
            len(files_metadata),
            self._format_size(total_size),
            self.queue.qsize(),
        )
        return record

    async def get(self, job_id: str, job_type: JobType | None = None) -> dict[str, Any]:
        record = await asyncio.to_thread(self.store.get, job_id, job_type)
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

        if record.job_type == JobType.DOCUMENT_SUMMARY:
            await self._process_document_summary(record, worker_number)
            return

        await self._process_draft_reason(record, worker_number)

    async def _process_draft_reason(self, record: JobRecord, worker_number: int) -> None:
        started_at = time.perf_counter()
        delete_source = False
        with logger.contextualize(request_id=record.request_id):
            logger.info(
                "▶️ 异步任务开始 | 任务={} | worker={} | 文件名={}",
                record.job_id,
                worker_number,
                record.filename,
            )
            try:
                with record.file_path.open("rb") as stream:
                    upload = UploadFile(filename=record.filename, file=stream)
                    reason, result_filename, chars = (
                        await self.draft_reason_service.extract_from_upload(upload)
                    )
                result = {
                    "draft_reason": reason,
                    "filename": result_filename,
                    "chars_processed": chars,
                }
                await asyncio.to_thread(self.store.mark_succeeded, record.job_id, result)
                delete_source = True
                logger.info(
                    "✅ 异步任务完成 | 任务={} | 耗时={:.2f}s | 文本长度={}字符 | 结果长度={}字符",
                    record.job_id,
                    time.perf_counter() - started_at,
                    chars,
                    len(reason),
                )
            except asyncio.CancelledError:
                await asyncio.to_thread(self.store.return_to_queue, record.job_id)
                logger.info("⏸️ 异步任务中断，已退回队列 | 任务={}", record.job_id)
                raise
            except ServiceError as exc:
                await asyncio.to_thread(self.store.mark_failed, record.job_id, exc.code, exc.message)
                delete_source = True
                logger.warning(
                    "❌ 异步任务失败 | 任务={} | 错误码={} | 耗时={:.2f}s",
                    record.job_id,
                    exc.code,
                    time.perf_counter() - started_at,
                )
            except Exception:
                await asyncio.to_thread(
                    self.store.mark_failed, record.job_id, "INTERNAL_ERROR", "任务处理失败"
                )
                delete_source = True
                logger.exception(
                    "❌ 异步任务异常 | 任务={} | 耗时={:.2f}s",
                    record.job_id,
                    time.perf_counter() - started_at,
                )
            finally:
                if delete_source:
                    await asyncio.to_thread(shutil.rmtree, record.file_path.parent, True)

    async def _process_document_summary(self, record: JobRecord, worker_number: int) -> None:
        started_at = time.perf_counter()
        delete_source = False
        with logger.contextualize(request_id=record.request_id):
            logger.info(
                "▶️ 文档摘要异步任务开始 | 任务={} | worker={} | 文件名={}",
                record.job_id,
                worker_number,
                record.filename,
            )
            try:
                uploads = await asyncio.to_thread(self._open_summary_uploads, record.file_path)
                try:
                    summaries = await self.document_summary_service.summarize_uploads(uploads)
                finally:
                    for upload in uploads:
                        await upload.close()
                result = {"summaries": [item.model_dump() for item in summaries]}
                await asyncio.to_thread(self.store.mark_succeeded, record.job_id, result)
                delete_source = True
                logger.info(
                    "✅ 文档摘要异步任务完成 | 任务={} | 耗时={:.2f}s | 文件数={} | 结果长度={}字符",
                    record.job_id,
                    time.perf_counter() - started_at,
                    len(summaries),
                    sum(len(item.summary or "") for item in summaries),
                )
            except asyncio.CancelledError:
                await asyncio.to_thread(self.store.return_to_queue, record.job_id)
                logger.info("⏸️ 文档摘要异步任务中断，已退回队列 | 任务={}", record.job_id)
                raise
            except ServiceError as exc:
                await asyncio.to_thread(self.store.mark_failed, record.job_id, exc.code, exc.message)
                delete_source = True
                logger.warning(
                    "❌ 文档摘要异步任务失败 | 任务={} | 错误码={} | 耗时={:.2f}s",
                    record.job_id,
                    exc.code,
                    time.perf_counter() - started_at,
                )
            except Exception:
                await asyncio.to_thread(
                    self.store.mark_failed, record.job_id, "INTERNAL_ERROR", "任务处理失败"
                )
                delete_source = True
                logger.exception(
                    "❌ 文档摘要异步任务异常 | 任务={} | 耗时={:.2f}s",
                    record.job_id,
                    time.perf_counter() - started_at,
                )
            finally:
                if delete_source:
                    await asyncio.to_thread(shutil.rmtree, record.file_path, True)

    async def _save_upload(self, upload: UploadFile, target: Path) -> int:
        total = 0
        with target.open("wb") as output:
            while chunk := await upload.read(1024 * 1024):
                total += len(chunk)
                if total > self.settings.upload_max_bytes:
                    raise ServiceError(
                        413,
                        "FILE_TOO_LARGE",
                        f"文件不能超过 {self.settings.upload_max_mb} MB",
                    )
                output.write(chunk)
        if total == 0:
            raise ServiceError(400, "EMPTY_FILE", "上传文件为空")
        return total

    @staticmethod
    def _write_manifest(job_dir: Path, files_metadata: list[dict[str, str]]) -> None:
        (job_dir / "manifest.json").write_text(
            json.dumps(files_metadata, ensure_ascii=False), encoding="utf-8"
        )

    @staticmethod
    def _open_summary_uploads(job_dir: Path) -> list[UploadFile]:
        files_metadata = json.loads((job_dir / "manifest.json").read_text(encoding="utf-8"))
        uploads: list[UploadFile] = []
        try:
            for item in files_metadata:
                stream = (job_dir / item["path"]).open("rb")
                uploads.append(UploadFile(filename=item["filename"], file=stream))
            return uploads
        except Exception:
            for upload in uploads:
                upload.file.close()
            raise

    async def _cleanup_loop(self) -> None:
        interval = min(60, max(10, self.settings.async_job_ttl_seconds // 2))
        while True:
            await asyncio.sleep(interval)
            cutoff = time.time() - self.settings.async_job_ttl_seconds
            expired = await asyncio.to_thread(self.store.delete_expired, cutoff)
            for path, job_type in expired:
                target = path if job_type == JobType.DOCUMENT_SUMMARY else path.parent
                await asyncio.to_thread(shutil.rmtree, target, True)
            if expired:
                logger.debug("expired_async_jobs_removed count={}", len(expired))

    @staticmethod
    def _format_size(size_bytes: int) -> str:
        if size_bytes < 1024:
            return f"{size_bytes}B"
        if size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f}KB"
        return f"{size_bytes / (1024 * 1024):.1f}MB"
