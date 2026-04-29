"""Embedded Go AST scanner used by :mod:`codeprobe.mining.ast_resolver`.

This subpackage exists only to ship ``scanner.go`` as package data so
``AstResolver`` can locate it via ``Path(__file__).parent`` after a
regular ``pip install`` of codeprobe.
"""
