"""Shim sensor_msgs (TEST/DEV ONLY). See app.runner.shim.rclpy."""

from __future__ import annotations


class LaserScan:
    def __init__(self) -> None:
        self.angle_min: float = 0.0
        self.angle_max: float = 0.0
        self.angle_increment: float = 0.0
        self.range_min: float = 0.0
        self.range_max: float = 0.0
        self.ranges: list[float] = []


class JointState:
    """Subset of sensor_msgs/msg/JointState used by the joint tests."""

    def __init__(self) -> None:
        self.header = None
        self.name: list[str] = []
        self.position: list[float] = []
        self.velocity: list[float] = []
        self.effort: list[float] = []
