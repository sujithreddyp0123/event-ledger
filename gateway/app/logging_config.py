import json
import logging
from contextvars import ContextVar
from datetime import datetime, timezone

# ContextVar keeps the current trace ID scoped to one async request instead of
# leaking across concurrent requests handled by the same process.
trace_id_ctx: ContextVar[str | None] = ContextVar("trace_id", default=None)


class JsonFormatter(logging.Formatter):
    def __init__(self, service_name: str):
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "service": self.service_name,
            "message": record.getMessage(),
            "trace_id": getattr(record, "trace_id", None) or trace_id_ctx.get(),
        }
        return json.dumps(payload, default=str)


def configure_logging(service_name: str) -> logging.Logger:
    logger = logging.getLogger(service_name)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter(service_name))
    logger.addHandler(handler)
    logger.propagate = False
    return logger
