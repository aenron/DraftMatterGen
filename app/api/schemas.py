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

