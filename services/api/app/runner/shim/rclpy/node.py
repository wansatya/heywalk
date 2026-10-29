"""Shim Node: publishers, subscriptions, timers, logger (TEST/DEV ONLY).

See ``app.runner.shim.rclpy`` for why this exists. The node binds to the
active loopback transport installed by the harness before the controller
module is imported.
"""

from __future__ import annotations

from typing import Any, Callable


class _TransportNotInstalledError(RuntimeError):
    pass


def _active_transport() -> Any:
    # Absolute import: the shim is imported as top-level ``rclpy``, so it
    # has no parent package to resolve relatives against. The harness
    # guarantees ``app.*`` is importable before any controller loads.
    from app.runner import loopback

    transport = loopback.active_transport()
    if transport is None:
        raise _TransportNotInstalledError(
            "No loopback transport is installed. Controllers run through the "
            "HeyWalk harness, not standalone."
        )
    return transport


class Logger:
    """Stdout logger; the runner captures these lines as run logs."""

    def __init__(self, name: str) -> None:
        self._name = name

    def _emit(self, level: str, message: Any) -> None:
        print(f"[{self._name}] {level}: {message}", flush=True)

    def info(self, message: Any) -> None:
        self._emit("INFO", message)

    def warn(self, message: Any) -> None:
        self._emit("WARN", message)

    def warning(self, message: Any) -> None:
        self._emit("WARN", message)

    def error(self, message: Any) -> None:
        self._emit("ERROR", message)

    def debug(self, message: Any) -> None:
        self._emit("DEBUG", message)


class _ClockTime:
    def __init__(self, sim_time: float) -> None:
        self._sim_time = sim_time

    @property
    def nanoseconds(self) -> int:
        return int(self._sim_time * 1e9)

    def to_sec(self) -> float:
        return self._sim_time


class _Clock:
    def now(self) -> _ClockTime:
        return _ClockTime(_active_transport().time)


class Publisher:
    def __init__(self, topic: str) -> None:
        self.topic_name = topic

    def publish(self, message: Any) -> None:
        _active_transport().publish(self.topic_name, message)


class Subscription:
    def __init__(self, topic: str, callback: Callable[[Any], None]) -> None:
        self.topic_name = topic
        self.callback = callback


class Timer:
    def __init__(self, period_s: float, callback: Callable[[], None]) -> None:
        self.timer_period_s = float(period_s)
        self.callback = callback
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled


class Node:
    """Drop-in subset of rclpy.node.Node for generated controllers."""

    def __init__(self, name: str, *args: Any, **kwargs: Any) -> None:
        self._name = name
        self._logger = Logger(name)
        self._clock = _Clock()
        self._timers: list[Timer] = []
        self._subscriptions: list[Subscription] = []
        self._parameters: dict[str, Any] = {}

    def get_name(self) -> str:
        return self._name

    def get_logger(self) -> Logger:
        return self._logger

    def get_clock(self) -> _Clock:
        return self._clock

    def create_publisher(
        self, msg_type: Any, topic: str, qos_profile: Any = 10
    ) -> Publisher:
        return Publisher(topic)

    def create_subscription(
        self,
        msg_type: Any,
        topic: str,
        callback: Callable[[Any], None],
        qos_profile: Any = 10,
    ) -> Subscription:
        subscription = Subscription(topic, callback)
        self._subscriptions.append(subscription)
        _active_transport().add_subscription(topic, callback)
        return subscription

    def create_timer(
        self, period_s: float, callback: Callable[[], None]
    ) -> Timer:
        timer = Timer(period_s, callback)
        self._timers.append(timer)
        _active_transport().add_timer(timer)
        return timer

    def declare_parameter(self, name: str, default: Any = None) -> Any:
        self._parameters[name] = default
        return default

    def get_parameter(self, name: str) -> Any:
        class _Value:
            def __init__(self, value: Any) -> None:
                self.value = value

        class _Parameter:
            def __init__(self, value: Any) -> None:
                self._value = value

            def get_parameter_value(self) -> _Value:
                return _Value(self._value)

        return _Parameter(self._parameters.get(name))

    def destroy_node(self) -> None:
        for timer in self._timers:
            timer.cancel()
