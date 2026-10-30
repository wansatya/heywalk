"""Bundled starter robots for MuJoCo runs.

Generated projects are expected to contain a robot description (``robot.xml``).
That leaves two gaps the platform has to close itself:

* a brand-new project has nothing to simulate until the model writes one, and
* a project created before MuJoCo support has a controller but no MJCF, so
  every MuJoCo run fails deep inside the harness with a terse log line.

The catalog below fixes both: each entry is a complete, validated robot
package (MJCF + reference controller) the platform can install into a project
without asking the model for anything. Everything here is platform-owned
starter code, never presented as model output — except the MicroDuck entry,
which installs the official Pollen Robotics model (see
``microduck/ATTRIBUTION.md``).

The arm is a fixed-base, gravity-compensated joint test stand: the trunk is
welded to the world and the links are free to move, so the automated joint
tests grade tracking without ground contact ever being mistaken for a
collision. MicroDuck is the opposite: a free-standing biped with real ground
contact, where foot touches are expected and a fall is the failure signal.
Joint tests weld its trunk to a stand (`fix_base`); locomotion runs walk
free-standing, steered by the generated controller while the vendored
upstream ONNX policy supplies the gait.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .microduck import (
    EXPECTED_GROUND_GEOMS,
    FALL_TRUNK_Z,
    OFFICIAL_CONTROL_HZ,
    OFFICIAL_KEYFRAME,
    OFFICIAL_TIMESTEP,
    SOURCE_URL as MICRODUCK_SOURCE_URL,
    MicroduckModelError,
    compose_scene_xml,
)
from .robot_description import (
    RobotPackageError,
    build_robot_package,
)
from .scenario import ScenarioError, default_tests_for_joints


class UnknownStarterRobotError(KeyError):
    """Raised when a starter robot id is not in the catalog."""


# --------------------------------------------------------------------------
# Reference controller
# --------------------------------------------------------------------------

# The same ROS 2 contract the automated tests exercise: read targets, read
# state, publish commands, obey the emergency stop. Joint-count agnostic.
REFERENCE_CONTROLLER = '''"""HeyWalk starter controller: rate-limited joint position tracking.

Platform-provided starter code, not model output. It implements the ROS 2
interface the automated joint tests use:

  in : /joint_targets     std_msgs/Float64MultiArray  (rad or m, one per joint)
  in : /joint_states      sensor_msgs/JointState      (positions)
  in : /emergency_stop    std_msgs/Bool               (latched stop)
  out: /joint_commands    std_msgs/Float64MultiArray  (position commands)

Commands ramp toward the target at MAX_JOINT_SPEED so the motion stays
inside actuator limits instead of stepping to the target in one tick.
"""

from rclpy.node import Node
from sensor_msgs.msg import JointState
from std_msgs.msg import Bool, Float64MultiArray

MAX_JOINT_SPEED = 2.0  # rad/s (or m/s for slide joints)
CONTROL_PERIOD = 0.02  # 50 Hz


class JointPositionController(Node):
    def __init__(self) -> None:
        super().__init__("heywalk_joint_position_controller")
        self._targets: list[float] = []
        self._positions: list[float] = []
        self._stopped = False
        self._commands = self.create_publisher(
            Float64MultiArray, "/joint_commands", 10
        )
        self.create_subscription(
            Float64MultiArray, "/joint_targets", self._on_targets, 10
        )
        self.create_subscription(JointState, "/joint_states", self._on_state, 10)
        self.create_subscription(Bool, "/emergency_stop", self._on_stop, 10)
        self.create_timer(CONTROL_PERIOD, self._tick)

    def _on_targets(self, message: Float64MultiArray) -> None:
        if self._stopped:
            return
        self._targets = [float(value) for value in message.data]

    def _on_state(self, message: JointState) -> None:
        self._positions = [float(value) for value in message.position]

    def _on_stop(self, message: Bool) -> None:
        self._stopped = bool(message.data)
        if self._stopped:
            # Hold the pose we are in rather than chasing a stale target.
            self._targets = list(self._positions)

    def _tick(self) -> None:
        if self._stopped or not self._targets:
            return
        step = MAX_JOINT_SPEED * CONTROL_PERIOD
        commands: list[float] = []
        for index, target in enumerate(self._targets):
            current = (
                self._positions[index]
                if index < len(self._positions)
                else target
            )
            delta = target - current
            commands.append(current + max(-step, min(step, delta)))
        message = Float64MultiArray()
        message.data = commands
        self._commands.publish(message)


def main() -> None:
    import rclpy

    rclpy.init()
    node = JointPositionController()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
'''


MICRODUCK_REFERENCE_CONTROLLER = '''"""HeyWalk MicroDuck starter: joint tracking + flag steering.

Platform-provided starter code, not model output. One file serves both
MicroDuck run modes:

  joint tests : track /joint_targets on /joint_commands (rate-limited).
  locomotion  : steer the built-in walking policy toward /goal_pose with
                /cmd_vel (proportional yaw, capped crawl speed). The walk
                passes only with no fall and no obstacle contact.

Locomotion steering wakes up only when /odom arrives; joint-test runs
publish no odom, so the same controller runs unmodified in both modes.

Every locomotion run also republishes /objective_state (std_msgs/msg/String)
with the CURRENT phase type: go_to, kick, touch, or wander. This starter
only records and logs it; generated controllers adapt their steering per
phase (e.g. drive through a kick goal instead of stopping at the radius).
"""

import math

from geometry_msgs.msg import PoseStamped, Twist
from nav_msgs.msg import Odometry
from rclpy.node import Node
from sensor_msgs.msg import JointState
from std_msgs.msg import Bool, Float64MultiArray, String

MAX_JOINT_SPEED = 2.0  # rad/s
CONTROL_PERIOD = 0.02  # 50 Hz
WALK_SPEED = 0.2  # m/s forward crawl
TURN_GAIN = 2.0
MAX_TURN = 0.5  # rad/s (upstream training range)
GOAL_STOP_RADIUS = 0.3  # m
# NOTE: always pair yaw with forward motion. The walking policy turns by
# differential stepping: yaw-only commands barely rotate in place, while
# forward+yaw arcs track well. Never command vx=0 to "turn first".


class MicroduckController(Node):
    def __init__(self) -> None:
        super().__init__("heywalk_microduck_controller")
        self._targets: list[float] = []
        self._positions: list[float] = []
        self._stopped = False
        self._odom: tuple[float, float, float] | None = None
        self._goal: tuple[float, float] | None = None
        self._phase: str | None = None
        self._commands = self.create_publisher(
            Float64MultiArray, "/joint_commands", 10
        )
        self._cmd_vel = self.create_publisher(Twist, "/cmd_vel", 10)
        self.create_subscription(
            Float64MultiArray, "/joint_targets", self._on_targets, 10
        )
        self.create_subscription(JointState, "/joint_states", self._on_state, 10)
        self.create_subscription(Bool, "/emergency_stop", self._on_stop, 10)
        self.create_subscription(Odometry, "/odom", self._on_odom, 10)
        self.create_subscription(PoseStamped, "/goal_pose", self._on_goal, 10)
        self.create_subscription(String, "/objective_state", self._on_phase, 10)
        self.create_timer(CONTROL_PERIOD, self._tick)

    def _on_targets(self, message: Float64MultiArray) -> None:
        if self._stopped:
            return
        self._targets = [float(value) for value in message.data]

    def _on_state(self, message: JointState) -> None:
        self._positions = [float(value) for value in message.position]

    def _on_stop(self, message: Bool) -> None:
        self._stopped = bool(message.data)
        if self._stopped:
            self._targets = list(self._positions)
            self._publish_twist(0.0, 0.0)

    def _on_odom(self, message: Odometry) -> None:
        pose = message.pose.pose
        yaw = math.atan2(
            2.0 * pose.orientation.w * pose.orientation.z,
            1.0 - 2.0 * pose.orientation.z * pose.orientation.z,
        )
        self._odom = (float(pose.position.x), float(pose.position.y), yaw)

    def _on_goal(self, message: PoseStamped) -> None:
        self._goal = (
            float(message.pose.position.x), float(message.pose.position.y)
        )

    def _on_phase(self, message: String) -> None:
        # Locomotion phase type, republished every tick. Recorded here so
        # adaptations can branch on it; this starter steers the same way
        # in every phase (which is why it fails a kick run).
        phase = message.data or None
        if phase == self._phase:
            return
        self._phase = phase
        if phase is not None:
            self.get_logger().info(f"objective phase: {phase}")

    def _tick(self) -> None:
        self._track_joints()
        self._steer()

    def _track_joints(self) -> None:
        if self._stopped or not self._targets:
            return
        step = MAX_JOINT_SPEED * CONTROL_PERIOD
        commands: list[float] = []
        for index, target in enumerate(self._targets):
            current = (
                self._positions[index]
                if index < len(self._positions)
                else target
            )
            delta = target - current
            commands.append(current + max(-step, min(step, delta)))
        message = Float64MultiArray()
        message.data = commands
        self._commands.publish(message)

    def _steer(self) -> None:
        if self._stopped or self._odom is None or self._goal is None:
            return
        x, y, yaw = self._odom
        goal_x, goal_y = self._goal
        distance = math.hypot(goal_x - x, goal_y - y)
        if distance < GOAL_STOP_RADIUS:
            self._publish_twist(0.0, 0.0)
            return
        heading = math.atan2(goal_y - y, goal_x - x)
        error = (heading - yaw + math.pi) % (2.0 * math.pi) - math.pi
        turn = max(-MAX_TURN, min(MAX_TURN, TURN_GAIN * error))
        self._publish_twist(WALK_SPEED, turn)

    def _publish_twist(self, forward: float, turn: float) -> None:
        command = Twist()
        command.linear.x = forward
        command.angular.z = turn
        self._cmd_vel.publish(command)


def main() -> None:
    import rclpy

    rclpy.init()
    node = MicroduckController()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
'''


# --------------------------------------------------------------------------
# Official MicroDuck model
# --------------------------------------------------------------------------

#: Scenario defaults applied to runs whose robot is the official MicroDuck
#: composition (see ExecutionManager.start_run): the official timestep and
#: control rate, STAND reset, Euler integration (the official runtime sets no
#: integrator, so MuJoCo's default applies), foot-sole ground contacts, fall
#: detection, and body poses for the 3D renderer. Explicit run settings win.
MICRODUCK_SCENARIO_DEFAULTS: dict[str, Any] = {
    "dt": OFFICIAL_TIMESTEP,
    "control_hz": OFFICIAL_CONTROL_HZ,
    "keyframe": OFFICIAL_KEYFRAME,
    "integrator": "Euler",
    "expected_contact_geoms": list(EXPECTED_GROUND_GEOMS),
    "fall_trunk_z": FALL_TRUNK_Z,
    "include_body_poses": True,
    # Joint tests run on a stand: the trunk welds at its STAND height so
    # tracking is measurable. Free-standing (fall-physics) runs stay
    # available by overriding fix_base=false per run.
    "fix_base": True,
}


def _official_microduck_mjcf() -> str:
    """Compose the official-model scene, with an actionable load error."""
    try:
        return compose_scene_xml()
    except MicroduckModelError as exc:
        raise RobotPackageError(
            f"Bundled MicroDuck model cannot be loaded: {exc}"
        ) from None


# --------------------------------------------------------------------------
# Two-joint arm
# --------------------------------------------------------------------------

ARM_JOINTS: tuple[str, ...] = ("shoulder", "elbow")

ARM_MJCF = """<mujoco model="heywalk_arm">
  <!-- HeyWalk starter robot: minimal two-joint arm. Fixed base, gravity
       compensated links, position actuators. Useful for a first experiment
       and for checking the ROS 2 joint contract end to end. -->
  <compiler angle="radian" autolimits="true"/>
  <option timestep="0.002" integrator="implicitfast"/>
  <default>
    <joint damping="0.3" armature="0.002"/>
    <position kp="40" kv="2"/>
    <geom contype="0" conaffinity="0" density="300"/>
  </default>
  <worldbody>
    <body name="base" pos="0 0 0">
      <geom name="base_geom" type="cylinder" size="0.05 0.02"/>
      <body name="upper_arm" pos="0 0 0.02" gravcomp="1">
        <joint name="shoulder" type="hinge" axis="0 1 0" range="-1.2 1.2"/>
        <geom name="upper_arm_geom" type="capsule" fromto="0 0 0 0.22 0 0"
              size="0.015"/>
        <body name="fore_arm" pos="0.22 0 0" gravcomp="1">
          <joint name="elbow" type="hinge" axis="0 1 0" range="-1.2 1.2"/>
          <geom name="fore_arm_geom" type="capsule" fromto="0 0 0 0.18 0 0"
                size="0.012"/>
        </body>
      </body>
    </body>
  </worldbody>
  <actuator>
    <position name="shoulder_position" joint="shoulder"/>
    <position name="elbow_position" joint="elbow"/>
  </actuator>
</mujoco>
"""


# --------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class StarterRobot:
    """A complete, platform-owned robot package that can be installed."""

    id: str
    name: str
    summary: str
    attribution: str
    license: str
    source_url: str
    mjcf: str
    controller: str = REFERENCE_CONTROLLER
    #: Task text that matches the sample controller (empty = leave it alone).
    sample_task: str = ""

    def files(self) -> dict[str, str]:
        """Model-authored files (companions are synthesized on validation)."""
        return {"controller.py": self.controller, "robot.xml": self.mjcf}


STARTER_ROBOTS: tuple[StarterRobot, ...] = (
    StarterRobot(
        id="microduck",
        name="MicroDuck (official model)",
        summary=(
            "The official Pollen Robotics MicroDuck biped: 14 actuated joints"
            " (two 5-DoF legs plus a 4-DoF head/neck), CAD inertias, IMU"
            " sensors, and real ground contact. Joint tests run on a stand;"
            " locomotion runs walk free-standing to the flag."
        ),
        attribution=(
            "Official MicroDuck MuJoCo model by Pollen Robotics"
            " (Hugging Face)."
        ),
        license="Apache-2.0 (model); CC BY-NC-SA 4.0 (meshes)",
        source_url=MICRODUCK_SOURCE_URL,
        mjcf=_official_microduck_mjcf(),
        controller=MICRODUCK_REFERENCE_CONTROLLER,
        sample_task="Walk to the green flag then stop safely.",
    ),
    StarterRobot(
        id="arm",
        name="Two-joint arm",
        summary=(
            "The smallest useful robot: a shoulder and an elbow with position"
            " actuators. Good for a first run or a contract check."
        ),
        attribution="HeyWalk reference model.",
        license="Apache-2.0",
        source_url="",
        mjcf=ARM_MJCF,
    ),
)


def starter_robots() -> tuple[StarterRobot, ...]:
    return STARTER_ROBOTS


def get_starter_robot(robot_id: str) -> StarterRobot:
    for robot in STARTER_ROBOTS:
        if robot.id == robot_id:
            return robot
    known = ", ".join(sorted(robot.id for robot in STARTER_ROBOTS))
    raise UnknownStarterRobotError(
        f"Unknown starter robot {robot_id!r}; known robots: {known}."
    )


def starter_robot_summary(robot: StarterRobot) -> dict[str, Any]:
    """Describe a starter robot exactly as it would be installed.

    Validates the bundled package the same way generation does, so the UI can
    show joints and planned tests before anything is written.
    """
    package = build_robot_package(robot.files())
    try:
        tests = default_tests_for_joints(
            [(joint.name, joint.lower, joint.upper) for joint in package.info.joints]
        )
    except ScenarioError:
        tests = ()
    return {
        "id": robot.id,
        "name": robot.name,
        "summary": robot.summary,
        "attribution": robot.attribution,
        "license": robot.license,
        "source_url": robot.source_url,
        "model_path": package.model_path,
        "joints": [
            {
                "name": joint.name,
                "type": joint.type,
                "actuator": joint.actuator,
                "lower": joint.lower,
                "upper": joint.upper,
            }
            for joint in package.info.joints
        ],
        "tests": [
            {
                "id": test.id,
                "joint": test.joint,
                "target": test.target,
                "tolerance": test.tolerance,
            }
            for test in tests
        ],
    }


__all__ = [
    "MICRODUCK_REFERENCE_CONTROLLER",
    "MICRODUCK_SCENARIO_DEFAULTS",
    "REFERENCE_CONTROLLER",
    "STARTER_ROBOTS",
    "StarterRobot",
    "UnknownStarterRobotError",
    "get_starter_robot",
    "starter_robot_summary",
    "starter_robots",
]
