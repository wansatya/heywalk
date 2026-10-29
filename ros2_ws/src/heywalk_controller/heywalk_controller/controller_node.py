"""Reference navigation controller (works under real rclpy and the shim).

This template shows the structure every generated controller must follow:

- Exactly one ``rclpy`` Node subclass constructible with no arguments
  (or a ``create_node()`` factory); the harness instantiates it.
- Publishes velocity commands on ``/cmd_vel`` (Twist).
- Subscribes ``/odom`` (Odometry), ``/scan`` (LaserScan),
  ``/emergency_stop`` (Bool), and ``/goal_pose`` (PoseStamped).
- Timer-driven control loop (~10 Hz); never blocks the executor.
- Honors speed limits and stops immediately on emergency stop.

Behavior: proportional go-to-goal with a simple obstacle slowdown/turn.
Simulation only -- not validated on physical hardware.
"""

from __future__ import annotations

import math

import rclpy
from geometry_msgs.msg import PoseStamped, Twist
from nav_msgs.msg import Odometry
from rclpy.node import Node
from sensor_msgs.msg import LaserScan
from std_msgs.msg import Bool

MAX_LINEAR_SPEED = 0.5
MAX_ANGULAR_SPEED = 1.5
CONTROL_PERIOD_S = 0.1
OBSTACLE_DISTANCE_M = 0.6
GOAL_TOLERANCE_M = 0.35
LINEAR_GAIN = 0.8
ANGULAR_GAIN = 1.2


def yaw_from_quaternion(z: float, w: float) -> float:
    return math.atan2(2.0 * w * z, 1.0 - 2.0 * z * z)


def clamp(value: float, limit: float) -> float:
    return min(max(value, -limit), limit)


class ControllerNode(Node):
    def __init__(self) -> None:
        super().__init__("heywalk_controller")
        self._cmd_pub = self.create_publisher(Twist, "/cmd_vel", 10)
        self.create_subscription(Odometry, "/odom", self._on_odom, 10)
        self.create_subscription(LaserScan, "/scan", self._on_scan, 10)
        self.create_subscription(Bool, "/emergency_stop", self._on_estop, 10)
        self.create_subscription(PoseStamped, "/goal_pose", self._on_goal, 10)
        self._pose: tuple[float, float, float] | None = None
        self._ranges: list[float] = []
        self._goal: tuple[float, float] | None = None
        self._estop = False
        self.create_timer(CONTROL_PERIOD_S, self._on_tick)
        self.get_logger().info("Controller started; waiting for odom and goal.")

    # ---- subscriptions ----

    def _on_odom(self, message: Odometry) -> None:
        position = message.pose.pose.position
        orientation = message.pose.pose.orientation
        self._pose = (
            float(position.x),
            float(position.y),
            yaw_from_quaternion(float(orientation.z), float(orientation.w)),
        )

    def _on_scan(self, message: LaserScan) -> None:
        self._ranges = [float(r) for r in message.ranges]

    def _on_estop(self, message: Bool) -> None:
        if message.data and not self._estop:
            self.get_logger().warn("Emergency stop received; halting.")
        self._estop = bool(message.data)

    def _on_goal(self, message: PoseStamped) -> None:
        self._goal = (
            float(message.pose.position.x),
            float(message.pose.position.y),
        )
        self.get_logger().info(f"Goal received: {self._goal}.")

    # ---- control loop ----

    def _publish(self, v: float, omega: float) -> None:
        command = Twist()
        command.linear.x = clamp(v, MAX_LINEAR_SPEED)
        command.angular.z = clamp(omega, MAX_ANGULAR_SPEED)
        self._cmd_pub.publish(command)

    def _on_tick(self) -> None:
        if self._estop:
            self._publish(0.0, 0.0)
            return
        if self._pose is None or self._goal is None:
            self._publish(0.0, 0.0)
            return
        x, y, theta = self._pose
        goal_x, goal_y = self._goal
        dx, dy = goal_x - x, goal_y - y
        distance = math.hypot(dx, dy)
        if distance < GOAL_TOLERANCE_M:
            self._publish(0.0, 0.0)
            return
        heading_error = math.atan2(dy, dx) - theta
        heading_error = math.atan2(
            math.sin(heading_error), math.cos(heading_error)
        )
        front = min(self._ranges) if self._ranges else float("inf")
        if front < OBSTACLE_DISTANCE_M:
            # Obstacle ahead: stop forward motion and turn away.
            self._publish(0.0, 0.6 if heading_error >= 0 else -0.6)
            return
        slow = min(1.0, distance)
        self._publish(
            LINEAR_GAIN * distance * slow, ANGULAR_GAIN * heading_error
        )


def main(args=None) -> None:
    rclpy.init(args=args)
    node = ControllerNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
