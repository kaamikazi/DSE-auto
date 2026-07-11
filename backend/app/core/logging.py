import json
import logging
from datetime import UTC, datetime
from typing import Any

_REDACTED_KEYS = {"password", "token", "secret", "authorization", "api_key"}


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]"
            if any(part in key.lower() for part in _REDACTED_KEYS)
            else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(redact(payload), ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level.upper(), handlers=[handler], force=True)
