"""Pytest configuration: make the lib package importable.

We insert the `provisioner/` directory (parent of this
`provisioner/tests/` directory) into sys.path so
`from lib.log import ...` works when pytest is run from the repo
root.

Layout reminder:
    proxmox-k3s/
    ├── provisioner/
    │   ├── lib/             <-- becomes importable as `lib`
    │   └── tests/conftest.py  <-- we are here
    └── pyproject.toml       <-- pytest testpaths = ["provisioner/tests"]
"""
from __future__ import annotations

import sys
from pathlib import Path

# provisioner/tests/conftest.py -> provisioner/ is its parent.
PROVISIONER_DIR = Path(__file__).resolve().parent.parent
if str(PROVISIONER_DIR) not in sys.path:
    sys.path.insert(0, str(PROVISIONER_DIR))
