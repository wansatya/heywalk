# heywalk_controller (ROS 2 Jazzy)

Reference ROS 2 package for HeyWalk: a template navigation controller and
the simulator bridge node. Simulation only — not validated on hardware.

## Nodes

- `controller_node` — template controller. Publishes `/cmd_vel`, subscribes
  `/odom`, `/scan`, `/emergency_stop`, `/goal_pose`. The harness also
  publishes `/objective_state` (`std_msgs/String`, the current locomotion
  phase type: `go_to` / `kick` / `touch` / `wander`). Generated controllers
  follow this same structure so the harness can instantiate them.
- `simulator_bridge` — deterministic 2D simulator behind ROS topics.
  Consumes `/cmd_vel`, publishes `/odom` + `/scan` + `/goal_pose` +
  `/objective_state` (`go_to`), halts on `/emergency_stop`, and records
  `telemetry.jsonl`.

## Build (inside a Jazzy environment)

```bash
cd ros2_ws
colcon build --packages-select heywalk_controller
. install/setup.bash
```

## Run with the simulator

```bash
ros2 run heywalk_controller simulator_bridge \
  --workspace /path/to/staged/workspace --out /tmp/mfout --seed 0
ros2 run heywalk_controller controller_node
```

The isolated runner (`services/api/app/runner`) stages the workspace and
drives both nodes through `app.runner.harness --transport rclpy` inside a
dedicated container; see `docker/runner.Dockerfile`.
