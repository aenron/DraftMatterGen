from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


class FakeDraftReasonService:
    async def extract_from_upload(self, upload):
        await upload.read()
        await upload.close()
        return "根据工作需要，拟办理相关事项。", upload.filename, 120


def make_client(tmp_path: Path, api_key_enabled: bool = False) -> TestClient:
    settings = Settings(
        APP_ENV="test",
        API_KEY_ENABLED=api_key_enabled,
        SERVICE_API_KEY="test-secret",
        LLM_BASE_URL="http://llm.test/v1",
        LLM_MODEL="test-model",
        TEMP_DIR=tmp_path,
    )
    app = create_app(settings, FakeDraftReasonService())
    return TestClient(app)


def test_extract_success(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    response = client.post(
        "/api/v1/draft-reasons/extract",
        files={"file": ("sample.txt", BytesIO("测试".encode()), "text/plain")},
    )
    assert response.status_code == 200
    body = response.json()
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


def test_health(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    assert client.get("/health/live").json() == {"status": "ok"}
    assert client.get("/health/ready").json() == {"status": "ready"}


def test_missing_file_uses_standard_error(tmp_path: Path) -> None:
    response = make_client(tmp_path).post("/api/v1/draft-reasons/extract")
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_REQUEST"
