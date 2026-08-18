# ADR-0002: Python 3.13 + uv for the toolchain

- **Status:** accepted
- **Date:** 2026-08-19
- **Phase:** 0
- **Affects:** all

## Context

The spec fixes the runtime as Python / LangGraph / FastAPI / PostgreSQL but says nothing about
dependency management, and dependency reproducibility is not cosmetic here: spec §13 requires that a
model or prompt change be evaluable against a fixed benchmark, and §12 requires that decisions be
reproducible. A benchmark run is only comparable across time if the environment that produced it is
pinned.

What the machine already has: Python 3.13.1 (via the `py` launcher), Docker 29.7.2, Docker Compose
v5.3.1, PostgreSQL 17.6 client, Node 24.13.0. No `uv`. A stale Poetry shim points at a Microsoft Store
Python that no longer exists — that path is a trap, not an option.

## Decision

Python **3.13** as the target runtime, **uv** for dependency resolution, locking, and virtualenv
management, with a committed `uv.lock`. Project metadata lives in `pyproject.toml`; **ruff** for lint
and format, **mypy** in strict mode for the deterministic modules, **pytest** for tests.

The lockfile is committed and CI installs from it with `--frozen`. Docker images install from the same
lock, so local and container environments cannot drift.

## Alternatives considered

| Option | Why not |
| --- | --- |
| Poetry | The existing install is broken on this machine, and its resolver is markedly slower on the LangChain/LangGraph dependency tree we are about to pull in |
| pip + `requirements.txt` | No real lockfile; transitive versions drift silently, which breaks benchmark comparability |
| pip-tools | Workable, but two tools where uv is one, and no environment management |
| Conda | Heavier than needed; no benefit for a pure-Python service with a Postgres client |

## Consequences

uv gives fast, reproducible installs and one tool for environments, locking, and running. It is
relatively young, but it reads standard `pyproject.toml` — if it becomes a problem, the escape hatch is
exporting to `requirements.txt` and falling back to pip, so reversal is cheap.

Python 3.13 is current and supported by the LangGraph/FastAPI/Pydantic stack. Should any dependency turn
out to lack 3.13 wheels, we drop to 3.12 by changing one line in `pyproject.toml` — flagged here so the
fallback is not rediscovered under pressure.

uv must be installed on this machine as part of Phase 0.
