import logging
import json
import sys

from loguru import logger

from app.core.config import Settings


class InterceptHandler(logging.Handler):
    """Forward standard-library logs (including Uvicorn) to Loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        frame = logging.currentframe()
        depth = 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def configure_logging(settings: Settings) -> None:
    def add_defaults(record: dict) -> None:
        record["extra"].setdefault("request_id", "-")
        record["extra"].setdefault("business", "-")
        record["extra"].setdefault("mode", "-")
        record["extra"].setdefault("job_id", "-")
        record["extra"].setdefault("filename", "-")

    logger.remove()
    logger.configure(patcher=add_defaults)
    if settings.log_json:
        def compact_json_sink(message) -> None:
            record = message.record
            payload = {
                "time": record["time"].isoformat(),
                "level": record["level"].name,
                "request_id": record["extra"].get("request_id", "-"),
                "business": record["extra"].get("business", "-"),
                "mode": record["extra"].get("mode", "-"),
                "job_id": record["extra"].get("job_id", "-"),
                "filename": record["extra"].get("filename", "-"),
                "message": record["message"],
            }
            sys.stderr.write(json.dumps(payload, ensure_ascii=False) + "\n")

        logger.add(
            compact_json_sink,
            level=settings.log_level.upper(),
            backtrace=False,
            diagnose=False,
        )
    else:
        logger.add(
            sys.stderr,
            level=settings.log_level.upper(),
            backtrace=False,
            diagnose=False,
            format=(
                "<green>{time:YYYY-MM-DD HH:mm:ss.S}</green> | "
                "<level>{level: <7}</level> | "
                "<cyan>{extra[request_id]}</cyan> | "
                "<cyan>业务={extra[business]} | 模式={extra[mode]} | "
                "任务={extra[job_id]} | 文件={extra[filename]}</cyan> | "
                "<level>{message}</level>"
            ),
        )

    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"):
        standard_logger = logging.getLogger(name)
        standard_logger.handlers = [InterceptHandler()]
        standard_logger.propagate = False

    # These libraries otherwise duplicate information already emitted by the
    # application request and LLM summary logs.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
