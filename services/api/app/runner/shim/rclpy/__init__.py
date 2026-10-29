"""Minimal rclpy API subset for the loopback transport (TEST/DEV ONLY).

This shim implements just enough of the rclpy API for HeyWalk's generated
controllers to run unmodified against the in-process loopback transport:
``Node`` with publishers, subscriptions, timers, and a logger, plus
``init``/``ok``/``shutdown`` lifecycle helpers.

It is used ONLY by the local subprocess runner and unit tests. Production
runs inside the Docker runner use the real ROS 2 Jazzy rclpy. The shim is
never installed into the runner image and must never be presented as ROS 2.
"""

from __future__ import annotations

from typing import Any, Callable

from .node import Logger, Node

__all__ = ["Logger", "Node", "init", "ok", "shutdown", "spin"]

_initialized = False


def init(*args: Any, **kwargs: Any) -> None:
    global _initialized
    _initialized = True


def ok() -> bool:
    return _initialized


def shutdown(*args: Any, **kwargs: Any) -> None:
    global _initialized
    _initialized = False


def spin(node: Node, *, _pump: Callable[[], bool] | None = None) -> None:
    """Pump callbacks until the pump reports done (loopback helper)."""
    if _pump is None:
        raise RuntimeError(
            "rclpy.spin is only supported with the loopback pump; "
            "the harness drives nodes tick-by-tick instead."
        )
    while _initialized and _pump():
        pass
