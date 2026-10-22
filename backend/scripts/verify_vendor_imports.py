"""Import-resolution verification pass over the vendored integrations tree.

Walks every module under `vendor.integrations` (via pkgutil) and attempts
`importlib.import_module` on each, collecting any ImportError/
ModuleNotFoundError. This is the concrete realization of FEAT-001's
acceptance criterion "every module under vendor.integrations imports
successfully via the verification script, with zero failures reported."

Run from `ToolboxStudio/backend/` with the venv active:
    .venv\\Scripts\\python scripts/verify_vendor_imports.py
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
from pathlib import Path

# Make `app` and `vendor` importable when run as a plain script from
# backend/ (mirrors how `python -c "import app"` is invoked in practice).
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# Importing `app` triggers install_os_trust_store() and the
# app.integrations -> vendor.integrations alias wiring, exactly as it
# would for any real caller of the vendored code.
import app  # noqa: E402,F401
import vendor.integrations as vendor_integrations  # noqa: E402


def main() -> int:
    failures: list[tuple[str, str]] = []
    module_count = 0

    for module_info in pkgutil.iter_modules(
        vendor_integrations.__path__, prefix="vendor.integrations."
    ):
        module_count += 1
        name = module_info.name
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - we want to report ANY failure
            failures.append((name, f"{type(exc).__name__}: {exc}"))

    print(f"Checked {module_count} modules under vendor.integrations.")
    if failures:
        print(f"{len(failures)} import failure(s):")
        for name, detail in failures:
            print(f"  - {name}: {detail}")
        return 1

    print(f"0 import errors across {module_count} integration modules.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
