"""Shim geometry_msgs (TEST/DEV ONLY). See app.runner.shim.rclpy."""

from __future__ import annotations


class _Vector3:
    def __init__(self, x: float = 0.0, y: float = 0.0, z: float = 0.0) -> None:
        self.x = x
        self.y = y
        self.z = z


class Twist:
    def __init__(self) -> None:
        self.linear = _Vector3()
        self.angular = _Vector3()


class _Point:
    def __init__(self, x: float = 0.0, y: float = 0.0, z: float = 0.0) -> None:
        self.x = x
        self.y = y
        self.z = z


class _Quaternion:
    def __init__(
        self, x: float = 0.0, y: float = 0.0, z: float = 0.0, w: float = 1.0
    ) -> None:
        self.x = x
        self.y = y
        self.z = z
        self.w = w


class _Pose:
    def __init__(self) -> None:
        self.position = _Point()
        self.orientation = _Quaternion()


class PoseStamped:
    def __init__(self) -> None:
        self.pose = _Pose()
