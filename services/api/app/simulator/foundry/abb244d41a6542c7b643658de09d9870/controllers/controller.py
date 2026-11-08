"""HeyWalk starter controller: rate-limited joint position tracking.

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
