"""Objective, model-independent run evaluation (AGENTS.md section 8).

Thin orchestration-layer facade over the simulator-owned evaluator. The
evaluation logic lives in :mod:`app.simulator.evaluator` so the engine and
its scoring stay testable together; this module preserves the service-level
import path used by the execution manager.
"""

from __future__ import annotations

from ..simulator.evaluator import (
    EVALUATOR_VERSION,
    evaluate,
    evaluate_from_telemetry,
    failure_report,
)

__all__ = [
    "EVALUATOR_VERSION",
    "evaluate",
    "evaluate_from_telemetry",
    "failure_report",
]
