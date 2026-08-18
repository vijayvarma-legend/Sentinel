"""Machine-checked enforcement of the prime directive.

    No LLM output directly posts money.

Spec DoD-9 states that the LLM *cannot* bypass deterministic validation, policy,
authorization, or execution controls. "Cannot" is a structural claim, so it is checked
structurally: this module walks the real import graph and fails the build on violation.

See ADR-0004 (docs/decisions/0004-trust-classes.md) and docs/SERVICE_REGISTRY.md.

These tests deliberately do not import the application. They parse source, so they hold
even for modules that are half-written, broken, or unimportable.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from collections import deque
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
PKG = SRC / "sentinel"

# The registry, as code. Kept in step with docs/SERVICE_REGISTRY.md by test_registry_matches_code.
TRUST_CLASSES: dict[str, str] = {
    "core": "deterministic",
    "db": "deterministic",
    "storage": "deterministic",
    "graph": "deterministic",
    "auth": "control",
    "ingestion": "deterministic",
    "extraction": "llm",
    "validation": "deterministic",
    "duplicates": "deterministic",
    "risk": "deterministic",
    "reasoning": "llm",
    "policy": "control",
    "hitl": "human",
    "erp": "control",
    "audit": "observability",
    "agentops": "observability",
}

LLM_MODULES = {m for m, t in TRUST_CLASSES.items() if t == "llm"}
CONTROL_MODULES = {m for m, t in TRUST_CLASSES.items() if t == "control"}

# Modules an LLM-class module must not be able to reach, directly or transitively.
# sentinel.erp moves money. sentinel.db is the write path to the financial record.
EXECUTION_MODULES = {"erp", "db"}

# Third-party model SDKs that have no business inside deterministic financial code.
LLM_SDK_ROOTS = {"anthropic", "openai", "langchain", "langchain_core", "langgraph", "litellm"}

DETERMINISTIC_FINANCIAL = {"validation", "duplicates", "risk", "policy", "erp"}


def _iter_source_files(module: str):
    return (PKG / module).rglob("*.py")


def _imports_of(path: Path) -> set[str]:
    """Every module path imported by one source file, as dotted strings."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:  # a broken file must fail loudly, not silently pass
        pytest.fail(f"{path} does not parse: {exc}")

    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import: resolve against the containing package
                base = path.relative_to(SRC).parent.parts
                anchor = base[: len(base) - node.level + 1]
                prefix = ".".join((*anchor, node.module) if node.module else anchor)
            elif node.module:
                prefix = node.module
            else:  # pragma: no cover -- unreachable: level 0 always carries a module
                continue
            found.add(prefix)
            # `from sentinel import erp` names a submodule in the alias, not the module
            # field. Record both readings; a bare attribute import is harmless noise, a
            # missed submodule edge is a hole in the safety net.
            found.update(f"{prefix}.{alias.name}" for alias in node.names)
    return found


def _sentinel_deps(module: str) -> set[str]:
    """Direct first-party dependencies of a sentinel module, as bare module names."""
    deps: set[str] = set()
    for file in _iter_source_files(module):
        for imported in _imports_of(file):
            parts = imported.split(".")
            if parts[0] == "sentinel" and len(parts) > 1 and parts[1] != module:
                deps.add(parts[1])
    return deps


def _reachable(start: str) -> dict[str, list[str]]:
    """Every sentinel module reachable from `start`, mapped to the path that got there."""
    paths = {start: [start]}
    queue = deque([start])
    while queue:
        current = queue.popleft()
        for dep in _sentinel_deps(current):
            if dep not in paths:
                paths[dep] = [*paths[current], dep]
                queue.append(dep)
    return paths


# --------------------------------------------------------------------------------------
# The prime directive
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("llm_module", sorted(LLM_MODULES))
def test_llm_modules_cannot_reach_execution(llm_module: str) -> None:
    """An advisory module must have no path -- at any depth -- to money.

    A single import from the reasoning agent into the ERP client would void the
    system's central safety property and nothing else would notice. This notices.
    """
    reachable = _reachable(llm_module)
    violations = {m: reachable[m] for m in EXECUTION_MODULES if m in reachable}

    assert not violations, (
        f"TRUST BOUNDARY VIOLATION -- sentinel.{llm_module} is trust class 'llm' and must "
        f"never reach an execution or write path (ADR-0004).\n"
        + "\n".join(
            f"  reaches sentinel.{target} via: " + " -> ".join(f"sentinel.{p}" for p in path)
            for target, path in sorted(violations.items())
        )
        + "\n\nAn LLM module receives the data it needs from the orchestrator. "
        "It does not fetch, and it does not execute."
    )


@pytest.mark.parametrize("control_module", sorted(CONTROL_MODULES))
def test_control_modules_do_not_import_llm_modules(control_module: str) -> None:
    """Gates must not call models.

    Evidence produced by an LLM reaches the policy engine as inert data on the pipeline
    state -- never as a live call from inside the gate that is deciding.
    """
    offenders = _sentinel_deps(control_module) & LLM_MODULES
    assert not offenders, (
        f"TRUST BOUNDARY VIOLATION -- sentinel.{control_module} is trust class 'control' "
        f"and imports LLM module(s): {sorted(offenders)}.\n"
        "Pass the model's output in as data on the state object instead (ADR-0004)."
    )


@pytest.mark.parametrize("module", sorted(DETERMINISTIC_FINANCIAL))
def test_financial_modules_import_no_model_sdk(module: str) -> None:
    """Financial mathematics and policy enforcement stay entirely outside the LLM."""
    offenders: set[str] = set()
    for file in _iter_source_files(module):
        for imported in _imports_of(file):
            if imported.split(".")[0] in LLM_SDK_ROOTS:
                offenders.add(f"{file.relative_to(SRC)}: {imported}")

    assert not offenders, (
        f"sentinel.{module} performs deterministic financial work and must not import a "
        f"model SDK:\n" + "\n".join(f"  {o}" for o in sorted(offenders))
    )


def test_core_domain_has_no_internal_dependencies() -> None:
    """The domain layer is the shared floor -- it must not depend on anything above it."""
    deps = _sentinel_deps("core")
    assert not deps, (
        f"sentinel.core must not depend on other sentinel modules, but imports: {sorted(deps)}. "
        "The domain layer is depended upon; it does not depend (ADR-0003)."
    )


# --------------------------------------------------------------------------------------
# The registry is the source of truth, so it has to stay true
# --------------------------------------------------------------------------------------


def test_every_module_declares_its_trust_class() -> None:
    """Each service package states its own trust class, and states it correctly."""
    mismatches: list[str] = []
    for module, expected in TRUST_CLASSES.items():
        init = PKG / module / "__init__.py"
        if not init.exists():
            mismatches.append(f"sentinel.{module}: missing __init__.py")
            continue
        source = init.read_text(encoding="utf-8")
        if f'TRUST_CLASS = "{expected}"' not in source:
            mismatches.append(f"sentinel.{module}: does not declare TRUST_CLASS = {expected!r}")
    assert not mismatches, "\n".join(mismatches)


def test_registry_matches_code() -> None:
    """No service exists in code without a row in the registry, or vice versa."""
    on_disk = {p.name for p in PKG.iterdir() if p.is_dir() and not p.name.startswith("_")}
    declared = set(TRUST_CLASSES)

    assert on_disk == declared, (
        f"Service registry drift.\n"
        f"  in code but not registered: {sorted(on_disk - declared)}\n"
        f"  registered but not in code: {sorted(declared - on_disk)}\n"
        "Update TRUST_CLASSES here and docs/SERVICE_REGISTRY.md together."
    )


def test_import_linter_contracts_hold() -> None:
    """The same boundaries, checked independently by import-linter over the real graph.

    Belt and braces: the AST walk above catches violations in unimportable code, while
    import-linter catches dynamic edges the AST walk cannot see.
    """
    result = subprocess.run(
        [sys.executable, "-m", "importlinter.cli", "lint-imports"],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[2],
    )
    assert result.returncode == 0, (
        f"import-linter contracts broken:\n{result.stdout}\n{result.stderr}"
    )
