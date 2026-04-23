"""Path bootstrap so these tests exercise the local src/ tree.

In editable / CI installs the project-level conftest + installed package cover
this, but when codeprobe is installed as a released wheel into site-packages
we need to make sure ``from codeprobe.mcp import ...`` resolves to the source
in this working tree rather than the stale installed package.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
_SRC_STR = str(_SRC)

if _SRC_STR not in sys.path:
    sys.path.insert(0, _SRC_STR)

# If codeprobe was already imported from site-packages by a parent conftest,
# drop the cached modules so that src/ is re-resolved. This is safe — no test
# relies on identity of codeprobe module objects across imports.
_local_init = _SRC / "codeprobe" / "__init__.py"
_cached = sys.modules.get("codeprobe")
if _cached is not None and getattr(_cached, "__file__", None) != str(_local_init):
    for name in [m for m in list(sys.modules) if m == "codeprobe" or m.startswith("codeprobe.")]:
        del sys.modules[name]
    importlib.import_module("codeprobe")
