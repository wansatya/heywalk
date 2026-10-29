"""Deterministic differential-drive 2D simulation engine (AGENTS.md section 7).

The engine integrates differential-drive motion at a fixed timestep, detects
collisions against walls and obstacles, generates laser-scan ranges, reports
odometry, halts on the emergency-stop signal, and records a timestamped trace.

The model is a lightweight deterministic approximation, not full physics:
commanded velocities apply instantaneously within the configured limits.
"""

from __future__ import annotations

import math
import random
from typing import Any

from .world import Rect, WorldConfig

MIN_RANGE = 0.02


def normalize_angle(theta: float) -> float:
    wrapped = (theta + math.pi) % (2.0 * math.pi) - math.pi
    if wrapped == -math.pi and theta > 0:
        return math.pi
    return wrapped


def integrate_diff_drive(
    x: float, y: float, theta: float, v: float, omega: float, dt: float
) -> tuple[float, float, float]:
    """Exact differential-drive integration over one timestep."""
    if abs(omega) < 1e-9:
        return (
            x + v * math.cos(theta) * dt,
            y + v * math.sin(theta) * dt,
            theta,
        )
    radius = v / omega
    dtheta = omega * dt
    return (
        x + radius * (math.sin(theta + dtheta) - math.sin(theta)),
        y - radius * (math.cos(theta + dtheta) - math.cos(theta)),
        theta + dtheta,
    )


def circle_hits_rect(x: float, y: float, radius: float, rect: Rect) -> bool:
    closest_x = min(max(x, rect.x), rect.x_max)
    closest_y = min(max(y, rect.y), rect.y_max)
    return math.hypot(x - closest_x, y - closest_y) <= radius


def pose_in_collision(x: float, y: float, world: WorldConfig) -> bool:
    radius = world.robot_radius
    if x - radius < 0 or x + radius > world.width:
        return True
    if y - radius < 0 or y + radius > world.height:
        return True
    return any(circle_hits_rect(x, y, radius, obstacle) for obstacle in world.obstacles)


def _ray_segment_intersection(
    ox: float,
    oy: float,
    dx: float,
    dy: float,
    ax: float,
    ay: float,
    bx: float,
    by: float,
) -> float | None:
    """Distance along the ray to a segment, or None when there is no hit."""
    ex, ey = bx - ax, by - ay
    denom = dx * ey - dy * ex
    if abs(denom) < 1e-12:
        return None
    fx, fy = ax - ox, ay - oy
    t = (fx * ey - fy * ex) / denom
    u = (fx * dy - fy * dx) / denom
    if t >= 0 and 0 <= u <= 1:
        return t
    return None


def cast_ray(
    ox: float, oy: float, angle: float, world: WorldConfig
) -> float:
    """Cast one ray; return the distance to the nearest wall or obstacle."""
    dx, dy = math.cos(angle), math.sin(angle)
    best = world.scan_max_range
    width, height = world.width, world.height
    walls = [
        (0.0, 0.0, width, 0.0),
        (width, 0.0, width, height),
        (width, height, 0.0, height),
        (0.0, height, 0.0, 0.0),
    ]
    for obstacle in world.obstacles:
        walls.append((obstacle.x, obstacle.y, obstacle.x_max, obstacle.y))
        walls.append((obstacle.x_max, obstacle.y, obstacle.x_max, obstacle.y_max))
        walls.append((obstacle.x_max, obstacle.y_max, obstacle.x, obstacle.y_max))
        walls.append((obstacle.x, obstacle.y_max, obstacle.x, obstacle.y))
    for ax, ay, bx, by in walls:
        hit = _ray_segment_intersection(ox, oy, dx, dy, ax, ay, bx, by)
        if hit is not None and hit < best:
            best = hit
    return max(MIN_RANGE, min(best, world.scan_max_range))


def compute_scan(x: float, y: float, theta: float, world: WorldConfig) -> list[float]:
    """Laser-scan ranges centered on the robot heading."""
    rays = world.num_rays
    if rays == 1:
        angles = [theta]
    else:
        start = theta - world.scan_fov / 2.0
        step = world.scan_fov / rays if world.scan_fov >= 2.0 * math.pi - 1e-9 else (
            world.scan_fov / (rays - 1)
        )
        angles = [start + i * step for i in range(rays)]
    return [cast_ray(x, y, angle, world) for angle in angles]


class Simulator:
    """Fixed-timestep differential-drive simulator with a timestamped trace."""

    def __init__(self, world: WorldConfig, seed: int = 0) -> None:
        self.world = world
        self.seed = seed
        self._rng = random.Random(seed)
        self.invalid_command_count = 0
        self.reset()

    def reset(self) -> dict[str, Any]:
        self.x = self.world.start.x
        self.y = self.world.start.y
        self.theta = normalize_angle(self.world.start.theta)
        self.v = 0.0
        self.omega = 0.0
        self.t = 0.0
        self._rng = random.Random(self.seed)
        self.invalid_command_count = 0
        self._trace: list[dict[str, Any]] = []
        self._collided = pose_in_collision(self.x, self.y, self.world)
        sample = self._record_sample(
            collision=self._collided, estop_active=False, commands_valid=True
        )
        return sample

    @property
    def trace(self) -> list[dict[str, Any]]:
        return [dict(sample) for sample in self._trace]

    @property
    def goal_reached(self) -> bool:
        return (
            math.hypot(self.x - self.world.goal.x, self.y - self.world.goal.y)
            <= self.world.goal.radius
        )

    @property
    def collided(self) -> bool:
        return self._collided

    @property
    def elapsed(self) -> float:
        return self.t

    def odom(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y, "theta": self.theta, "v": self.v,
                "omega": self.omega, "t": self.t}

    def scan(self) -> list[float]:
        ranges = compute_scan(self.x, self.y, self.theta, self.world)
        return self._apply_noise(ranges)

    def step(
        self,
        v_cmd: float,
        omega_cmd: float,
        estop_active: bool = False,
        *,
        include_ranges: bool = False,
    ) -> dict[str, Any]:
        world = self.world
        commands_valid = math.isfinite(v_cmd) and math.isfinite(omega_cmd)
        if not commands_valid:
            self.invalid_command_count += 1
            v_cmd = 0.0 if not math.isfinite(v_cmd) else v_cmd
            omega_cmd = 0.0 if not math.isfinite(omega_cmd) else omega_cmd

        if estop_active:
            v, omega = 0.0, 0.0
        else:
            v = min(max(v_cmd, -world.max_linear_speed), world.max_linear_speed)
            omega = min(
                max(omega_cmd, -world.max_angular_speed), world.max_angular_speed
            )

        new_x, new_y, new_theta = integrate_diff_drive(
            self.x, self.y, self.theta, v, omega, world.dt
        )
        new_theta = normalize_angle(new_theta)
        collision = pose_in_collision(new_x, new_y, world)
        if collision:
            self._collided = True
            self.v, self.omega = 0.0, 0.0
        else:
            self.x, self.y, self.theta = new_x, new_y, new_theta
            self.v, self.omega = v, omega
        self.t += world.dt
        return self._record_sample(
            collision=collision,
            estop_active=estop_active,
            commands_valid=commands_valid,
            include_ranges=include_ranges,
        )

    def run_commands(
        self,
        commands: list[tuple[float, float]],
        *,
        estop_at: int | None = None,
    ) -> list[dict[str, Any]]:
        """Drive an open-loop command list; latch e-stop from step `estop_at`."""
        samples: list[dict[str, Any]] = []
        for index, (v_cmd, omega_cmd) in enumerate(commands):
            samples.append(
                self.step(v_cmd, omega_cmd, estop_active=estop_at is not None and index >= estop_at)
            )
        return samples

    def _apply_noise(self, ranges: list[float]) -> list[float]:
        std = self.world.scan_noise_std
        if std <= 0:
            return list(ranges)
        noisy = [r + self._rng.gauss(0.0, std) for r in ranges]
        return [max(MIN_RANGE, min(r, self.world.scan_max_range)) for r in noisy]

    def _record_sample(
        self,
        *,
        collision: bool,
        estop_active: bool,
        commands_valid: bool,
        include_ranges: bool = False,
    ) -> dict[str, Any]:
        ranges = self._apply_noise(
            compute_scan(self.x, self.y, self.theta, self.world)
        )
        sample: dict[str, Any] = {
            "t": self.t,
            "x": self.x,
            "y": self.y,
            "theta": self.theta,
            "v": self.v,
            "omega": self.omega,
            "collision": collision,
            "goal_reached": self.goal_reached,
            "estop_active": estop_active,
            "min_range": min(ranges) if ranges else None,
        }
        if not commands_valid:
            sample["commands_valid"] = False
        if include_ranges:
            sample["ranges"] = ranges
        self._trace.append(dict(sample))
        return dict(sample)
