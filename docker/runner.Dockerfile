# HeyWalk isolated-runner image: ROS 2 Jazzy + MuJoCo / 2D simulator.
#
# Each experiment run launches ONE single-use container from this image via
# `docker run --rm` (see services/api/app/runner/docker_runner.py). The
# container executes ONLY the generated controller against the simulator
# bridge; it holds no API keys, no database, and no host mounts beyond the
# staged workspace (read-only) and the telemetry output dir.
#
# Build from the repo root:
#   docker build -f docker/runner.Dockerfile -t heywalk-runner:jazzy .
# Or: docker compose build runner

FROM ros:jazzy-ros-base

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-pip \
    ros-jazzy-geometry-msgs \
    ros-jazzy-nav-msgs \
    ros-jazzy-sensor-msgs \
    ros-jazzy-std-msgs \
  && rm -rf /var/lib/apt/lists/*

# MuJoCo is the primary simulation backend (AGENTS.md section 7): the harness
# loads the generated MJCF, emulates the ros2_control position interface, and
# runs the platform-owned joint tests inside this container. Headless by
# design: MUJOCO_GL=disable keeps rendering (and its GL/X11 deps) out.
RUN pip3 install --no-cache-dir --break-system-packages \
    "mujoco>=3.2" \
    "numpy>=1.26" \
    "onnxruntime>=1.20"

ENV MUJOCO_GL=disable \
    PYTHONPATH=/opt/heywalk:/opt/heywalk/ros2_ws/src/heywalk_controller

# Simulator, harness, and ROS bridge. No secrets are baked in; the API never
# passes credentials into run containers.
COPY services/api/app /opt/heywalk/app
COPY ros2_ws/src/heywalk_controller /opt/heywalk/ros2_ws/src/heywalk_controller

# Writable HOME + ROS log dir: run containers execute as an arbitrary non-root
# uid with a read-only rootfs, so rclpy must resolve ~/ and its log dir to
# the /tmp tmpfs (mounted per run). Placed late to preserve layer caching.
ENV HOME=/tmp \
    ROS_LOG_DIR=/tmp/ros-log

# Non-root default user (the API also pins --user per run as defense in depth).
RUN useradd --create-home --uid 65532 runner \
  && mkdir -p /workspace /out \
  && chown runner:runner /out
USER runner
WORKDIR /workspace

# Keep the ros_entrypoint (sources /opt/ros/jazzy/setup.bash). The API
# supplies the full harness command per run:
#   python3 -m app.runner.harness --workspace /workspace --out /out ...
CMD ["python3", "-m", "app.runner.harness", "--help"]
