"""Generic robot interface (the ``RobotAdapter`` role).

A robot backend (today: MuJoCo; later: other simulators or hardware
adapters) exposes model metadata, accepts bounded position commands,
advances or mirrors state, and reports observations. This module defines
that contract as a runtime-checkable protocol so new backends can be added
without changing callers, and so tests can prove conformance.

``MujocoEngine`` (see ``mujoco_engine.py``) is the first implementation; it
conforms structurally and the MicroDuck suite asserts that conformance.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class RobotAdapter(Protocol):
    """Generic position-command robot surface."""

    @property
    def joint_names(self) -> tuple[str, ...]:
        """Actuated joint names in command/observation order."""
        ...

    @property
    def joint_count(self) -> int:
        """Number of actuated joints (== len(joint_names))."""
        ...

    def set_commands(self, commands: list[float] | tuple[float, ...]) -> bool:
        """Apply position commands; False when the length is wrong.

        Out-of-range values are clamped to the joint limits; non-finite
        values are treated as zero. A False return leaves state unchanged.
        """
        ...

    def joint_positions(self) -> list[float]:
        """Current joint positions in command order."""
        ...

    def joint_velocities(self) -> list[float]:
        """Current joint velocities in command order."""
        ...

    def reset(self) -> None:
        """Restore the deterministic initial state for this backend."""
        ...

    def snapshot(self) -> dict[str, Any]:
        """Full observation: joints, base pose, sensors, contacts."""
        ...

    def close(self) -> None:
        """Release backend resources (idempotent)."""
        ...


def check_adapter(adapter: Any) -> RobotAdapter:
    """Narrow an object to the adapter contract or raise a clear error."""
    if not isinstance(adapter, RobotAdapter):
        raise TypeError(
            f"{type(adapter).__name__!r} does not implement the RobotAdapter"
            " contract (joint_names, joint_count, set_commands,"
            " joint_positions, joint_velocities, reset, snapshot, close)."
        )
    names = adapter.joint_names
    if tuple(names) == () or adapter.joint_count != len(names):
        raise TypeError(
            "RobotAdapter joint_names/joint_count disagree;"
            " commands cannot be addressed reliably."
        )
    return adapter


__all__ = ["RobotAdapter", "check_adapter"]
