"""StructuredLogger — dual console + JSON-line audit log.

Implements M4 (no silent failures): every event emits one JSON object per
line to the audit log with timestamp, level, step, trace_id, message, data.

Implements M7 (secrets never logged): keys whose names contain "secret" or
"token" (case-insensitive) are redacted to "[REDACTED]" recursively in any
log dict before it is written to disk.
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Key names that always get redacted when they appear as a JSON key in a log
# dict. We match by substring, case-insensitively.
_REDACT_KEYS = {"secret", "token", "password", "ssh_key", "sshkey"}


def _scrub(value: Any) -> Any:
    """Recursively redact any dict key whose name contains a redact substring.

    Lists are walked element-wise. Scalars pass through unchanged. Redacted
    keys are DROPPED entirely (not replaced with `[REDACTED]`) so a log
    query never accidentally surfaces the key path.
    """
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            k_lower = k.lower()
            if any(token in k_lower for token in _REDACT_KEYS):
                # Drop the key entirely.
                continue
            out[k] = _scrub(v)
        return out
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def _new_trace_id() -> str:
    """Generate a short, sort-friendly trace id (8 hex chars)."""
    return uuid.uuid4().hex[:8]


@dataclass
class StructuredLogger:
    """Dual console + JSON-line audit log writer.

    The audit log is one JSON object per line at `log_path`. Console output
    is single-line, colored (best effort), suitable for `make build-image`.
    """

    name: str
    log_path: Path | None = None
    verbose: bool = False
    _lock: threading.Lock = None  # type: ignore[assignment]
    _trace_id: str = ""

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._trace_id = _new_trace_id()
        if self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            # Touch the file so downstream readers see it immediately.
            if not self.log_path.exists():
                self.log_path.touch()

    # ----- public API -----

    def info(self, step: str, **fields: Any) -> None:
        self._emit("INFO", step, fields)

    def error(
        self, step: str, error: str, resolution: str, **fields: Any
    ) -> None:
        self._emit("ERROR", step, {"error": error, "resolution": resolution, **fields})

    def warn(self, step: str, message: str, **fields: Any) -> None:
        self._emit("WARNING", step, {"message": message, **fields})

    # ----- internals -----

    def _emit(self, level: str, step: str, fields: dict[str, Any]) -> None:
        record = {
            "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
            "level": level,
            "step": step,
            "trace_id": self._trace_id,
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "_scrubbed": True,
            **_scrub(fields),
        }

        with self._lock:
            # Audit log: append one JSON object per line.
            if self.log_path is not None:
                with self.log_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, separators=(",", ":")) + "\n")

        # Console: single line, no big dict dumps.
        try:
            msg = record.get("message") or record.get("error") or step
            print(f"[{level}] {step}: {msg}", flush=True)
        except Exception:  # noqa: BLE001 — never let logging crash the caller
            pass

    @property
    def trace_id(self) -> str:
        return self._trace_id


# Use logging only as a fallback sink for third-party libs; StructuredLogger
# is the canonical channel for our own events.
logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _one_line(text: str, *, limit: int = 240) -> str:
    """Collapse a multi-line string to a single line for log readability."""
    collapsed = re.sub(r"\s+", " ", text or "").strip()
    if len(collapsed) > limit:
        return collapsed[: limit - 1] + "…"
    return collapsed
