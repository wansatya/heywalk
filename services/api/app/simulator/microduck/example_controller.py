"""MicroDuck example controller: ramp to the official STAND pose and hold it.

Platform-provided example, not model output. It implements the same ROS 2
joint interface the automated tests use:

  in : /joint_states      sensor_msgs/JointState      (positions)
  in : /emergency_stop    std_msgs/Bool               (latched stop)
  out: /joint_commands    std_msgs/Float64MultiArray  (position commands)

The target pose is the official STAND keyframe from
``scene.xml`` (pollen-robotics/microduck_rl, Apache-2.0), in the model's
actuator order. Holding STAND with position servos alone does NOT balance
the robot — without feedback control the duck topples, which is the honest
starting point for controller experiments. No walking is claimed here.
"""

from rclpy.node import Node
from sensor_msgs.msg import JointState
from std_msgs.msg import Bool, Float64MultiArray

# Official STAND keyframe joint values (scene.xml), actuator order:
# left_hip_yaw/roll/pitch, left_knee/ankle, neck_pitch, head_pitch/yaw/roll,
# right_hip_yaw/roll/pitch, right_knee/ankle.
STAND_POSE = (
    0.0,
    -0.08726646259971647,
    -0.457924,
    -0.004940,
    0.452984,
    0.3490658503988659,
    0.3490658503988659,
    0.0,
    0.0,
    0.0,
    0.08726646259971647,
    0.457924,
    0.004940,
    -0.452984,
)

MAX_JOINT_SPEED = 1.0  # rad/s per joint while ramping to STAND
CONTROL_PERIOD = 0.02  # 50 Hz, the official control rate


class StandPoseController(Node):
    def __init__(self) -> None:
        super().__init__("microduck_stand_example")
        self._positions: list[float] = []
        self._stopped = False
        self._commands = self.create_publisher(
            Float64MultiArray, "/joint_commands", 10
        )
        self.create_subscription(JointState, "/joint_states", self._on_state, 10)
        self.create_subscription(Bool, "/emergency_stop", self._on_stop, 10)
        self.create_timer(CONTROL_PERIOD, self._tick)

    def _on_state(self, message: JointState) -> None:
        self._positions = [float(value) for value in message.position]

    def _on_stop(self, message: Bool) -> None:
        self._stopped = bool(message.data)

    def _tick(self) -> None:
        if self._stopped:
            return
        step = MAX_JOINT_SPEED * CONTROL_PERIOD
        commands: list[float] = []
        for index, target in enumerate(STAND_POSE):
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
    node = StandPoseController()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
