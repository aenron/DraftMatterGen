from app.prompts.draft_reason import SYSTEM_PROMPT


def test_prompt_defines_document_type_and_review_department_endings() -> None:
    assert "使用“报送相关部门阅示。”" in SYSTEM_PROMPT
    assert "报送信息化管理部门阅示" in SYSTEM_PROMPT
    assert "特此致函，恳请予以支持配合。" in SYSTEM_PROMPT
    assert "特此报告。" in SYSTEM_PROMPT
