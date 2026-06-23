import asyncio
from pathlib import Path

import pytest

from app.parsers.docx_parser import DocxParser
from app.parsers.txt_parser import TxtParser


REFERENCE_DIR = Path(__file__).parents[1] / "参考文件"


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("样例1.docx", "上海智伏机电科技工程中心"),
        ("样例2.docx", "联想超融合平台"),
        ("样例3.docx", "国产化专用电脑及打印机"),
        ("样例4.docx", "网络系统安全隐患整改报告"),
    ],
)
def test_extract_reference_docx(filename: str, expected: str) -> None:
    text = asyncio.run(DocxParser().extract(REFERENCE_DIR / filename))
    assert expected in text


def test_extract_utf8_txt() -> None:
    text = asyncio.run(TxtParser().extract(REFERENCE_DIR / "拟稿事由1.txt"))
    assert text.startswith("拟稿事由：")
    assert "气体灭火系统" in text


def test_extract_gb18030_txt(tmp_path: Path) -> None:
    path = tmp_path / "sample.txt"
    path.write_bytes("中文编码测试".encode("gb18030"))
    assert asyncio.run(TxtParser().extract(path)) == "中文编码测试"
