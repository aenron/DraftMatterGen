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
- 同时提供同步接口和异步任务接口；异步接口支持排队、状态查询、失败信息和结果过期清理。

## 本地运行

需要 Python 3.11 或更高版本。若需要解析 `.doc`，本机还需要安装 LibreOffice，并将 `LIBREOFFICE_BINARY` 配置为其可执行文件。

```bash
python -m venv .venv
pip install -r requirements-dev.txt
copy .env.example .env
uvicorn app.main:app --reload --no-access-log --log-level warning
```

如果仍看到 `Will watch for changes`、`Started reloader process` 等信息，说明实际启动命令没有带上 `--log-level warning`；这些日志由 Uvicorn reloader 父进程在应用加载前输出，应用内日志配置无法拦截。

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
TZ=Asia/Shanghai
```

生产环境可设置 `LOG_JSON=true` 输出精简 JSON 日志，方便日志平台采集。默认 `INFO` 级别下每个成功请求只输出一条业务汇总日志；排查文档解析或模型调用细节时临时设置 `LOG_LEVEL=DEBUG`。

一次正常请求会输出两条主要业务日志：

```text
2026-06-23 16:30:00.123 | INFO    | request-id | 📥 文件接收完成 | 文件名=样例1.docx | 类型=docx | 大小=13.5KB | 文本长度=180字符
2026-06-23 16:30:01.708 | INFO    | request-id | ✅ 请求完成 | 状态=200 | 耗时=1.59s | 结果长度=42字符 | 文件名=样例1.docx
```

模型服务需兼容：

```http
POST {LLM_BASE_URL}/chat/completions
```

如果模型服务不支持 `response_format={"type":"json_object"}`，设置：

```dotenv
LLM_RESPONSE_FORMAT_JSON=false
```

异步任务参数：

```dotenv
ASYNC_QUEUE_MAX_SIZE=100
ASYNC_WORKER_COUNT=2
ASYNC_JOB_TTL_SECONDS=3600
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

## 异步接口

提交任务后接口立即返回 HTTP `202`：

```bash
curl -X POST "http://localhost:8000/api/v1/draft-reasons/extract-async" \
  -H "X-API-Key: replace-me" \
  -F "file=@参考文件/样例1.docx"
```

响应示例：

```json
{
  "code": 0,
  "message": "accepted",
  "data": {
    "job_id": "a73cd05ff07f4d18bb4e0f7758255ec4",
    "status": "queued",
    "status_url": "http://localhost:8000/api/v1/draft-reasons/jobs/a73cd05ff07f4d18bb4e0f7758255ec4"
  },
  "request_id": "9ed20e33b8424cdf"
}
```

查询任务：

```bash
curl "http://localhost:8000/api/v1/draft-reasons/jobs/a73cd05ff07f4d18bb4e0f7758255ec4" \
  -H "X-API-Key: replace-me"
```

`status` 可能为 `queued`、`processing`、`succeeded` 或 `failed`。成功时 `result` 返回拟稿事由、文件名和处理字符数；失败时 `error` 返回错误码和说明。

当前异步队列及任务状态保存在单个服务进程内，容器已固定为一个 Uvicorn worker，实际任务并发由 `ASYNC_WORKER_COUNT` 控制。容器重启会清空未完成任务；如需多副本和任务持久化，应将任务层升级为 Redis/Celery 等外部队列。

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

镜像默认使用中国标准时间 `Asia/Shanghai`。修改 `TZ` 或时区相关配置后需要重新构建镜像，而不是只重启旧容器。

### 加速镜像构建

Dockerfile 使用 BuildKit 分别缓存 APT 软件包和 pip 下载文件，Compose 还会把完整构建缓存导出到项目的 `.docker-cache` 目录。日常构建直接执行：

```bash
docker compose build
docker compose up -d
```

不要在日常构建中使用 `--no-cache`，否则会主动放弃所有缓存。只有需要强制验证全新构建时才使用它。业务代码位于依赖安装层之后，因此仅修改 `app/` 通常只会重建最后几层，不会重新安装 LibreOffice 和 Python 依赖。

镜像以非 root 用户运行，根文件系统只读，临时文档写入容器的 `/tmp` 内存文件系统。原始文件在单次请求完成后删除。

## 测试

```bash
pytest -q
```

测试包含参考 DOCX/TXT 解析、上传校验、清理、API 鉴权、健康检查和 LLM JSON 解析。
