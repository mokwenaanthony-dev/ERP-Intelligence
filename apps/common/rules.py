"""The business-rules catalogue (Wave 9b): every rule the ERP enforces, read from the code itself.

An ERP's value is less its code than the rules it has learned: *a blocked customer cannot order*,
*a posted document is never changed*, *a goods receipt cannot exceed the order*. Those rules live in
the service layer as ``raise SalesError("Customer ... is blocked.")``. This module finds every such
``raise`` of a business-rule error (``PostingError`` and its subclasses, and ``ImmutableDocumentError``)
with Python's ``ast`` module, without importing or running anything, and writes them as a catalogue:
module, function and the message the user sees.

``docs/RULES.md`` is generated from it (``python manage.py business_rules``) and a test fails when the
file and the code disagree, so the catalogue cannot go stale: a new rule in the code is a new line in
the document, in the same pull request. Placeholders in messages are shown as ``{customer.number}``.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ROOTS = {"PostingError", "ImmutableDocumentError"}  # the base classes of every business-rule error
SKIP = {"migrations", "__pycache__", "tests", "management"}

HEADER = """# Business rules catalogue

_Generated from the code by `python manage.py business_rules`; do not edit by hand. A test
(`tests/test_rules_catalogue.py`) fails when this file and the code disagree._

Every rule below is enforced in the service layer: breaking it stops the operation, nothing is
posted, and the API answers 400 with the message shown (placeholders in braces are filled in).
These are the "learned rules" of the system, the part of an ERP that is worth more than its code
(see `docs/VISION.md`).
"""


@dataclass(frozen=True)
class Rule:
    """One enforced rule: where it lives, the error it raises and its message."""

    module: str  # e.g. apps/sales/services.py
    function: str  # the function or method that enforces it
    error: str  # e.g. SalesError
    message: str  # the text the user sees, placeholders in braces

    @property
    def area(self) -> str:
        """The app the rule belongs to (``sales``, ``finance`` ...)."""
        return self.module.split("/")[1]


def _files():
    for path in sorted((ROOT / "apps").rglob("*.py")):
        if not SKIP & set(path.relative_to(ROOT).parts):
            yield path


def rule_errors(trees: dict[Path, ast.Module]) -> set[str]:
    """Names of every exception class that is (transitively) a subclass of a business-rule root."""
    bases: dict[str, set[str]] = {}
    for tree in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                bases.setdefault(node.name, set()).update(ast.unparse(b).split(".")[-1] for b in node.bases)
    errors = set(ROOTS)
    changed = True
    while changed:
        changed = False
        for name, parents in bases.items():
            if name not in errors and parents & errors:
                errors.add(name)
                changed = True
    return errors


def message_of(node: ast.expr | None) -> str:
    """The message of a raise as text: f-string placeholders become ``{expression}``."""
    if node is None:
        return ""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant):
                parts.append(str(value.value))
            elif isinstance(value, ast.FormattedValue):
                parts.append("{" + ast.unparse(value.value) + "}")
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return message_of(node.left) + message_of(node.right)
    return "{" + ast.unparse(node) + "}"


def _passed_on(text: str) -> bool:
    """True for ``raise XError(str(exc))``: another rule's message passed on, already in the catalogue."""
    return text.startswith("{") and text.endswith("}") and text.count("{") == 1


def collect() -> list[Rule]:
    """Every business rule in ``apps/``, sorted by module, then function, then message."""
    trees = {path: ast.parse(path.read_text(encoding="utf-8")) for path in _files()}
    errors = rule_errors(trees)
    rules = set()
    for path, tree in trees.items():
        module = path.relative_to(ROOT).as_posix()

        def visit(node, function):
            """Walk the tree, remembering the nearest enclosing function."""
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    visit(child, child.name)
                    continue
                if (isinstance(child, ast.Raise) and isinstance(child.exc, ast.Call)
                        and ast.unparse(child.exc.func).split(".")[-1] in errors):
                    call = child.exc
                    text = " ".join(message_of(call.args[0] if call.args else None).split())
                    if not _passed_on(text):
                        rules.add(Rule(module, function or "(module)", ast.unparse(call.func).split(".")[-1], text))
                visit(child, function)

        visit(tree, None)
    return sorted(rules, key=lambda r: (r.module, r.function, r.message))


def _cell(text: str) -> str:
    return text.replace("|", "\\|") or "-"


def render(rules: list[Rule]) -> str:
    """The catalogue as Markdown: a summary per area, then one table per area."""
    areas: dict[str, list[Rule]] = {}
    for rule in rules:
        areas.setdefault(rule.area, []).append(rule)
    lines = [HEADER, f"**{len(rules)} rules** in {len(areas)} areas.", "", "| Area | Rules |", "|---|---:|"]
    lines += [f"| {area} | {len(found)} |" for area, found in areas.items()]
    for area, found in areas.items():
        lines += ["", f"## {area}", "", "| Rule (message shown) | Where | Error |", "|---|---|---|"]
        lines += [f"| {_cell(r.message)} | `{r.module.split('/', 2)[2]}` {r.function} | {r.error} |" for r in found]
    return "\n".join(lines) + "\n"


def catalogue_path() -> Path:
    """Where the generated catalogue lives."""
    return ROOT / "docs" / "RULES.md"
