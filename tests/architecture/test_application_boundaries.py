"""Application-layer ownership rules that complement Tach's module graph."""

import ast

from .dependency_rules import SOURCE_ROOT

_APPLICATION_ROOT = SOURCE_ROOT / "application"


def test_application_use_cases_do_not_import_application_runtime_or_facade() -> None:
    for path in _APPLICATION_ROOT.rglob("*.py"):
        if path.name == "service.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            assert node.module != "my_code.application.service", path
            if node.module == "my_code.runtime.application":
                assert "ApplicationRuntime" not in {
                    alias.name for alias in node.names
                }, path


def test_only_facade_owns_the_application_operation_lock() -> None:
    lock_users = tuple(
        path
        for path in _APPLICATION_ROOT.rglob("*.py")
        if "operation_lock" in path.read_text(encoding="utf-8")
    )
    assert lock_users == (_APPLICATION_ROOT / "service.py",)


def test_background_wake_signal_belongs_to_background_tasks() -> None:
    assert (SOURCE_ROOT / "features" / "background_tasks" / "wake.py").exists()
    assert not (SOURCE_ROOT / "features" / "subagents" / "wake.py").exists()


def test_application_package_initializers_are_empty() -> None:
    for path in _APPLICATION_ROOT.rglob("__init__.py"):
        assert not path.read_text(encoding="utf-8").strip(), path


def test_lifecycle_migration_has_no_legacy_production_names() -> None:
    forbidden = {
        "ApplicationAssembly",
        "AppState",
        "ContextRuntime",
        "ContextPlanningState",
        "AttachmentDerivationState",
        "ToolContext",
        "ProviderContinuationState",
        "RuntimeStatus",
        "ContextStatus",
        "SessionLifecycle",
        "WorkspaceState",
        "ToolState",
        "PermissionState",
        "TurnStarted",
        "TurnFinished",
        "AgentTurnSucceeded",
        "AgentTurnOutcome",
    }
    for path in SOURCE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        declared = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        }
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        assert not forbidden.intersection(declared | imported), path

    assert not (SOURCE_ROOT / "runtime" / "state.py").exists()
    assert not (SOURCE_ROOT / "context" / "session.py").exists()
    assert not (SOURCE_ROOT / "application" / "sessions" / "lifecycle.py").exists()
