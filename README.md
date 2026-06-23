# 拟稿事由提取服务

基于 FastAPI 的文档拟稿事由提取接口。服务接收 DOCX、DOC 或 TXT 文件，提取正文后调用 OpenAI 兼容的大模型接口，返回归纳后的“拟稿事由”。

## 功能

- 支持 DOCX、旧版 DOC、TXT。
- DOCX 同时提取普通段落和表格内容。
- DOC 通过 LibreOffice 无界面转换。
- TXT 支持 UTF-8、GB18030 和编码自动识别。
- 校验文件大小、扩展名和文件实际格式。
- LLM 超时、重试、JSON 响应校验和错误映射。
- 长文档分段提取后二次合并。
- 支持 API Key 鉴权、请求 ID 和容器健康检查。
- 使用 Loguru 输出请求、解析、模型调用、耗时和异常日志，不记录文档正文或密钥。

## 本地运行

需要 Python 3.11 或更高版本。若需要解析 `.doc`，本机还需要安装 LibreOffice，并将 `LIBREOFFICE_BINARY` 配置为其可执行文件。

```bash
python -m venv .venv
pip install -r requirements-dev.txt
copy .env.example .env
uvicorn app.main:app --reload
```

编辑 `.env`，至少填写：

```dotenv
LLM_BASE_URL=https://your-llm-host/v1
LLM_API_KEY=your-key
LLM_MODEL=your-model
```

日志配置：

```dotenv
LOG_LEVEL=INFO
LOG_JSON=false
```

生产环境可设置 `LOG_JSON=true` 输出 JSON 日志，方便日志平台采集。

模型服务需兼容：

```http
POST {LLM_BASE_URL}/chat/completions
```

如果模型服务不支持 `response_format={"type":"json_object"}`，设置：

```dotenv
LLM_RESPONSE_FORMAT_JSON=false
```

## 调用接口

```bash
curl -X POST "http://localhost:8000/api/v1/draft-reasons/extract" \
  -H "X-API-Key: replace-me" \
  -F "file=@参考文件/样例1.docx"
```

返回示例：

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "draft_reason": "为保障三院地区机房气体灭火系统的稳定运行，我办拟与原服务商续签维保服务合同。报院部阅示。",
    "filename": null,
    "chars_processed": null
  },
  "request_id": "9ed20e33b8424cdf"
}
```

传入 `?include_metadata=true` 可返回文件名和处理字符数。

接口文档：`http://localhost:8000/docs`

健康检查：

```text
GET /health/live
GET /health/ready
```

## Docker 部署

```bash
copy .env.example .env
docker compose up --build -d
```

镜像以非 root 用户运行，根文件系统只读，临时文档写入容器的 `/tmp` 内存文件系统。原始文件在单次请求完成后删除。

## 测试

```bash
pytest -q
```

测试包含参考 DOCX/TXT 解析、上传校验、清理、API 鉴权、健康检查和 LLM JSON 解析。
