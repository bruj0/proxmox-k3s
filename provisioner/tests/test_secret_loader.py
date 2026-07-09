"""Tests for lib.secret_loader.SecretLoader.

Covers M7: secrets flow from env only, never logged. The loader refuses to
substitute from disk or hard-coded values.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from lib.log import StructuredLogger
from lib.secret_loader import SecretLoader


def test_load_returns_value_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FOO_TOKEN", "abc-123")
    loader = SecretLoader(StructuredLogger("t", log_path=tmp_path / "audit.log"))
    assert loader.get("FOO_TOKEN") == "abc-123"


def test_load_raises_when_env_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("FOO_TOKEN", raising=False)
    loader = SecretLoader(StructuredLogger("t", log_path=tmp_path / "audit.log"))
    with pytest.raises(RuntimeError, match="FOO_TOKEN"):
        loader.get("FOO_TOKEN")


def test_secret_value_never_in_audit_log(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Reading a secret must not leak its value into any log line."""
    monkeypatch.setenv("FOO_TOKEN", "super-secret-value-zzz")
    log_path = tmp_path / "audit.log"
    loader = SecretLoader(StructuredLogger("t", log_path=log_path))
    loader.get("FOO_TOKEN")

    content = log_path.read_text()
    assert "super-secret-value-zzz" not in content, (
        f"secret value leaked to log: {content!r}"
    )


def test_loader_supports_batch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("A_TOKEN", "a-val")
    monkeypatch.setenv("B_TOKEN", "b-val")
    loader = SecretLoader(StructuredLogger("t", log_path=tmp_path / "audit.log"))
    out = loader.get_many(["A_TOKEN", "B_TOKEN"])
    assert out == {"A_TOKEN": "a-val", "B_TOKEN": "b-val"}


def test_batch_raises_with_missing_keys(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("A_TOKEN", "a-val")
    monkeypatch.delenv("B_TOKEN", raising=False)
    loader = SecretLoader(StructuredLogger("t", log_path=tmp_path / "audit.log"))
    with pytest.raises(RuntimeError) as excinfo:
        loader.get_many(["A_TOKEN", "B_TOKEN"])
    assert "B_TOKEN" in str(excinfo.value)
