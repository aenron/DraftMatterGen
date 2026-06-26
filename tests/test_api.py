import asyncio
from io import BytesIO
from pathlib import Path
import time

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.errors import ServiceError
from app.main import create_app
from app.api.schemas import DocumentSummaryItem
from app.services.draft_reason_service import DraftReasonService


class FakeDraftReasonService:
    async def extract_from_upload(self, upload):
        await upload.read()
        await upload.close()
        return "根据工作需要，拟办理相关事项。", upload.filename, 120


class FailingDraftReasonService:
    async def extract_from_upload(self, upload):
        await upload.close()
        raise ServiceError(502, "LLM_UNAVAILABLE", "LLM 服务暂时不可用")


class BlockingDraftReasonService:
    async def extract_from_upload(self, upload):
        await asyncio.sleep(60)


class FakeDocumentSummaryService:
    async def summarize_uploads(self, uploads):
        results = []
        for upload in uploads:
            content = await upload.read()
            await upload.close()
            if upload.filename.endswith(".xlsx"):
                results.append(
                    DocumentSummaryItem(
                        filename=upload.filename,
                        status="ignored",
                        reason="xlsx 文件已按规则忽略",
                    )
                )
            else:
                results.append(
                    DocumentSummaryItem(
                        filename=upload.filename,
                        status="succeeded",
                        summary=f"摘要：{upload.filename}",
                        chars_processed=len(content),
                    )
                )
        return results


def make_client(
    tmp_path: Path,
    api_key_enabled: bool = False,
    service=None,
    summary_service=None,
) -> TestClient:
    settings = Settings(
        APP_ENV="test",
        API_KEY_ENABLED=api_key_enabled,
        SERVICE_API_KEY="test-secret",
        LLM_BASE_URL="http://llm.test/v1",
        LLM_MODEL="test-model",
        TEMP_DIR=tmp_path,
        ASYNC_DATA_DIR=tmp_path / "async-data",
    )
    app = create_app(settings, service or FakeDraftReasonService(), summary_service)
    return TestClient(app)


def test_extract_success(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    response = client.post(
        "/api/v1/draft-reasons/extract",
        files={"file": ("sample.txt", BytesIO("测试".encode()), "text/plain")},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 200
    assert body["data"]["draft_reason"] == "根据工作需要，拟办理相关事项。"
    assert body["data"]["filename"] is None
    assert response.headers["X-Request-ID"] == body["request_id"]


def test_extract_with_metadata(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    response = client.post(
        "/api/v1/draft-reasons/extract?include_metadata=true",
        files={"file": ("sample.txt", b"test", "text/plain")},
    )
    assert response.json()["data"]["filename"] == "sample.txt"
    assert response.json()["data"]["chars_processed"] == 120


def test_api_key(tmp_path: Path) -> None:
    client = make_client(tmp_path, api_key_enabled=True)
    unauthorized = client.post(
        "/api/v1/draft-reasons/extract",
        files={"file": ("sample.txt", b"test", "text/plain")},
    )
    assert unauthorized.status_code == 401
    authorized = client.post(
        "/api/v1/draft-reasons/extract",
        headers={"X-API-Key": "test-secret"},
        files={"file": ("sample.txt", b"test", "text/plain")},
    )
    assert authorized.status_code == 200


def test_document_summary_multi_file(tmp_path: Path) -> None:
    client = make_client(tmp_path, summary_service=FakeDocumentSummaryService())
    response = client.post(
        "/api/v1/document-summaries/extract",
        files=[
            ("files", ("sample.txt", b"test", "text/plain")),
            (
                "files",
                (
                    "budget.xlsx",
                    b"ignored",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                ),
            ),
        ],
    )
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 200
    assert body["data"]["summaries"][0]["status"] == "succeeded"
    assert body["data"]["summaries"][0]["summary"] == "摘要：sample.txt"
    assert body["data"]["summaries"][1]["status"] == "ignored"
    assert body["data"]["summaries"][1]["summary"] is None


def test_document_summary_service_uses_its_own_allowed_extensions(tmp_path: Path) -> None:
    settings = Settings(
        APP_ENV="test",
        LLM_BASE_URL="http://llm.test/v1",
        LLM_MODEL="test-model",
        TEMP_DIR=tmp_path,
        ASYNC_DATA_DIR=tmp_path / "async-data",
        ALLOWED_EXTENSIONS="docx,doc,txt",
        SUMMARY_ALLOWED_EXTENSIONS="docx,doc,pdf,txt,xlsx",
    )
    app = create_app(settings, DraftReasonService(settings))

    assert "pdf" not in app.state.draft_reason_service.document_service.allowed_extensions
    assert "pdf" in app.state.document_summary_service.document_service.allowed_extensions


def test_health(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    assert client.get("/health/live").json() == {"status": "ok"}
    assert client.get("/health/ready").json() == {"status": "ready"}


def test_missing_file_uses_standard_error(tmp_path: Path) -> None:
    response = make_client(tmp_path).post("/api/v1/draft-reasons/extract")
    assert response.status_code == 422
    assert response.json()["code"] == 422
    assert response.json()["error_code"] == "INVALID_REQUEST"


def test_async_extract_success(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        submitted = client.post(
            "/api/v1/draft-reasons/extract-async",
            files={"file": ("sample.txt", "异步测试".encode(), "text/plain")},
        )
        assert submitted.status_code == 202
        assert submitted.json()["code"] == 202
        submission = submitted.json()["data"]
        assert submission["status"] == "queued"
        assert submission["job_id"] in submission["status_url"]

        body = None
        for _ in range(50):
            response = client.get(f"/api/v1/draft-reasons/jobs/{submission['job_id']}")
            body = response.json()["data"]
            if body["status"] in {"succeeded", "failed"}:
                break
            time.sleep(0.01)

        assert body is not None
        assert body["status"] == "succeeded"
        assert body["result"]["draft_reason"] == "根据工作需要，拟办理相关事项。"
        assert body["result"]["filename"] == "sample.txt"
        assert body["result"]["chars_processed"] == 120
        assert body["error"] is None


def test_async_job_not_found(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        response = client.get("/api/v1/draft-reasons/jobs/not-found")
        assert response.status_code == 404
        assert response.json()["code"] == 404
        assert response.json()["error_code"] == "JOB_NOT_FOUND"


def test_async_extract_failure_is_queryable(tmp_path: Path) -> None:
    with make_client(tmp_path, service=FailingDraftReasonService()) as client:
        submitted = client.post(
            "/api/v1/draft-reasons/extract-async",
            files={"file": ("sample.txt", b"test", "text/plain")},
        ).json()["data"]

        body = None
        for _ in range(50):
            body = client.get(
                f"/api/v1/draft-reasons/jobs/{submitted['job_id']}"
            ).json()["data"]
            if body["status"] == "failed":
                break
            time.sleep(0.01)

        assert body is not None
        assert body["status"] == "failed"
        assert body["result"] is None
        assert body["error"] == {
            "code": "LLM_UNAVAILABLE",
            "message": "LLM 服务暂时不可用",
        }


def test_async_job_recovers_after_restart(tmp_path: Path) -> None:
    job_id = None
    with make_client(tmp_path, service=BlockingDraftReasonService()) as client:
        job_id = client.post(
            "/api/v1/draft-reasons/extract-async",
            files={"file": ("recover.txt", b"persistent content", "text/plain")},
        ).json()["data"]["job_id"]

        for _ in range(50):
            status = client.get(f"/api/v1/draft-reasons/jobs/{job_id}").json()["data"][
                "status"
            ]
            if status == "processing":
                break
            time.sleep(0.01)
        assert status == "processing"

    assert (tmp_path / "async-data" / "jobs.db").exists()

    with make_client(tmp_path) as restarted_client:
        body = None
        for _ in range(100):
            body = restarted_client.get(
                f"/api/v1/draft-reasons/jobs/{job_id}"
            ).json()["data"]
            if body["status"] in {"succeeded", "failed"}:
                break
            time.sleep(0.01)

        assert body is not None
        assert body["status"] == "succeeded"
        assert body["result"]["filename"] == "recover.txt"
