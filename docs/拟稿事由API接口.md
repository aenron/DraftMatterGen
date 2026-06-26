# 拟稿事由提取服务接口文档

## 1. 基本信息

| 项目 | 说明 |
|---|---|
| 服务名称 | 拟稿事由提取服务 |
| API 版本 | v1 |
| 默认服务地址 | `http://168.8.6.168:8002` |
| 数据格式 | JSON |
| 文件上传格式 | `multipart/form-data` |
| 支持文件类型 | 拟稿事由：`.docx`、`.doc`、`.txt`；文档摘要：`.docx`、`.doc`、`.pdf`、`.txt`，`.xlsx` 按规则忽略 |
| 默认文件大小上限 | 20 MB，可通过环境变量调整 |

交互式接口文档：

```text
http://168.8.6.168:8002/docs
```

## 2. 鉴权

当服务配置如下参数时启用接口鉴权：

```dotenv
API_KEY_ENABLED=true
SERVICE_API_KEY=your-api-key
```

调用业务接口时需要携带请求头：

```http
X-API-Key: your-api-key
```

未启用鉴权时不需要传递该请求头。

### 2.1 文件类型配置

拟稿事由接口和文档摘要接口使用不同的环境变量控制允许的文件类型：

```dotenv
# 拟稿事由接口
ALLOWED_EXTENSIONS=docx,doc,txt

# 文档摘要接口；xlsx 允许时会按规则忽略，不进入摘要处理
SUMMARY_ALLOWED_EXTENSIONS=docx,doc,pdf,txt,xlsx
```

## 3. 通用约定

### 3.1 请求 ID

服务为每次请求生成唯一请求 ID，并同时写入响应头和响应体：

```http
X-Request-ID: e0c26072fb8648199777a6852fc62042
```

调用方也可以主动传入 `X-Request-ID`。该值可用于关联接口响应和服务日志。

### 3.2 响应状态码

响应体中的 `code` 与 HTTP 状态码保持一致：

| HTTP 状态 | `code` | 说明 |
|---:|---:|---|
| 200 | 200 | 请求成功 |
| 202 | 202 | 异步任务已接收 |
| 400 | 400 | 文件名、文件内容或格式无效 |
| 401 | 401 | 接口鉴权失败 |
| 404 | 404 | 异步任务不存在或已过期 |
| 413 | 413 | 文件或文档内容超过限制 |
| 415 | 415 | 不支持的文件类型 |
| 422 | 422 | 文档无法解析或请求参数无效 |
| 502 | 502 | 模型服务异常或响应格式错误 |
| 503 | 503 | 模型未配置或异步队列已满 |
| 504 | 504 | 模型调用超时 |

### 3.3 通用错误响应

```json
{
  "code": 502,
  "error_code": "LLM_UNAVAILABLE",
  "message": "LLM 服务暂时不可用",
  "data": null,
  "request_id": "e0c26072fb8648199777a6852fc62042"
}
```

字段说明：

| 字段 | 类型 | 说明 |
|---|---|---|
| code | integer | 与 HTTP 状态码一致 |
| error_code | string | 程序可识别的业务错误码 |
| message | string | 错误说明 |
| data | null | 错误响应固定为 `null` |
| request_id | string | 请求唯一标识 |

## 4. 同步提取拟稿事由

上传文件并等待文档解析和大模型提取完成后返回结果。

### 4.1 请求

```http
POST /api/v1/draft-reasons/extract
Content-Type: multipart/form-data
```

查询参数：

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---:|---:|---|
| include_metadata | boolean | 否 | false | 是否返回文件名和处理字符数 |

表单参数：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| file | binary | 是 | DOCX、DOC 或 TXT 文件 |

### 4.2 cURL 示例

```bash
curl -X POST \
  "http://127.0.0.1:8000/api/v1/draft-reasons/extract?include_metadata=true" \
  -H "X-API-Key: your-api-key" \
  -F "file=@参考文件/样例1.docx"
```

### 4.3 成功响应

HTTP 状态码：`200 OK`

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "draft_reason": "为保障三地院区气体灭火系统的稳定运行，我办拟与原服务商续签维保服务合同。报院部阅示。",
    "filename": "样例1.docx",
    "chars_processed": 180
  },
  "request_id": "e0c26072fb8648199777a6852fc62042"
}
```

`include_metadata=false` 时，`filename` 和 `chars_processed` 返回 `null`。

字段说明：

| 字段 | 类型 | 说明 |
|---|---|---|
| data.draft_reason | string | 提取并归纳后的拟稿事由 |
| data.filename | string/null | 原始文件名 |
| data.chars_processed | integer/null | 从文档中提取并送入业务处理的文本字符数 |

## 5. 异步提交提取任务

文件持久化完成后立即返回任务 ID，不等待模型处理完成。

### 5.1 请求

```http
POST /api/v1/draft-reasons/extract-async
Content-Type: multipart/form-data
```

表单参数：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| file | binary | 是 | DOCX、DOC 或 TXT 文件 |

### 5.2 cURL 示例

```bash
curl -X POST \
  "http://127.0.0.1:8000/api/v1/draft-reasons/extract-async" \
  -H "X-API-Key: your-api-key" \
  -F "file=@参考文件/样例1.docx"
```

### 5.3 成功响应

HTTP 状态码：`202 Accepted`

```json
{
  "code": 202,
  "message": "accepted",
  "data": {
    "job_id": "a73cd05ff07f4d18bb4e0f7758255ec4",
    "status": "queued",
    "status_url": "http://127.0.0.1:8000/api/v1/draft-reasons/jobs/a73cd05ff07f4d18bb4e0f7758255ec4"
  },
  "request_id": "9ed20e33b8424cdf"
}
```

字段说明：

| 字段 | 类型 | 说明 |
|---|---|---|
| data.job_id | string | 异步任务唯一标识 |
| data.status | string | 提交成功时通常为 `queued` |
| data.status_url | string | 任务状态查询地址 |

异步任务状态和源文件使用 SQLite 及持久化目录保存。服务重启后，未完成任务会重新进入队列。

## 6. 查询异步任务

### 6.1 请求

```http
GET /api/v1/draft-reasons/jobs/{job_id}
```

路径参数：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| job_id | string | 是 | 提交异步任务时返回的任务 ID |

### 6.2 cURL 示例

```bash
curl \
  "http://127.0.0.1:8000/api/v1/draft-reasons/jobs/a73cd05ff07f4d18bb4e0f7758255ec4" \
  -H "X-API-Key: your-api-key"
```

### 6.3 任务状态

| 状态 | 说明 | result | error |
|---|---|---|---|
| queued | 等待后台 worker 处理 | null | null |
| processing | 正在解析文档或调用模型 | null | null |
| succeeded | 任务处理成功 | 有值 | null |
| failed | 任务处理失败 | null | 有值 |

典型状态流转：

```text
queued -> processing -> succeeded
                     -> failed
```

### 6.4 排队或处理中响应

HTTP 状态码：`200 OK`

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "job_id": "a73cd05ff07f4d18bb4e0f7758255ec4",
    "status": "processing",
    "submitted_at": "2026-06-24T02:10:00.000000Z",
    "started_at": "2026-06-24T02:10:00.120000Z",
    "completed_at": null,
    "result": null,
    "error": null
  },
  "request_id": "1c55f958ebf44bb096f31f14c8d84b3d"
}
```

### 6.5 成功响应

HTTP 状态码：`200 OK`

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "job_id": "a73cd05ff07f4d18bb4e0f7758255ec4",
    "status": "succeeded",
    "submitted_at": "2026-06-24T02:10:00.000000Z",
    "started_at": "2026-06-24T02:10:00.120000Z",
    "completed_at": "2026-06-24T02:10:01.708000Z",
    "result": {
      "draft_reason": "为保障三地院区气体灭火系统的稳定运行，我办拟与原服务商续签维保服务合同。报院部阅示。",
      "filename": "样例1.docx",
      "chars_processed": 180
    },
    "error": null
  },
  "request_id": "1c55f958ebf44bb096f31f14c8d84b3d"
}
```

### 6.6 任务失败响应

任务查询本身成功，因此 HTTP 状态码和响应体 `code` 仍为 `200`。任务执行结果由 `data.status` 判断。

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "job_id": "a73cd05ff07f4d18bb4e0f7758255ec4",
    "status": "failed",
    "submitted_at": "2026-06-24T02:10:00.000000Z",
    "started_at": "2026-06-24T02:10:00.120000Z",
    "completed_at": "2026-06-24T02:11:00.120000Z",
    "result": null,
    "error": {
      "code": "LLM_TIMEOUT",
      "message": "LLM 服务调用超时"
    }
  },
  "request_id": "1c55f958ebf44bb096f31f14c8d84b3d"
}
```

调用方轮询建议：

1. 提交任务并保存 `job_id`。
2. 每 1～3 秒查询一次任务状态。
3. `queued` 或 `processing` 时继续等待。
4. `succeeded` 时读取 `result` 并停止轮询。
5. `failed` 时读取 `error` 并停止轮询。
6. 查询返回 `404` 时，任务可能不存在或已超过任务保留时间。

## 7. 健康检查

### 7.1 存活检查

```http
GET /health/live
```

成功响应：

```json
{
  "status": "ok"
}
```

### 7.2 就绪检查

```http
GET /health/ready
```

LLM 地址和模型名称已配置时返回：

```json
{
  "status": "ready"
}
```

该接口只检查必要配置是否存在，不会实际调用模型。

## 8. 业务错误码

| error_code | HTTP 状态 | 说明 |
|---|---:|---|
| INVALID_FILENAME | 400 | 文件名或扩展名无效 |
| NO_FILES | 400 | 未上传任何文件 |
| EMPTY_FILE | 400 | 上传文件为空 |
| FILE_SIGNATURE_MISMATCH | 400 | 文件扩展名和实际内容不一致 |
| UNAUTHORIZED | 401 | API Key 无效或缺失 |
| JOB_NOT_FOUND | 404 | 异步任务不存在或已过期 |
| FILE_TOO_LARGE | 413 | 上传文件超过大小限制 |
| TOO_MANY_FILES | 413 | 一次上传文件数超过限制 |
| DOCUMENT_TOO_LONG | 413 | 文档内容超过处理范围 |
| UNSUPPORTED_FILE_TYPE | 415 | 文件类型不受支持 |
| INVALID_REQUEST | 422 | 请求参数校验失败 |
| NO_READABLE_TEXT | 422 | 文档中未提取到可读文字 |
| DOCUMENT_PARSE_FAILED | 422 | 文档解析失败 |
| DOCUMENT_CONVERSION_FAILED | 422 | DOC 转换失败 |
| DOCUMENT_CONVERSION_TIMEOUT | 422 | DOC 转换超时 |
| LLM_REQUEST_REJECTED | 502 | 模型服务拒绝请求 |
| LLM_INVALID_RESPONSE | 502 | 模型响应格式不符合要求 |
| LLM_UNAVAILABLE | 502 | 模型服务暂时不可用 |
| LLM_NOT_CONFIGURED | 503 | 模型地址或模型名称未配置 |
| PDF_PARSER_UNAVAILABLE | 500 | PDF 解析组件不可用 |
| ASYNC_QUEUE_FULL | 503 | 异步任务队列已满 |
| LLM_TIMEOUT | 504 | 模型调用超时 |

## 9. 文档摘要生成

一次上传多个文件，服务逐个解析并生成主要内容摘要。`.xlsx` 文件不会进入解析和模型处理，返回 `ignored` 状态。单个文件解析或摘要失败时，不影响其他文件，失败原因会写入该文件结果。

长文档会优先使用 PDF 前几页或文档开头内容、疑似目录，并对正文进行有限切片摘要；模型调用受全局 `LLM_MAX_CONCURRENCY` 控制，切片调用默认顺序执行。

### 9.1 请求

```http
POST /api/v1/document-summaries/extract
Content-Type: multipart/form-data
```

表单参数：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| files | binary[] | 是 | 多个 DOCX、DOC、PDF、TXT 或 XLSX 文件 |

### 9.2 cURL 示例

```bash
curl -X POST \
  "http://127.0.0.1:8000/api/v1/document-summaries/extract" \
  -H "X-API-Key: your-api-key" \
  -F "files=@申报书.pdf" \
  -F "files=@附件说明.docx" \
  -F "files=@经费预算.xlsx"
```

### 9.3 成功响应

HTTP 状态码：`200 OK`

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "summaries": [
      {
        "filename": "申报书.pdf",
        "status": "succeeded",
        "summary": "本文档主要围绕……",
        "reason": null,
        "chars_processed": 18000
      },
      {
        "filename": "经费预算.xlsx",
        "status": "ignored",
        "summary": null,
        "reason": "xlsx 文件已按规则忽略",
        "chars_processed": null
      }
    ]
  },
  "request_id": "e0c26072fb8648199777a6852fc62042"
}
```

字段说明：

| 字段 | 类型 | 说明 |
|---|---|---|
| data.summaries[].filename | string | 原始文件名 |
| data.summaries[].status | string | `succeeded`、`ignored` 或 `failed` |
| data.summaries[].summary | string/null | 生成的文档摘要 |
| data.summaries[].reason | string/null | 忽略或失败原因 |
| data.summaries[].chars_processed | integer/null | 从文档中提取并参与处理的文本字符数 |

## 10. Python 调用示例

### 10.1 同步调用

```python
import requests

url = "http://127.0.0.1:8000/api/v1/draft-reasons/extract"
headers = {"X-API-Key": "your-api-key"}

with open("样例1.docx", "rb") as file:
    response = requests.post(
        url,
        headers=headers,
        params={"include_metadata": "true"},
        files={"file": ("样例1.docx", file)},
        timeout=180,
    )

response.raise_for_status()
print(response.json()["data"]["draft_reason"])
```

### 10.2 异步调用及轮询

```python
import time
import requests

base_url = "http://127.0.0.1:8000"
headers = {"X-API-Key": "your-api-key"}

with open("样例1.docx", "rb") as file:
    response = requests.post(
        f"{base_url}/api/v1/draft-reasons/extract-async",
        headers=headers,
        files={"file": ("样例1.docx", file)},
        timeout=30,
    )

response.raise_for_status()
job_id = response.json()["data"]["job_id"]

while True:
    response = requests.get(
        f"{base_url}/api/v1/draft-reasons/jobs/{job_id}",
        headers=headers,
        timeout=10,
    )
    response.raise_for_status()
    job = response.json()["data"]

    if job["status"] == "succeeded":
        print(job["result"]["draft_reason"])
        break
    if job["status"] == "failed":
        raise RuntimeError(job["error"])

    time.sleep(2)
```
