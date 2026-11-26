"""
Composable pipeline engine for LogLearner v2.

Steps declare requires/provides. Pipeline validates the chain and runs them
sequentially, threading an immutable PipelineContext through.

Intentionally minimal: no async, no branching, no parallelism yet. Add those
when a concrete step needs them — don't speculate.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class PipelineContext:
    """Immutable context threaded through pipeline steps."""

    data: dict = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def has(self, key: str) -> bool:
        return key in self.data

    def set(self, key: str, value: Any) -> "PipelineContext":
        return replace(self, data={**self.data, key: value})

    def update(self, **kwargs) -> "PipelineContext":
        return replace(self, data={**self.data, **kwargs})


@runtime_checkable
class StepProtocol(Protocol):
    name: str
    requires: tuple[str, ...]
    provides: tuple[str, ...]

    def __call__(self, ctx: PipelineContext) -> PipelineContext: ...


class PipelineError(RuntimeError):
    pass


class Pipeline:
    """Linear pipeline of Steps. Call .then(step) to append; .run(ctx) to execute."""

    def __init__(self) -> None:
        self.steps: list[StepProtocol] = []

    def then(self, step: StepProtocol) -> "Pipeline":
        if not hasattr(step, "name") or not hasattr(step, "requires") or not hasattr(step, "provides"):
            raise PipelineError(
                f"Step {step!r} missing required attributes (name/requires/provides)"
            )
        self.steps.append(step)
        return self

    def describe(self) -> list[dict]:
        return [
            {"name": s.name, "requires": list(s.requires), "provides": list(s.provides)}
            for s in self.steps
        ]

    def run(self, ctx: PipelineContext | None = None, verbose: bool = False) -> PipelineContext:
        ctx = ctx or PipelineContext()
        provided: set[str] = set(ctx.data.keys())
        for step in self.steps:
            missing = [k for k in step.requires if k not in provided]
            if missing:
                raise PipelineError(
                    f"Step '{step.name}' requires {missing} not in context "
                    f"(available: {sorted(provided)})"
                )
            if verbose:
                print(f"  → {step.name} (requires={list(step.requires)}, provides={list(step.provides)})")
            ctx = step(ctx)
            if not isinstance(ctx, PipelineContext):
                raise PipelineError(f"Step '{step.name}' returned {type(ctx).__name__}, expected PipelineContext")
            provided.update(step.provides)
        return ctx
