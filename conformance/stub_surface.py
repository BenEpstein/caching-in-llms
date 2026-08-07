"""Shared helpers: read the offline stubs' declared surface out of tests/conftest.py.

By AST parse, never by import - importing tests/conftest.py installs the stub
modules into sys.modules (its whole job), which is exactly what a conformance
run must avoid.
"""

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

_CONFTEST_TREE = ast.parse((REPO / "tests" / "conftest.py").read_text())


def stub_fields(class_name: str) -> list:
    """Field names of a stub dataclass in tests/conftest.py, in declared order."""
    for node in ast.walk(_CONFTEST_TREE):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return [
                stmt.target.id for stmt in node.body if isinstance(stmt, ast.AnnAssign)
            ]
    raise AssertionError(f"tests/conftest.py declares no class {class_name}")


def stubbed_message_names() -> list:
    """The message classes _install_lmcache_stubs registers as real dataclasses.

    Read from its `for cls in (...)` loop so the conformance list can never
    drift from what the offline suite actually stubs.
    """
    for node in ast.walk(_CONFTEST_TREE):
        if isinstance(node, ast.FunctionDef) and node.name == "_install_lmcache_stubs":
            for stmt in ast.walk(node):
                if isinstance(stmt, ast.For) and isinstance(stmt.iter, ast.Tuple):
                    return [elt.id for elt in stmt.iter.elts]
    raise AssertionError("tests/conftest.py has no _install_lmcache_stubs class loop")


def unused_message_names() -> list:
    """The names conftest fabricates as empty placeholder classes."""
    for node in ast.walk(_CONFTEST_TREE):
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == "_UNUSED_MESSAGES" for t in node.targets
        ):
            return [c.value for c in node.value.elts]
    raise AssertionError("tests/conftest.py declares no _UNUSED_MESSAGES")
