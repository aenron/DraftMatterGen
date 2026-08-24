from app.prompts.draft_reason import BASE_SYSTEM_PROMPT, SYSTEM_PROMPT, build_system_prompt


def test_prompt_defines_document_type_and_review_department_endings() -> None:
    assert "使用“报送相关部门阅示。”" in SYSTEM_PROMPT
    assert "报送信息化管理部门阅示" in SYSTEM_PROMPT
    assert "特此致函，恳请予以支持配合。" in SYSTEM_PROMPT
    assert "特此报告。" in SYSTEM_PROMPT


def test_explicit_letter_and_report_use_base_prompt() -> None:
    assert build_system_prompt("letter") == BASE_SYSTEM_PROMPT
    assert build_system_prompt("report") == BASE_SYSTEM_PROMPT


def test_request_or_submission_adds_department_rule() -> None:
    prompt = build_system_prompt("request_or_submission")
    assert "报送xx部门阅示。" in prompt
    assert "特此报告。" not in prompt
