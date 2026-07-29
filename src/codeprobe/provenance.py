"""Runtime install-provenance guard (codeprobe-v3wn).

Detects when the running ``codeprobe`` process is silently bound to a
different source tree than the one that owns its virtualenv. This happens
when an editable install (``pip install -e .`` / ``uv pip install -e .``) is
run *from inside a git worktree* but targets a shared ``.venv`` that lives at
a different checkout: whichever worktree installs last wins, rewriting both
the ``__editable__.*.pth`` (module search path) and the ``bin/codeprobe``
console-script shebang to point at that worktree. The project-root CLI then
imports stale worktree code with no visible signal.

The guard compares three physical facts that a corrupt install cannot make
mutually consistent:

* **cross_venv_script** — the launched console script lives in venv *A*
  (``<A>/bin/codeprobe``) but its shebang selected an interpreter from venv
  *B*. Detectable because ``sys.argv[0]`` names the script (venv *A*) while
  ``sys.prefix`` names the running interpreter's venv (*B*).
* **foreign_module** — the running venv sits next to a ``src/codeprobe``
  checkout (the editable-dev layout), yet ``codeprobe`` imported from a
  *different* tree because the venv's ``.pth`` was rewritten to a foreign
  worktree.

The check is a pure function over injected paths so it is fully testable, and
it is *total* (never raises) so it is safe to call on the CLI's hot startup
path. Repair is a documented, worktree-safe reinstall command; nothing here
deletes or mutates any worktree.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

#: Set to a non-empty value to suppress the startup provenance warning.
SKIP_ENV = "CODEPROBE_SKIP_PROVENANCE_CHECK"


@dataclass(frozen=True)
class ProvenanceReport:
    """Outcome of :func:`analyze`.

    ``kind`` is one of ``ok``, ``not_applicable``, ``cross_venv_script`` or
    ``foreign_module``. ``ok`` is ``True`` for the first two.
    """

    ok: bool
    kind: str
    detail: str
    fix: str


def _real(path: object) -> Path | None:
    """Resolve ``path`` to a real absolute path, or ``None`` on failure."""
    if not isinstance(path, (str, os.PathLike)):
        return None
    try:
        return Path(path).resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def _is_venv(directory: Path) -> bool:
    """True when ``directory`` is a virtualenv root (has ``pyvenv.cfg``)."""
    try:
        return (directory / "pyvenv.cfg").is_file()
    except OSError:
        return False


def _bin_name() -> str:
    return "Scripts" if os.name == "nt" else "bin"


def _script_venv(argv0: object) -> Path | None:
    """Return the venv that physically contains the launched console script.

    Console scripts live at ``<venv>/bin/<name>`` (POSIX) or
    ``<venv>/Scripts/<name>`` (Windows). Returns the venv root only when
    ``argv0`` has that shape and the parent is a real venv, otherwise
    ``None`` (e.g. ``python -m codeprobe``, a global install, or a test
    runner launcher).
    """
    real = _real(argv0)
    if real is None or not real.is_file():
        return None
    bindir = real.parent
    if bindir.name != _bin_name():
        return None
    venv = bindir.parent
    return venv if _is_venv(venv) else None


def repair_command(venv: Path, root: Path) -> str:
    """A worktree-safe command that repoints ``venv`` at ``root``.

    ``pip install -e --force-reinstall --no-deps`` rewrites the editable
    ``.pth``, the ``direct_url.json`` provenance, and the console-script
    shebangs to reference ``venv``'s own interpreter and ``root``'s source,
    without touching dependencies or any git worktree.
    """
    python = venv / _bin_name() / ("python.exe" if os.name == "nt" else "python")
    return f'"{python}" -m pip install -e "{root}" --force-reinstall --no-deps'


def analyze(*, package_file: object, prefix: object, argv0: object) -> ProvenanceReport:
    """Compare the running install's physical facts for a foreign binding.

    Args:
        package_file: ``codeprobe.__file__`` — where the package imported from.
        prefix: ``sys.prefix`` — the running interpreter's venv.
        argv0: ``sys.argv[0]`` — the launched console script (or module).

    Returns a :class:`ProvenanceReport`; ``ok`` is ``True`` when no foreign
    binding is detectable or the layout is not an editable-dev checkout.
    """
    running_venv = _real(prefix)
    pkg_init = _real(package_file)
    checked = False

    # Signal A: the console script and the running interpreter disagree on venv.
    script_venv = _script_venv(argv0)
    if script_venv is not None and running_venv is not None:
        checked = True
        if script_venv != running_venv:
            root = script_venv.parent
            return ProvenanceReport(
                ok=False,
                kind="cross_venv_script",
                detail=(
                    f"console script {script_venv / _bin_name() / 'codeprobe'} "
                    f"lives in venv {script_venv} but is executing the "
                    f"interpreter from {running_venv}; its shebang points at a "
                    f"foreign environment"
                ),
                fix=("Rewrite the console script for its own venv: " + repair_command(script_venv, root)),
            )

    # Signal B: the venv's adjacent src/ checkout is not what got imported.
    if running_venv is not None and pkg_init is not None:
        venv_root = running_venv.parent
        expected = _real(venv_root / "src" / "codeprobe" / "__init__.py")
        if expected is not None and expected.is_file():
            checked = True
            if expected != pkg_init:
                return ProvenanceReport(
                    ok=False,
                    kind="foreign_module",
                    detail=(
                        f"venv {running_venv} is adjacent to {venv_root / 'src'} "
                        f"but 'codeprobe' imported from {pkg_init.parent}; the "
                        f"editable install resolves to a foreign tree"
                    ),
                    fix=(
                        "Reinstall the editable package from the venv's own "
                        "checkout: " + repair_command(running_venv, venv_root)
                    ),
                )

    if checked:
        where = pkg_init.parent if pkg_init is not None else "<unknown>"
        return ProvenanceReport(
            ok=True,
            kind="ok",
            detail=f"codeprobe resolves from {where}, consistent with its venv",
            fix="",
        )
    return ProvenanceReport(
        ok=True,
        kind="not_applicable",
        detail="not an editable-checkout venv; provenance check skipped",
        fix="",
    )
