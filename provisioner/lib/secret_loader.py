"""SecretLoader — env-only credential loader.

Implements M7 (secrets never logged + never on disk in HCL):
  - reads from os.environ only;
  - refuses to substitute from disk files or defaults;
  - batch-loaded secrets can be logged as key lists only (never values).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .log import StructuredLogger


@dataclass
class SecretLoader:
    logger: StructuredLogger

    def get(self, name: str) -> str:
        """Read a single env var by canonical name. Raise on missing.

        The `resolution` field in the audit log points the operator at the
        env-var name so they can fix it without seeing the (never-logged)
        value.
        """
        value = os.environ.get(name)
        if value is None or value == "":
            self.logger.error(
                step="missing_secret",
                error=f"required env var {name} is not set",
                resolution=(
                    f"export {name}=<value> before running the script; "
                    f"the value is never logged"
                ),
            )
            raise RuntimeError(f"required env var {name} is not set")
        # Never log `value`. Just record that we read it.
        self.logger.info(step="secret_loaded", name=name)
        return value

    def get_many(self, names: list[str]) -> dict[str, str]:
        """Batch-load. If any are missing, emit error for the first one and raise.

        Returns a dict mapping canonical name → value. Only the keys are
        logged; values are never written to the audit log.
        """
        out: dict[str, str] = {}
        missing: list[str] = []
        for name in names:
            value = os.environ.get(name)
            if value is None or value == "":
                missing.append(name)
                continue
            out[name] = value
        if missing:
            self.logger.error(
                step="missing_secrets",
                error=f"missing env vars: {missing}",
                resolution=(
                    "export each var before running the script; "
                    "values are never logged"
                ),
                names=missing,
            )
            raise RuntimeError(f"missing env vars: {missing}")
        self.logger.info(
            step="secrets_loaded",
            count=len(names),
            names=sorted(names),
        )
        return out
