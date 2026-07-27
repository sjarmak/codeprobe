"""AST rule for positive composite-verifier rewards from exit status alone."""

from __future__ import annotations

import ast
from dataclasses import dataclass

_STDOUT_CONTRACT_SCORERS: frozenset[str] = frozenset(
    {
        "CheckpointScorer",
        "OracleChecksScorer",
    }
)


@dataclass(frozen=True)
class Finding:
    relpath: str
    line: int
    rule: str
    detail: str

    def format(self) -> str:
        return f"{self.relpath}:{self.line} [{self.rule}] {self.detail}"


def is_scoreresult_call(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id == "ScoreResult"
    if isinstance(func, ast.Attribute):
        return func.attr == "ScoreResult"
    return False


def _is_returncode_zero_comparison(node: ast.expr) -> bool:
    if not isinstance(node, ast.Compare):
        return False
    if len(node.ops) != 1 or not isinstance(node.ops[0], ast.Eq):
        return False
    operands = [node.left, *node.comparators]
    has_returncode = any(
        isinstance(operand, ast.Attribute) and operand.attr == "returncode"
        for operand in operands
    )
    has_zero = any(
        isinstance(operand, ast.Constant)
        and isinstance(operand.value, (int, float))
        and operand.value == 0
        for operand in operands
    )
    return has_returncode and has_zero


def _numeric_value(node: ast.expr) -> int | float | None:
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
    ):
        return node.value
    return None


def _named_numeric_constants(
    statements: list[ast.stmt],
) -> dict[str, int | float]:
    constants: dict[str, int | float] = {}
    for statement in statements:
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue
        value = _numeric_value(statement.value) if statement.value is not None else None
        if value is None:
            continue
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        for target in targets:
            if isinstance(target, ast.Name):
                constants[target.id] = value
    return constants


def _positive_reward(
    node: ast.expr,
    constants: dict[str, int | float],
) -> bool:
    literal = _numeric_value(node)
    if literal is not None:
        return literal > 0
    if isinstance(node, ast.Name):
        return constants.get(node.id, 0) > 0
    if isinstance(node, ast.Attribute):
        return constants.get(node.attr, 0) > 0
    return False


def _return_has_positive_reward(
    node: ast.Return,
    constants: dict[str, int | float],
) -> bool:
    value = node.value
    if value is None:
        return False
    if _positive_reward(value, constants):
        return True
    if isinstance(value, ast.Tuple) and value.elts:
        return _positive_reward(value.elts[0], constants)
    if isinstance(value, ast.Call) and is_scoreresult_call(value):
        for keyword in value.keywords:
            if keyword.arg == "score":
                return _positive_reward(keyword.value, constants)
    return False


def _walk_without_nested_scopes(node: ast.AST) -> list[ast.AST]:
    if isinstance(
        node,
        (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda),
    ):
        return []
    nodes = [node]
    for child in ast.iter_child_nodes(node):
        if isinstance(
            child,
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda),
        ):
            continue
        nodes.extend(_walk_without_nested_scopes(child))
    return nodes


def _parsed_score_name(statement: ast.stmt) -> str | None:
    if not isinstance(statement, ast.Assign):
        return None
    if not isinstance(statement.value, ast.Call):
        return None
    func = statement.value.func
    if (
        not isinstance(func, ast.Name)
        or func.id != "_parse_composite_verifier_stdout"
    ):
        return None
    if len(statement.targets) != 1:
        return None
    target = statement.targets[0]
    if not isinstance(target, (ast.Tuple, ast.List)) or not target.elts:
        return None
    first = target.elts[0]
    return first.id if isinstance(first, ast.Name) else None


def _is_not_none_guard(node: ast.expr, name: str) -> bool:
    if not isinstance(node, ast.Compare):
        return False
    if len(node.ops) != 1 or not isinstance(node.ops[0], ast.IsNot):
        return False
    operands = (node.left, node.comparators[0])
    return any(
        isinstance(operand, ast.Name) and operand.id == name
        for operand in operands
    ) and any(
        isinstance(operand, ast.Constant) and operand.value is None
        for operand in operands
    )


def _return_uses_name(node: ast.Return, name: str) -> bool:
    return any(
        isinstance(child, ast.Name) and child.id == name
        for child in ast.walk(node)
    )


def _has_prior_stdout_contract(
    function: ast.FunctionDef,
    fallback: ast.If,
) -> bool:
    try:
        fallback_index = function.body.index(fallback)
    except ValueError:
        return False
    prior_statements = function.body[:fallback_index]
    for index, statement in enumerate(prior_statements):
        parsed_name = _parsed_score_name(statement)
        if parsed_name is None:
            continue
        for guard in prior_statements[index + 1 :]:
            if not isinstance(guard, ast.If):
                continue
            if not _is_not_none_guard(guard.test, parsed_name):
                continue
            if any(
                isinstance(body_statement, ast.Return)
                and _return_uses_name(body_statement, parsed_name)
                for body_statement in guard.body
            ):
                return True
    return False


def find_positive_reward_exit_fallbacks(
    source: str, relpath: str
) -> list[Finding]:
    """Flag composite-verifier full credit based on process exit alone."""
    findings: list[Finding] = []
    tree = ast.parse(source, filename=relpath)
    module_constants = _named_numeric_constants(tree.body)
    for class_node in tree.body:
        if not isinstance(class_node, ast.ClassDef):
            continue
        if class_node.name not in _STDOUT_CONTRACT_SCORERS:
            continue
        constants = {
            **module_constants,
            **_named_numeric_constants(class_node.body),
        }
        for function in class_node.body:
            if not isinstance(function, ast.FunctionDef):
                continue
            function_constants = {
                **constants,
                **_named_numeric_constants(function.body),
            }
            function_nodes = [
                node
                for statement in function.body
                for node in _walk_without_nested_scopes(statement)
            ]
            for node in function_nodes:
                if not isinstance(node, ast.If):
                    continue
                if not _is_returncode_zero_comparison(node.test):
                    continue
                positive_returns = [
                    child
                    for statement in node.body
                    for child in _walk_without_nested_scopes(statement)
                    if isinstance(child, ast.Return)
                    and _return_has_positive_reward(child, function_constants)
                ]
                if not positive_returns:
                    continue
                if _has_prior_stdout_contract(function, node):
                    continue
                findings.append(
                    Finding(
                        relpath=relpath,
                        line=node.lineno,
                        rule="positive-reward-exit-fallback",
                        detail=(
                            f"{class_node.name} awards positive reward from "
                            "returncode without first parsing the verifier "
                            "stdout contract"
                        ),
                    )
                )
    return findings
