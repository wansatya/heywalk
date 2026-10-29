"""Standalone simulator bridge node (ROS 2 Jazzy).

Bridges the deterministic 2D simulator to ROS topics for ``ros2 run`` usage:

- Subscribes ``/cmd_vel`` (Twist) and ``/emergency_stop`` (Bool).
- Publishes ``/odom`` (Odometry), ``/scan`` (LaserScan), ``/goal_pose``
  (PoseStamped, latched once at startup and then periodically), and
  ``/objective_state`` (String, the current locomotion phase type).
- Steps the simulator at the world's fixed timestep and appends telemetry
  to ``<out>/telemetry.jsonl`` via the shared closed-loop driver.

The isolated-run harness (``app.runner.harness --transport rclpy``) runs
this same closed loop single-process; this node exists so the bridge can
also run as an ordinary ROS 2 node during development.

Requires the HeyWalk sim on PYTHONPATH (provided by the runner image).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import rclpy
from geometry_msgs.msg import PoseStamped, Twist
from nav_msgs.msg import Odometry
from rclpy.node import Node
from sensor_msgs.msg import LaserScan
from std_msgs.msg import Bool, String

from app.runner import topics
from app.runner.loopback import ClosedLoop
from app.simulator.world import WorldConfig


class SimulatorBridge(Node):
    def __init__(
        self, world: WorldConfig, seed: int, out_dir: Path,
        max_samples: int = 200_000,
    ) -> None:
        super().__init__("heywalk_sim_bridge")
        self._loop = ClosedLoop(world, seed, out_dir, max_samples=max_samples)
        self._estop = False
        self.create_subscription(Twist, topics.CMD_VEL_TOPIC, self._on_cmd, 10)
        self.create_subscription(
            Bool, topics.EMERGENCY_STOP_TOPIC, self._on_estop, 10
        )
        self._odom_pub = self.create_publisher(
            Odometry, topics.ODOM_TOPIC, 10
        )
        self._scan_pub = self.create_publisher(
            LaserScan, topics.SCAN_TOPIC, 10
        )
        self._goal_pub = self.create_publisher(
            PoseStamped, topics.GOAL_POSE_TOPIC, 10
        )
        self._objective_pub = self.create_publisher(
            String, topics.OBJECTIVE_STATE_TOPIC, 10
        )
        self._goal_pub.publish(
            topics.fill_goal_message(PoseStamped(), self._loop.goal_payload())
        )
        self._publish_objective()
        self.create_timer(world.dt, self._on_tick)
        self.get_logger().info(
            f"Bridge started: dt={world.dt:g}s seed={seed}."
        )

    def _on_cmd(self, message: Twist) -> None:
        self._loop.on_twist_message(message)

    def _publish_objective(self) -> None:
        """Republish the locomotion phase type (the 2D arena's is go_to)."""
        self._objective_pub.publish(
            topics.string_message(self._loop.objective_payload())
        )

    def _on_estop(self, message: Bool) -> None:
        if message.data and not self._estop:
            self.get_logger().warn("Emergency stop latched; halting motion.")
        self._estop = bool(message.data)

    def _on_tick(self) -> None:
        sample = self._loop.tick(estop_active=self._estop)
        self._odom_pub.publish(
            topics.fill_odometry_message(Odometry(), self._loop.odom_payload())
        )
        self._scan_pub.publish(
            topics.fill_laser_scan_message(LaserScan(), self._loop.scan_payload())
        )
        self._publish_objective()
        if sample["goal_reached"]:
            self.get_logger().info(
                f"Goal reached at t={sample['t']:.2f}s."
            )
        if sample["collision"]:
            self.get_logger().warn(
                f"Collision at t={sample['t']:.2f}s."
            )

    def close(self) -> None:
        self._loop.close()


def main(args: Any = None) -> None:
    parser = argparse.ArgumentParser(description="HeyWalk simulator bridge.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--seed", required=True, type=int)
    parsed, _ = parser.parse_known_args(sys.argv[1:] if args is None else args)
    world_path = Path(parsed.workspace) / "world.json"
    world = WorldConfig.from_dict(
        json.loads(world_path.read_text(encoding="utf-8"))
    )
    rclpy.init(args=args)
    bridge = SimulatorBridge(world, parsed.seed, Path(parsed.out))
    try:
        rclpy.spin(bridge)
    except KeyboardInterrupt:
        pass
    finally:
        bridge.close()
        bridge.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
