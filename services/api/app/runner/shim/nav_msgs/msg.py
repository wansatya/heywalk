"""Shim nav_msgs (TEST/DEV ONLY). See app.runner.shim.rclpy."""

from __future__ import annotations

from geometry_msgs.msg import Twist


class _Position:
    def __init__(self, x: float = 0.0, y: float = 0.0, z: float = 0.0) -> None:
        self.x = x
        self.y = y
        self.z = z


class _Orientation:
    def __init__(
        self, x: float = 0.0, y: float = 0.0, z: float = 0.0, w: float = 1.0
    ) -> None:
        self.x = x
        self.y = y
        self.z = z
        self.w = w


class _Pose:
    def __init__(self) -> None:
        self.position = _Position()
        self.orientation = _Orientation()


class _PoseWithCovariance:
    def __init__(self) -> None:
        self.pose = _Pose()


class _TwistWithCovariance:
    def __init__(self) -> None:
        self.twist = Twist()


class Odometry:
    def __init__(self) -> None:
        self.pose = _PoseWithCovariance()
        self.twist = _TwistWithCovariance()
