"""Tests for lib.log.StructuredLogger.

Covers M4 (silent failures): the logger must emit one structured JSON object
per event to the audit log. Tests assert on the JSON shape, not console text
(console format is for humans; audit log is for agents).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from lib.log import StructuredLogger


def test_logger_writes_one_json_per_line(tmp_path: Path) -> None:
    """Each log call appends exactly one JSON line to the audit log."""
    log_path = tmp_path / "audit.log"
    logger = StructuredLogger("test", log_path=log_path)

    logger.info(step="hello", message="world", n=42)
    logger.error(step="oops", error="bad", resolution="retry")

    lines = log_path.read_text().strip().split("\n")
    assert len(lines) == 2

    parsed = [json.loads(line) for line in lines]
    assert parsed[0]["step"] == "hello"
    assert parsed[0]["level"] == "INFO"
    assert parsed[0]["message"] == "world"
    assert parsed[0]["n"] == 42
    assert "timestamp" in parsed[0]

    assert parsed[1]["step"] == "oops"
    assert parsed[1]["level"] == "ERROR"
    assert parsed[1]["error"] == "bad"
    assert parsed[1]["resolution"] == "retry"


def test_logger_redacts_keys_named_secret(caplog: pytest.LogCaptureFixture) -> None:
    """A log dict containing a key named 'secret' must not write its value to disk.

    M7/NFR-007: secrets never logged. We rely on the JSON-line audit log here;
    the same redaction is also enforced on the underlying Python logger.
    """
    log_path = Path("/tmp/test_logger_redacts.log")
    if log_path.exists():
        log_path.unlink()

    logger = StructuredLogger("test", log_path=log_path)
    logger.info(step="secret_test", token_value="should-be-redacted")

    # Read the audit line back.
    line = log_path.read_text().strip()
    parsed = json.loads(line)
    assert "token_value" not in parsed, (
        f"key 'token_value' must be redacted from audit log, got: {parsed}"
    )


def test_logger_redacts_nested_secrets(tmp_path: Path) -> None:
    """Redaction must apply recursively — nested dicts too.

    Redacted keys are DROPPED entirely (not replaced with [REDACTED]) so a
    log search never surfaces a key path that could carry secret context.
    """
    log_path = tmp_path / "audit.log"
    logger = StructuredLogger("test", log_path=log_path)

    logger.info(step="nested", data={"api_token": "should-disappear", "n": 1})

    parsed = json.loads(log_path.read_text().strip())
    assert "api_token" not in parsed["data"]
    assert parsed["data"]["n"] == 1


def test_logger_has_correlation_trace_id(tmp_path: Path) -> None:
    """Each log entry has a trace_id field for log correlation."""
    log_path = tmp_path / "audit.log"
    logger = StructuredLogger("test", log_path=log_path)
    logger.info(step="step1", foo="bar")

    parsed = json.loads(log_path.read_text().strip())
    assert "trace_id" in parsed
    assert isinstance(parsed["trace_id"], str)
    assert len(parsed["trace_id"]) >= 8
