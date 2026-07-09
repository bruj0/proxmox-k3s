"""Allow `python -m provisioner ...` invocation (matches the cicd repo's pattern)."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
