"""The documentation rule, enforced: the code explains itself (Wave 4c, kept for every wave since).

* Every module has a docstring saying what it is for.
* In the business logic (models, services, orders, queries, the graph, logging, idempotency...)
  every public class, method and function has a docstring saying what it does and, where it is not
  obvious, why.
* API glue (views, serializers, admin, URLs, app configs) follows framework conventions: its module
  docstring explains the module, and the business rules it calls are documented where they live.

Private helpers (names starting with ``_``), Django ``Meta`` classes and choice enums (whose labels
are their documentation) are exempt. A new public function without a docstring fails CI.
"""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GLUE = {"views.py", "serializers.py", "admin.py", "urls.py", "apps.py"}


def _modules():
    for folder in ("apps", "erp_system", "ml_service"):
        for path in sorted((ROOT / folder).rglob("*.py")):
            if {"migrations", "__pycache__", "tests"} & set(path.parts):
                continue
            if path.name == "__init__.py" and not path.read_text(encoding="utf-8").strip():
                continue  # an empty package marker has nothing to explain
            yield path


MODULES = list(_modules())


def _is_choices(node):
    return isinstance(node, ast.ClassDef) and any("Choices" in ast.unparse(base) for base in node.bases)


def _undocumented(tree):
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if node.name.startswith("_") or node.name == "Meta" or _is_choices(node):
            continue
        if ast.get_docstring(node) is None:
            yield f"{node.name} (line {node.lineno})"


@pytest.mark.parametrize("path", MODULES, ids=lambda p: str(p.relative_to(ROOT)))
def test_every_module_explains_itself(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    assert ast.get_docstring(tree), f"{path.relative_to(ROOT)} has no module docstring"


@pytest.mark.parametrize("path", [p for p in MODULES if p.name not in GLUE and "commands" not in p.parts],
                         ids=lambda p: str(p.relative_to(ROOT)))
def test_business_logic_is_documented(path):
    missing = list(_undocumented(ast.parse(path.read_text(encoding="utf-8"))))
    assert not missing, f"{path.relative_to(ROOT)}: add docstrings to {', '.join(missing)}"


def test_the_rule_covers_the_whole_codebase():
    assert len(MODULES) > 100  # guards against the scan silently finding nothing
