"""Canonical source of the shipped codeprobe agent skills.

The five product skills (``codeprobe-{mine,run,interpret,calibrate,
check-infra}/SKILL.md``) live here as wheel package data so every pip
install can materialize them via ``codeprobe skills install``. The
copies at ``.claude/skills/codeprobe-*/SKILL.md`` are byte-identical
mirrors kept only so this repository's own agents resolve them;
``tests/skills/test_skill_cli_alignment.py::test_repo_mirror_in_sync``
enforces the mirror invariant.
"""
