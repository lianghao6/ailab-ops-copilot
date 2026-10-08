"""Copy and redact telemetry before retaining it or writing JSONL."""

from copy import deepcopy
from datetime import datetime, timezone
from enum import Enum
import json
import math
from pathlib import Path
import re
from typing import Any, Callable, Iterable

from .events import TraceEvent


class TraceRecorder:
    REDACTED = "[REDACTED]"
    DEFAULT_FIELDS = {"secret", "api_key", "authorization", "token", "password", "credential",
                      "cookie", "access_key", "private_key"}

    def __init__(self, *, sensitive_fields: Iterable[str] = (), secrets: Iterable[str] = (),
                 now: Callable[[], datetime] | None = None):
        fields = self.DEFAULT_FIELDS | set(sensitive_fields)
        self._fields = {self._key(key) for key in fields}
        names = "|".join("[-_ ]?".join(re.escape(part) for part in re.split(r"[-_ ]", key))
                         for key in fields)
        self._credential_text = re.compile(
            rf"(?i)\b([a-z0-9_-]*(?:{names}))[\"']?\s*[:=]\s*(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;\"']+)")
        # Quoted cookies can contain arbitrary separators and more cookie fields.
        # Remove the full header line; only CR/LF or end-of-string is a boundary.
        self._header_text = re.compile(
            r"(?i)\b(authorization|cookie)[\"']?\s*[:=][^\r\n]*")
        self._secrets = tuple(sorted({secret for secret in secrets if secret}, key=len, reverse=True))
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._events: list[TraceEvent] = []

    @staticmethod
    def _key(key: str) -> str:
        return re.sub(r"[^a-z0-9]", "", key.casefold())

    def _sensitive(self, key: str) -> bool:
        normalized = self._key(key)
        return any(normalized == field or normalized.endswith(field) for field in self._fields)

    def redact(self, value: Any) -> Any:
        """Redact nested keys, configured secret values and credential-shaped text.

        Unknown objects are omitted rather than using repr, which may contain
        credentials. Usage names such as tokens_in remain numeric telemetry.
        """
        if isinstance(value, dict):
            return {self._text(str(key)): self.REDACTED if self._sensitive(str(key)) else self.redact(item)
                    for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.redact(item) for item in value]
        if isinstance(value, Enum):
            return self.redact(value.value)
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, str):
            # JSON strings can contain nested credential keys too.
            try:
                decoded = json.loads(value)
            except (ValueError, TypeError):
                decoded = None
            if isinstance(decoded, (dict, list)):
                return json.dumps(self.redact(decoded), ensure_ascii=False, allow_nan=False)
            return self._text(value)
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else None
        return "[UNSUPPORTED]"

    def _text(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, self.REDACTED)
        text = self._header_text.sub(r"\1=" + self.REDACTED, text)
        text = re.sub(r"(?i)\b(Bearer|Basic)\s+[^\s,;\"']+", r"\1 " + self.REDACTED, text)
        text = self._credential_text.sub(r"\1=" + self.REDACTED, text)
        return text

    def record(self, event: TraceEvent) -> None:
        if not isinstance(event, TraceEvent):
            raise TypeError("TraceRecorder expects a TraceEvent")
        data = event.to_dict()
        data["timestamp"] = data["timestamp"] or self._now().isoformat()
        self._events.append(TraceEvent(**self.redact(data)))

    @property
    def events(self) -> tuple[TraceEvent, ...]:
        return tuple(deepcopy(self._events))

    def write_jsonl(self, path: Path) -> None:
        """Write only already-redacted snapshots; IO errors belong to the caller."""
        with Path(path).open("w", encoding="utf-8") as stream:
            for event in self._events:
                stream.write(json.dumps(event.to_dict(), ensure_ascii=False, allow_nan=False) + "\n")
