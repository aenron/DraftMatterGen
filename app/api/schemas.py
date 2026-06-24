from datetime import datetime

from pydantic import BaseModel


class DraftReasonData(BaseModel):
    draft_reason: str
    filename: str | None = None
    chars_processed: int | None = None


class DraftReasonResponse(BaseModel):
    code: int = 0
    message: str = "success"
    data: DraftReasonData
    request_id: str


class HealthResponse(BaseModel):
    status: str


class AsyncJobSubmissionData(BaseModel):
    job_id: str
    status: str
    status_url: str


class AsyncJobSubmissionResponse(BaseModel):
    code: int = 0
    message: str = "accepted"
    data: AsyncJobSubmissionData
    request_id: str


class AsyncJobError(BaseModel):
    code: str
    message: str


class AsyncJobData(BaseModel):
    job_id: str
    status: str
    submitted_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    result: DraftReasonData | None = None
    error: AsyncJobError | None = None


class AsyncJobResponse(BaseModel):
    code: int = 0
    message: str = "success"
    data: AsyncJobData
    request_id: str
