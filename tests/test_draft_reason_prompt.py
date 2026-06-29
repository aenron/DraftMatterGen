from app.prompts.draft_reason import SYSTEM_PROMPT


def test_prompt_derives_review_department_from_document() -> None:
    assert "不得固定写成“报院部阅示”" in SYSTEM_PROMPT
    assert "无法可靠判断时，统一使用“报送相关部门阅示”" in SYSTEM_PROMPT
    assert "报送信息化管理部门阅示" in SYSTEM_PROMPT
