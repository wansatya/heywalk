"""Shim std_msgs (TEST/DEV ONLY). See app.runner.shim.rclpy."""

from __future__ import annotations


class Bool:
    """Subset of std_msgs/msg/Bool.

    Like every rclpy message class this takes no constructor arguments:
    ``Bool(True)`` is a TypeError on the real ROS 2 stack, so the shim must
    reject it too or the loopback path hides a bug that only appears inside
    the ROS 2 runner. Assign the field after construction.
    """

    def __init__(self) -> None:
        self.data: bool = False


class Float64MultiArray:
    """Subset of std_msgs/msg/Float64MultiArray used by the joint tests."""

    def __init__(self) -> None:
        self.data: list[float] = []
        self.layout = None


class String:
    """Subset of std_msgs/msg/String (objective-state channel)."""

    def __init__(self) -> None:
        self.data: str = ""
