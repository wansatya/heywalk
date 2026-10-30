"""Meta Muse Spark integration (AGENTS.md section 6).

Live client for the official Meta Model API plus prompt builders, structured
output schemas, and response parsing/validation.

API choice (verified against Meta's public docs and provider references):
the Meta Model API lives at ``https://api.meta.ai/v1`` (configurable via
``META_API_BASE_URL``), authenticates with ``Authorization: Bearer
MODEL_API_KEY``, and accepts the Responses, Chat Completions, and Messages
formats for ``muse-spark-1.3``. This client uses the OpenAI-compatible Chat
Completions endpoint (``POST {base}/chat/completions``) with a JSON-schema
``response_format`` so controller code arrives as machine-readable structured
output. Response parsing stays tolerant: JSON is extracted from the message
text whether or not the server honors ``response_format``.

The API key lives server-side only (``Settings.model_api_key``). It is sent
as a Bearer [redacted] and never logged, never included in error messages, and
never returned to the browser or written into projects/exports.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from pydantic import ValidationError

from ..config import Settings
from ..models.schemas import (
    FixResult,
    GeneratedFile,
    GenerationResult,
    TaskInterpretation,
)

PROMPT_VERSION = "heywalk-prompts-v4"

CHAT_COMPLETIONS_PATH = "/chat/completions"
MAX_ATTEMPTS = 3
BACKOFF_BASE_S = 1.0
#: Output budget per model call. A controller plus a multi-joint MJCF can
#: exceed 8k tokens; truncation then presents as "not valid JSON", so the
#: budget stays generous and truncation is detected explicitly.
MAX_COMPLETION_TOKENS = 16000
GENERATION_TEMPERATURE = 0.2
#: Extra model calls after an unusable reply (malformed JSON or schema
#: mismatch), each carrying a repair nudge. One is enough: a retry usually
#: succeeds, and repeated failures need eyes, not more calls.
MAX_REPAIR_ATTEMPTS = 1

# Bounded evidence for diagnosis requests (AGENTS.md section 9: bounded).
MAX_LOG_LINES = 50
MAX_LOG_LINE_CHARS = 500
MAX_EVIDENCE_CHARS = 12000
MAX_FILE_CHARS_IN_PROMPT = 8000


class MetaModelError(Exception):
    """Base class for Muse Spark integration failures.

    Handlers attach a stage trace (and sometimes a hint) before the error
    reaches the API layer so failures explain what happened under the hood.
    """

    trace: list[dict[str, Any]] | None = None
    hint: str | None = None


class MetaModelNotConfiguredError(MetaModelError):
    """No API key / live client available. The API maps this to HTTP 503."""


class MetaModelResponseError(MetaModelError):
    """The model call failed or returned unusable output (mapped to 502)."""


@dataclass(frozen=True)
class GenerationContext:
    task_description: str
    existing_files: list[dict[str, str]]


@dataclass(frozen=True)
class FixEvidence:
    task_description: str
    controller_files: list[dict[str, str]]
    run_status: str
    failure_summary: str | None
    exit_code: int | None
    recent_logs: list[str]
    last_state: dict[str, Any] | None
    evaluation: dict[str, Any] | None
    metrics: dict[str, Any] | None
    #: Objective automated-test evidence from a MuJoCo run (AGENTS.md §8).
    test_results: tuple[dict[str, Any], ...] = ()
    backend: str = "differential_drive"


class MetaModelClient(Protocol):
    """AI service module interface consumed by the generation handlers."""

    model_name: str

    async def generate_controller(
        self, context: GenerationContext
    ) -> GenerationResult: ...

    async def propose_fix(self, evidence: FixEvidence) -> FixResult: ...

    async def interpret_task(self, task_description: str) -> TaskInterpretation: ...


# ---------------------------------------------------------------------------
# Structured output JSON schemas (sent as response_format.json_schema).
# ---------------------------------------------------------------------------

_GENERATED_FILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "content": {"type": "string"},
    },
    "required": ["path", "content"],
    "additionalProperties": False,
}

_OBJECTIVE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "type": {"type": "string"},
        "target": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "array",
                    "items": {"type": "number"},
                    "minItems": 2,
                    "maxItems": 2,
                },
            ]
        },
        "target_body": {
            "anyOf": [{"type": "null"}, {"type": "string"}]
        },
    },
    "required": ["type", "target", "target_body"],
    "additionalProperties": False,
}

GENERATION_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "files": {"type": "array", "items": _GENERATED_FILE_SCHEMA},
        "expected_behavior": {"type": "string"},
        "safety_considerations": {"type": "string"},
        # Ordered objective phases (1+, no cap); the brief instructs the
        # model to declare one entry per goal in the task, in order.
        "objectives": {
            "type": "array",
            "items": _OBJECTIVE_SCHEMA,
            "minItems": 1,
        },
    },
    "required": [
        "summary",
        "assumptions",
        "files",
        "expected_behavior",
        "safety_considerations",
        # Strict response_format requires every property key to be listed.
        "objectives",
    ],
    "additionalProperties": False,
}

FIX_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "failure_hypothesis": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "proposed_change": {"type": "string"},
        "updated_files": {"type": "array", "items": _GENERATED_FILE_SCHEMA},
        "expected_improvement": {"type": "string"},
    },
    "required": [
        "failure_hypothesis",
        "evidence",
        "proposed_change",
        "updated_files",
        "expected_improvement",
    ],
    "additionalProperties": False,
}

INTERPRET_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "goal": {"type": "string"},
        "safety_requirements": {
            "type": "array",
            "items": {"type": "string"},
        },
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "needs_clarification": {"type": "boolean"},
        "clarification_question": {"type": "string"},
        "suggested_task": {"type": "string"},
    },
    "required": [
        "goal",
        "safety_requirements",
        "assumptions",
        "needs_clarification",
        "clarification_question",
        "suggested_task",
    ],
    "additionalProperties": False,
}


def _response_format(name: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {"name": name, "strict": True, "schema": schema},
    }


# ---------------------------------------------------------------------------
# Prompt builders.
# ---------------------------------------------------------------------------

MUJOCO_INTERFACE_BRIEF = """\
MuJoCo joint-control contract (ROS 2 Jazzy, Python rclpy node). The primary
experiment runs your controller against a MuJoCo model with an emulated
ros2_control position command interface:
- /joint_states (sensor_msgs/msg/JointState): joint names, positions (rad or
  m), velocities and efforts, published by the simulator every control tick.
- /joint_targets (std_msgs/msg/Float64MultiArray): the current automated test
  target, one value per actuated joint in <robot.xml> actuator order.
- /joint_commands (std_msgs/msg/Float64MultiArray): YOUR position commands,
  one value per actuated joint, same order. This is the ros2_control position
  command interface; the simulator applies them to the position actuators.
- /emergency_stop (std_msgs/msg/Bool): when True, hold the current joint
  positions immediately and stop commanding motion.
Use a timer-driven control loop (~50 Hz). Command only finite values, keep
exactly one value per actuated joint, and clamp each command to the joint range
declared in <robot.xml>. Never block the executor: NO time.sleep, NO
busy-wait loops, NO heavy computation in any subscription callback or timer
tick — each callback must finish in a few milliseconds, because the
simulator advances only after your callback returns and a sleeping
controller stalls the whole run (choppy telemetry, wall-clock timeout).
Read the latest message, compute, publish, return; use the timer period —
never a sleep call — for pacing. Structure the main module as
one rclpy Node subclass constructible with no arguments (or provide a
create_node() factory); the harness instantiates it and bridges it to the
simulator over these topics."""

LEGACY_2D_INTERFACE_BRIEF = """\
Legacy 2D differential-drive worlds (only when the task explicitly asks for a
mobile base navigation experiment AND the project has no MuJoCo robot model)
use /cmd_vel (geometry_msgs/msg/Twist, you publish velocity commands), /odom
(nav_msgs/msg/Odometry), /scan (sensor_msgs/msg/LaserScan), /goal_pose
(geometry_msgs/msg/PoseStamped) and the same /emergency_stop. Keep speeds
within ±0.5 m/s linear and ±1.5 rad/s angular in that mode. The same
no-blocking rule applies: never time.sleep or busy-wait in a callback."""

#: Grading contract, stated only when the target is the official MicroDuck.
#: Two run modes share one controller file: joint tests grade tracking,
#: locomotion runs grade goal-directed walking steered over /cmd_vel.
MICRODUCK_GRADING_BRIEF = """\
How this robot is graded — two run modes, implement BOTH in one controller:
1. Joint tests (default): the harness moves one joint at a time to a target
   published on /joint_targets. Your controller MUST read every target and
   track it on /joint_commands (all 14 joints, every tick): measured
   tracking error, settling, and contacts are the entire grade. Before the
   first target arrives, hold the STAND home pose below so every test
   starts from a known pose instead of a zeroed one.
2. Locomotion runs: the harness walks the duck with a built-in walking
   policy while your controller STEERS it — /joint_commands are ignored in
   this mode. Subscribe /odom (nav_msgs/msg/Odometry: your x, y, yaw) and
   /goal_pose (geometry_msgs/msg/PoseStamped: the objective target x, y),
   and publish /cmd_vel (geometry_msgs/msg/Twist: forward m/s, yaw rad/s,
   at most 0.4 m/s). Steer only when /odom arrives — joint-test runs
   publish no odom, so the same file serves both modes.
   The locomotion OBJECTIVES come from the TASK TEXT — not from this
   template. Declare them in the response "objectives" field as an ordered
   list with ONE entry per goal in the task, IN ORDER (1 or more, no cap):
   [{"type": ..., "target": [x, y] or null, "target_body": "env_..." or null},
   ...]. The phases run in sequence, and EACH PHASE GETS ITS OWN ~20 s
   WINDOW counted from the moment it starts: each completed phase advances
   to the next, /goal_pose republishes to the new goal every tick, and the
   run passes only when EVERY phase does. The window is generous for one
   phase but not for two, so plan the most direct route you can per phase.
   A "then" in the task is a phase boundary: "Kick the ball away from
   you, then walk to the green flag and stop safely." declares
   [{"type": "kick", ...}, {"type": "go_to", ...}] — never a single phase.
   - go_to (default): reach a point and stop. Target null means the flag.
     When the task names a scene prop ("go to the box in front of you",
     "walk to the ball"), set target_body to that prop's body id instead
     of guessing coordinates: the runner resolves the prop's live position
     and publishes it on /goal_pose. Use "target": [x, y] only for a
     bare point inside the ±2 m arena. Steer to /goal_pose and stop
     inside 0.3 m; the runner stands there. Passes with no fall and no
     obstacle contact.
   - kick: the task asks to kick / push / play with the ball. Leave target
     and target_body null. Keep the SAME steering loop as go_to but never
     stop: publish forward + proportional yaw toward /goal_pose (the ball's
     start position) every tick while the phase lasts and let the harness
     advance it. Do NOT add a stop radius, a "contact latch", or a fixed
     straight push heading — a duck that stops at 0.3 m, or freezes its
     heading once it is near, curves past the ball and the run fails with
     "ball never touched". The walker is slow (roughly 0.2 m/s of real
     travel for a 0.2 m/s command), so a ball 0.4 m away needs ~2-3 s of
     walking and the first contact is often later than you expect. The
     verdict needs a real roll: the ball is dynamic and must be pushed
     clear.
   - touch: the task asks to hit/touch a wall. Target the nearest wall
     point (e.g. [1.9, 0.0] for the +x wall). Drive toward /goal_pose
     and DO NOT stop at the 0.3 m radius — keep walking gently until
     contact, which passes the phase. Other contacts still fail.
   - wander: the task asks to roam/patrol with no destination. Ignore
     /goal_pose; keep walking (steady forward speed, gentle turns)
     with no window until the arena wall ends the roam passing.
     Touching the wall passes the phase, but any obstacle contact
     or a fall fails it — steer around the crates, the ball, and the
     flag pole. Wander ends the run instead of advancing, so it MUST
     be the last phase.
   Steering across phases: subscribe /objective_state (std_msgs/msg/String,
   the CURRENT phase type, published every tick) and switch behavior per
   phase — drive through kick/touch goals without stopping, stop inside
   0.3 m for go_to, ignore the goal while wandering. Read the LIVE
   /goal_pose and /objective_state every tick; never latch a heading,
   a stop radius, or a "done" flag that assumes a single goal, or later
   phases steer stale.
   Steering has no obstacle sensing (open loop): prefer slow speeds and
   wide turns, and steer around the crates and the flag by geometry.
   Touch/wander controllers must route around the flag pole — it stands
   on the straight line to the +x wall.
In both modes /emergency_stop (std_msgs/msg/Bool) latches a stop: hold
still immediately and command no further motion."""

ROBOT_PACKAGE_BRIEF = """\
Robot package to generate (MuJoCo is the primary simulator):
- controller.py -- the ROS 2 node described above.
- robot.xml -- a MuJoCo MJCF model: a <mujoco> root containing <worldbody>,
  bodies, and NAMED <hinge> or <slide> joints, each with an explicit
  range="lower upper". Add exactly one <position> actuator per joint under
  <actuator>, with kp/damping tuned so the joint reaches its target without
  sustained oscillation. Joint names must be unique and stable; they appear on
  /joint_states, /joint_commands and /joint_targets in this order.
- robot.urdf -- optional URDF whose joint names match the MJCF (the platform
  synthesizes a kinematic skeleton when you omit it).
- ros2_control.yaml -- optional ros2_control configuration that mentions every
  joint (the platform synthesizes it when you omit it).
Supported joints are hinge and slide only; one position actuator per joint.
No DOCTYPE/ENTITY declarations and no absolute or "../" asset paths."""

GENERATE_SYSTEM = """\
You are HeyWalk's robotics controller generator. Write a ROS 2 Python node
plus a MuJoCo robot description that accomplish the user's task in a
deterministic simulated environment, then the automated joint tests will move
each actuated joint to a target and check position, velocity, and contacts.
Respond with a single JSON object matching the required schema and nothing
else: no markdown fences, no commentary outside the JSON.
"""

FIX_SYSTEM = """\
You are HeyWalk's failure-diagnosis assistant. Given a task, a ROS 2
controller, a MuJoCo robot description, and structured failure evidence from a
real simulation run (including automated joint-test results), find the most
likely cause and propose a minimal targeted correction. Preserve safety
requirements (joint limits, velocity limits, emergency-stop handling). Prefer a
small patch over a rewrite. Respond with a single JSON object matching the
required schema and nothing else: no markdown fences, no commentary outside the
JSON.
"""

INTERPRET_SYSTEM = """\
You are HeyWalk's task interpreter. Convert a natural-language robot
instruction into a structured task: goal, safety requirements, and environment
assumptions. If the instruction is ambiguous or unsafe to interpret reliably,
set needs_clarification to true and ask one focused question. Respond with a
single JSON object matching the required schema and nothing else.
"""


def truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[truncated {len(text) - limit} chars]"


def _render_files(files: list[dict[str, str]]) -> str:
    if not files:
        return "(no existing files)"
    parts = []
    for entry in files:
        content = truncate_text(
            str(entry.get("content", "")), MAX_FILE_CHARS_IN_PROMPT
        )
        parts.append(f"--- {entry.get('path', '(unnamed)')} ---\n{content}")
    return "\n".join(parts)


def _render_evidence(evidence: FixEvidence) -> str:
    lines = [
        f"backend: {evidence.backend}",
        f"run_status: {evidence.run_status}",
        f"failure_summary: {evidence.failure_summary or '(none)'}",
        f"exit_code: {evidence.exit_code}",
    ]
    if evidence.metrics:
        lines.append(f"metrics: {json.dumps(evidence.metrics)[:2000]}")
    if evidence.evaluation:
        lines.append(f"evaluation: {json.dumps(evidence.evaluation)[:2000]}")
    if evidence.last_state:
        lines.append(f"last_state: {json.dumps(evidence.last_state)[:2000]}")
    if evidence.test_results:
        lines.append(
            "automated_test_results: "
            f"{json.dumps(list(evidence.test_results))[:4000]}"
        )
    log_lines = (evidence.recent_logs or [])[-MAX_LOG_LINES:]
    rendered_logs = "\n".join(
        truncate_text(line, MAX_LOG_LINE_CHARS) for line in log_lines
    ) or "(no logs)"
    lines.append(f"recent_logs (last {len(log_lines)}):\n{rendered_logs}")
    return truncate_text("\n".join(lines), MAX_EVIDENCE_CHARS)


def target_robot_brief(files: list[dict[str, str]]) -> str:
    """Describe the project's target robot for the code-generation agent.

    When the project holds the official MicroDuck composition, the agent gets
    its exact joint/actuator/sensor inventory, the props it can be asked about
    (body id + live coordinates, generated from the composed scene so prompt
    and physics cannot disagree), and the control contract. That lets it map
    "the box in front of you" onto a real body instead of inventing numbers.
    Generated rigs need no brief: the agent authors their MJCF itself.
    """
    from ..simulator.microduck import (
        is_composed_scene,
        load_metadata,
        prop_catalog,
        stand_joint_targets,
    )

    for entry in files:
        content = str(entry.get("content", ""))
        if "<mujoco" in content[:4096].lower() and is_composed_scene(content):
            break
    else:
        return ""
    metadata = load_metadata()
    joints = ", ".join(metadata.joints)
    sensors = ", ".join(metadata.sensors)
    home = ", ".join(
        f"{name}={value:+.4f}" for name, value in stand_joint_targets()
    )
    props = "; ".join(
        f"{prop['body']} = {prop['label']} at ({prop['x']:+.2f},"
        f" {prop['y']:+.2f}){' [dynamic]' if prop['free'] else ''}"
        for prop in prop_catalog()
    )
    return (
        "Target robot: official MicroDuck biped by Pollen Robotics"
        " (already installed as robot.xml — do NOT rewrite it)."
        f" 14 position actuators in this exact order: {joints}."
        " Publish exactly 14 finite commands on /joint_commands in that"
        " order, clamped to each joint's range in robot.xml."
        f" Onboard sensors streamed in telemetry: {sensors}."
        f" STAND home pose in the same order (rad): {home}."
        " Joint-test runs weld the trunk to a stand at STAND height (the free"
        " joint in robot.xml is locked), so gradeable tracking never fights"
        " a fall; locomotion runs instead walk free-standing to the flag."
        " Gravity and foot-sole ground contact stay real, and touching an"
        " obstacle still fails the run. Control at 50 Hz (dt=0.005)."
        f" Scene props (body id = description at start position): {props}."
        " The duck spawns at (0, 0) facing +x, so 'in front' means +x,"
        " 'behind' means -x, 'left' is +y and 'right' is -y; the arena walls"
        " sit at ±2 m. Use these body ids for objective.target_body instead"
        " of coordinates — the arrange UI can move the props, and a body id"
        " tracks them. Only env_ball is dynamic (it rolls when pushed)."
    )


def _interface_section(brief: str) -> str:
    """Control-contract section for a generation/fix prompt.

    Official-MicroDuck projects get the joint contract plus the dual-mode
    grading rules (joint tracking + locomotion steering) and never see the
    legacy 2D paragraph: that mode's scan-based contract does not apply to
    the duck. Generic projects keep both contracts so the model can author
    either backend.
    """
    if brief:
        return f"{MUJOCO_INTERFACE_BRIEF}\n\n{MICRODUCK_GRADING_BRIEF}\n\n{brief}"
    return f"{MUJOCO_INTERFACE_BRIEF}\n\n{LEGACY_2D_INTERFACE_BRIEF}"


def build_generate_messages(context: GenerationContext) -> list[dict[str, str]]:
    brief = target_robot_brief(context.existing_files)
    if brief:
        # The robot is already installed: the model must output ONLY the
        # controller. Emitting robot.xml again (tens of thousands of
        # characters) blows the completion budget and truncates the JSON.
        closing = (
            "Update controller.py ONLY for this installed robot: output"
            " exactly ONE file (controller.py). Do NOT output robot.xml,"
            " robot.urdf, or ros2_control.yaml — they are already installed"
            " and must not be rewritten. Start from the installed"
            " controller.py (the platform's scripted gait: 50 Hz rate-limited"
            " joint tracking plus /cmd_vel steering toward /goal_pose) and"
            " adapt it to THIS task: keep what already works and change only"
            " what the objective needs — do not write a controller from"
            " scratch. Keep the joint-tracking mode intact so joint tests"
            " still pass. Explain your approach in"
            " summary/expected_behavior and list safety considerations"
            " explicitly."
        )
    else:
        closing = (
            "Generate the controller and robot description. Put the main"
            " node at controller.py. Explain your approach in"
            " summary/expected_behavior and list safety considerations"
            " explicitly."
        )
    user = (
        f"Task:\n{truncate_text(context.task_description, 4000)}\n\n"
        f"{_interface_section(brief)}\n\n"
        f"{ROBOT_PACKAGE_BRIEF}\n\n"
        f"Existing project files:\n"
        f"{_render_files(context.existing_files)}\n\n"
        f"{closing}"
    )
    return [
        {"role": "system", "content": GENERATE_SYSTEM},
        {"role": "user", "content": user},
    ]


def build_fix_messages(evidence: FixEvidence) -> list[dict[str, str]]:
    brief = target_robot_brief(evidence.controller_files)
    user = (
        f"Task:\n{truncate_text(evidence.task_description, 4000)}\n\n"
        f"{_interface_section(brief)}\n\n"
        f"{ROBOT_PACKAGE_BRIEF}\n\n"
        f"Current controller files:\n"
        f"{_render_files(evidence.controller_files)}\n\n"
        f"Failure evidence:\n{_render_evidence(evidence)}\n\n"
        "Diagnose the most likely failure cause from the evidence and return "
        "corrected files in updated_files. Fix the smallest set of files: a "
        "wrong joint order, a missing command, or a gain that cannot settle "
        "usually needs one file."
    )
    return [
        {"role": "system", "content": FIX_SYSTEM},
        {"role": "user", "content": user},
    ]


def build_interpret_messages(task_description: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": INTERPRET_SYSTEM},
        {
            "role": "user",
            "content": f"Instruction:\n{truncate_text(task_description, 4000)}",
        },
    ]


# ---------------------------------------------------------------------------
# Response parsing and validation (model output is untrusted).
# ---------------------------------------------------------------------------


def extract_json_object(text: str) -> dict[str, Any]:
    """Extract a JSON object from model text.

    Accepts raw JSON, fenced code blocks, or JSON embedded in prose.
    Raises MetaModelResponseError when no valid JSON object is found.
    """
    if not isinstance(text, str) or not text.strip():
        raise MetaModelResponseError("Model returned an empty response.")
    candidate = text.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end <= start:
            raise MetaModelResponseError(
                "Model output was not valid JSON: no JSON object found."
            ) from None
        try:
            parsed = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError as exc:
            raise MetaModelResponseError(
                f"Model output was not valid JSON: {exc.msg}."
            ) from None
    if not isinstance(parsed, dict):
        raise MetaModelResponseError(
            "Model output must be a JSON object matching the required schema."
        )
    return parsed


def _validate_model(model_cls: Any, data: dict[str, Any], purpose: str) -> Any:
    try:
        return model_cls.model_validate(data)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
            for err in exc.errors()[:5]
        )
        raise MetaModelResponseError(
            f"Model {purpose} output failed schema validation: {details}."
        ) from None


def _require_files(files: list[GeneratedFile], field: str, purpose: str) -> None:
    if not files:
        raise MetaModelResponseError(
            f"Model {purpose} output contained no files ({field} is empty);"
            " refusing to write an empty update."
        )
    for entry in files:
        if not entry.path.strip():
            raise MetaModelResponseError(
                f"Model {purpose} output contained a file with an empty path."
            )


#: Tasks asking to kick the ball must be graded as kick runs. The match is
#: deliberately tight (kick/kicks/kicked/kicking only): "walk to the ball"
#: is a go_to task and must not trip this guard.
_KICK_TASK_RE = re.compile(r"\bkick\w*\b", re.IGNORECASE)


def _require_objective_entry(objective: Any, index: int) -> None:
    """Validate one declared objective phase (type/target/body rules)."""
    label = f"objectives[{index}]"
    if objective.type not in ("go_to", "touch", "wander", "kick"):
        raise MetaModelResponseError(
            f"Model generation output declared an unknown {label} type"
            f" {objective.type!r}; expected go_to, touch, wander, or kick."
        )
    if objective.target is not None:
        if objective.type in ("wander", "kick"):
            raise MetaModelResponseError(
                f"Model generation output gave {label} ({objective.type}) a"
                f" target; {objective.type} runs have no destination point"
                " (target must be null)."
            )
        if len(objective.target) != 2 or any(
            not isinstance(v, (int, float)) for v in objective.target
        ):
            raise MetaModelResponseError(
                f"Model generation output gave a malformed {label}"
                " target; expected [x, y] or null."
            )
        for value in objective.target:
            if not (-2.0 <= float(value) <= 2.0):
                raise MetaModelResponseError(
                    f"Model generation output placed the {label} target"
                    f" {objective.target} outside the ±2 m arena."
                )
    if objective.target_body is not None:
        if objective.type != "go_to":
            raise MetaModelResponseError(
                f"Model generation output set {label}.target_body for a"
                f" {objective.type} run; target_body only applies to go_to."
            )
        from ..simulator.microduck import prop_body_names

        known = prop_body_names()
        if objective.target_body not in known:
            raise MetaModelResponseError(
                f"Model generation output named an unknown {label} target"
                f" body {objective.target_body!r}; use one of the composed"
                f" props: {', '.join(known)}."
            )


def _require_objectives(
    result: GenerationResult, task_description: str | None = None
) -> None:
    """Validate the declared objective sequence (1+, always required).

    The response schema requires objectives, but a server that does not
    enforce response_format lets the model omit them — and a silent null
    grades every task as a flag run (a kick controller then "succeeds" at
    walking to the flag). Omission, misdeclaration, and kick-task misses
    therefore raise, which triggers the client's automatic repair retry.
    """
    objectives = result.objectives
    if not objectives:
        raise MetaModelResponseError(
            "Model generation output omitted the required objectives field;"
            " declare one {type, target, target_body} entry per goal in the"
            " task, in order, with type one of go_to, touch, wander, or kick."
        )
    if (
        task_description
        and _KICK_TASK_RE.search(task_description)
        and all(objective.type != "kick" for objective in objectives)
    ):
        raise MetaModelResponseError(
            "Model generation output declared no kick phase for a task"
            " that asks to kick the ball; include type 'kick' with null"
            " target and target_body."
        )
    for index, objective in enumerate(objectives):
        _require_objective_entry(objective, index)
    for objective in objectives[:-1]:
        if objective.type == "wander":
            raise MetaModelResponseError(
                "Model generation output placed wander before the last"
                " phase; wander ends the run instead of advancing, so it"
                " must be last."
            )


def parse_generation_result(
    data: dict[str, Any] | str, task_description: str | None = None
) -> GenerationResult:
    payload = extract_json_object(data) if isinstance(data, str) else data
    if isinstance(payload, dict) and payload.get("objectives") is None:
        # Tolerant path for a model that emits the legacy singular field.
        legacy = payload.get("objective")
        if legacy is not None:
            payload = {**payload, "objectives": [legacy]}
    result = _validate_model(GenerationResult, payload, "generation")
    _require_files(list(result.files), "files", "generation")
    _require_objectives(result, task_description)
    return result


def parse_fix_result(data: dict[str, Any] | str) -> FixResult:
    payload = extract_json_object(data) if isinstance(data, str) else data
    result = _validate_model(FixResult, payload, "correction")
    _require_files(list(result.updated_files), "updated_files", "correction")
    return result


def parse_interpretation(data: dict[str, Any] | str) -> TaskInterpretation:
    payload = extract_json_object(data) if isinstance(data, str) else data
    return _validate_model(TaskInterpretation, payload, "interpretation")


def extract_chat_reply(payload: dict[str, Any]) -> tuple[str, str | None]:
    """Pull (assistant text, finish_reason) from a Chat Completions payload.

    The finish reason matters: ``length`` means the reply was cut off at
    the token budget, which otherwise presents as mysteriously invalid JSON.
    """
    if not isinstance(payload, dict):
        raise MetaModelResponseError("Model API returned a malformed body.")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise MetaModelResponseError(
            "Model API response contained no choices."
        )
    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message", {}) if isinstance(first, dict) else {}
    content = message.get("content", "") if isinstance(message, dict) else ""
    if isinstance(content, list):
        texts = [
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and "text" in part
        ]
        content = "".join(texts)
    if not isinstance(content, str) or not content.strip():
        raise MetaModelResponseError(
            "Model API returned an empty assistant message."
        )
    finish = first.get("finish_reason") if isinstance(first, dict) else None
    return content, str(finish) if finish is not None else None


def extract_chat_text(payload: dict[str, Any]) -> str:
    """Pull assistant text out of a Chat Completions response payload."""
    text, _ = extract_chat_reply(payload)
    return text


# ---------------------------------------------------------------------------
# Live HTTP client.
# ---------------------------------------------------------------------------


def _retryable_status(status: int) -> bool:
    return status == 429 or 500 <= status < 600


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def hint_for_error(exc: Exception) -> str | None:
    """Return a user-facing recovery hint for a model failure, if known."""
    if isinstance(exc, MetaModelNotConfiguredError):
        return None
    text = str(exc)
    lowered = text.lower()
    if "empty assistant message" in lowered or "empty response" in lowered:
        return (
            "The model returned no content — usually transient. Retry; if it "
            "persists, the model endpoint may be degraded."
        )
    if "timed out" in lowered:
        return (
            "The model did not answer in time. Retry; repeated timeouts may "
            "need a higher MODEL_TIMEOUT_S."
        )
    if "http 429" in lowered:
        return "Rate limited — wait a moment and retry."
    if "http 5" in lowered:
        return "The model endpoint errored — usually transient. Retry."
    if "http 4" in lowered:
        return (
            "The request was rejected — check the model name and endpoint "
            "configuration."
        )
    if "truncated" in lowered:
        return (
            "The model reply was cut off at the token limit before the JSON "
            "was complete. Retry — shorter outputs usually fit; if it "
            "persists, the task may need splitting."
        )
    if (
        "not valid json" in lowered
        or "malformed" in lowered
        or "missing" in lowered
        or "failed validation" in lowered
    ):
        return (
            "The model replied outside the expected JSON shape. Retry — the "
            "prompt pins a JSON schema, so a retry often succeeds."
        )
    return None


class HttpMetaModelClient:
    """Live client for the Meta Model API Chat Completions endpoint."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not settings.model_api_key:
            raise MetaModelNotConfiguredError(
                "Muse Spark API is not configured: set MODEL_API_KEY on the server."
            )
        self._api_key = settings.model_api_key
        self._base_url = settings.meta_api_base_url.rstrip("/")
        self._timeout_s = settings.model_timeout_s
        self.model_name = settings.muse_model
        self._transport = transport
        self.last_attempts: list[dict[str, Any]] = []

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    async def _chat_text(
        self,
        messages: list[dict[str, str]],
        response_format: dict[str, Any],
        purpose: str,
    ) -> tuple[str, str | None]:
        """One assistant reply with transport retries (returns text + stop)."""
        body = {
            "model": self.model_name,
            "messages": messages,
            "temperature": GENERATION_TEMPERATURE,
            "max_completion_tokens": MAX_COMPLETION_TOKENS,
            "response_format": response_format,
        }
        url = self._base_url + CHAT_COMPLETIONS_PATH
        last_error = "unknown error"
        timeout = httpx.Timeout(self._timeout_s)
        base_attempt = len(self.last_attempts)
        async with httpx.AsyncClient(
            timeout=timeout, transport=self._transport
        ) as client:
            for attempt in range(1, MAX_ATTEMPTS + 1):
                attempt_started = time.monotonic()
                try:
                    response = await client.post(
                        url, headers=self._headers(), json=body
                    )
                except httpx.TimeoutException:
                    last_error = (
                        f"request timed out after {self._timeout_s:g}s"
                    )
                    self.last_attempts.append({
                        "attempt": base_attempt + attempt,
                        "latency_ms": _elapsed_ms(attempt_started),
                        "outcome": "timeout",
                    })
                except httpx.HTTPError as exc:
                    self.last_attempts.append({
                        "attempt": base_attempt + attempt,
                        "latency_ms": _elapsed_ms(attempt_started),
                        "outcome": f"transport_error:{type(exc).__name__}",
                    })
                    raise MetaModelResponseError(
                        f"Model {purpose} request failed: "
                        f"{type(exc).__name__}."
                    ) from None
                else:
                    latency_ms = _elapsed_ms(attempt_started)
                    if response.status_code == 200:
                        try:
                            payload = response.json()
                        except ValueError:
                            self.last_attempts.append({
                                "attempt": base_attempt + attempt,
                                "latency_ms": latency_ms,
                                "outcome": "invalid_json",
                            })
                            raise MetaModelResponseError(
                                f"Model {purpose} response was not valid JSON."
                            ) from None
                        self.last_attempts.append({
                            "attempt": base_attempt + attempt,
                            "latency_ms": latency_ms,
                            "outcome": "ok",
                        })
                        return extract_chat_reply(payload)
                    last_error = (
                        f"HTTP {response.status_code}: "
                        f"{response.text[:300].strip() or '(empty body)'}"
                    )
                    self.last_attempts.append({
                        "attempt": base_attempt + attempt,
                        "latency_ms": latency_ms,
                        "outcome": f"http_{response.status_code}",
                    })
                    if not _retryable_status(response.status_code):
                        raise MetaModelResponseError(
                            f"Model {purpose} request failed: {last_error}."
                        ) from None
                if attempt < MAX_ATTEMPTS:
                    await asyncio.sleep(BACKOFF_BASE_S * (2 ** (attempt - 1)))
        raise MetaModelResponseError(
            f"Model {purpose} request failed after {MAX_ATTEMPTS} attempts:"
            f" {last_error}."
        )

    async def _chat_validated(
        self,
        messages: list[dict[str, str]],
        response_format: dict[str, Any],
        purpose: str,
        parse: Any,
    ) -> Any:
        """Assistant reply parsed into structured output, with one repair.

        A malformed reply gets a single automatic retry carrying a repair
        nudge plus the validation complaint — the manual "retry often
        succeeds" step, done by the client. Truncated replies fail fast:
        the same budget would cut them off again.
        """
        self.last_attempts = []
        text, finish = await self._chat_text(
            messages, response_format, purpose
        )
        try:
            return parse(text)
        except MetaModelResponseError as first_error:
            if finish == "length":
                raise MetaModelResponseError(
                    f"Model {purpose} reply was truncated at the"
                    f" {MAX_COMPLETION_TOKENS} token limit; the JSON is"
                    " incomplete."
                ) from None
            self.last_attempts.append({
                "attempt": "repair",
                "latency_ms": 0,
                "outcome": f"output_error:{len(text)}chars",
            })
            repair = (
                "Your previous reply could not be used:"
                f" {first_error} Reply again with ONLY a single JSON object"
                " matching the required schema — no markdown fences, no"
                " commentary, no extra text."
            )
            text, _ = await self._chat_text(
                [*messages, {"role": "user", "content": repair}],
                response_format,
                purpose,
            )
            try:
                return parse(text)
            except MetaModelResponseError as second_error:
                raise MetaModelResponseError(
                    f"Model {purpose} output was unusable after"
                    f" {MAX_REPAIR_ATTEMPTS + 1} attempts: {second_error}"
                    f" (reply was {len(text)} chars)."
                ) from None

    async def generate_controller(
        self, context: GenerationContext
    ) -> GenerationResult:
        return await self._chat_validated(
            build_generate_messages(context),
            _response_format("controller_generation", GENERATION_JSON_SCHEMA),
            "generation",
            lambda text: parse_generation_result(
                text, task_description=context.task_description
            ),
        )

    async def propose_fix(self, evidence: FixEvidence) -> FixResult:
        return await self._chat_validated(
            build_fix_messages(evidence),
            _response_format("controller_fix", FIX_JSON_SCHEMA),
            "correction",
            parse_fix_result,
        )

    async def interpret_task(self, task_description: str) -> TaskInterpretation:
        return await self._chat_validated(
            build_interpret_messages(task_description),
            _response_format("task_interpretation", INTERPRET_JSON_SCHEMA),
            "interpretation",
            parse_interpretation,
        )


# ---------------------------------------------------------------------------
# Honest stub + clearly-labeled dev mock (never presented as live output).
# ---------------------------------------------------------------------------


class StubMetaModelClient:
    """Honest placeholder: refuses to invent controller code."""

    model_name = "muse-spark-stub (live API not configured)"

    async def generate_controller(
        self, context: GenerationContext
    ) -> GenerationResult:
        raise MetaModelNotConfiguredError(
            "Muse Spark API is not configured: set MODEL_API_KEY on the server."
        )

    async def propose_fix(self, evidence: FixEvidence) -> FixResult:
        raise MetaModelNotConfiguredError(
            "Muse Spark API is not configured: set MODEL_API_KEY on the server."
        )

    async def interpret_task(
        self, task_description: str
    ) -> TaskInterpretation:
        raise MetaModelNotConfiguredError(
            "Muse Spark API is not configured: set MODEL_API_KEY on the server."
        )


DEV_MOCK_LABEL = "DEV MOCK — not a live Muse Spark result"

DEV_MOCK_CONTROLLER = '''\
"""DEV MOCK controller: tracks the automated joint targets. NOT model output."""

from rclpy.node import Node
from sensor_msgs.msg import JointState
from std_msgs.msg import Bool, Float64MultiArray


class MockJointController(Node):
    def __init__(self) -> None:
        super().__init__("dev_mock_joint_controller")
        self._targets: list[float] = []
        self._positions: list[float] = []
        self._estop = False
        self._commands = self.create_publisher(
            Float64MultiArray, "/joint_commands", 10
        )
        self.create_subscription(
            Float64MultiArray, "/joint_targets", self._on_targets, 10
        )
        self.create_subscription(JointState, "/joint_states", self._on_state, 10)
        self.create_subscription(Bool, "/emergency_stop", self._on_estop, 10)
        self.create_timer(0.02, self._tick)

    def _on_targets(self, message: Float64MultiArray) -> None:
        self._targets = [float(value) for value in message.data]

    def _on_state(self, message: JointState) -> None:
        self._positions = [float(value) for value in message.position]

    def _on_estop(self, message: Bool) -> None:
        self._estop = bool(message.data)

    def _tick(self) -> None:
        if self._estop:
            self._publish(self._positions)
            return
        if self._targets:
            self._publish(self._targets)

    def _publish(self, values: list[float]) -> None:
        message = Float64MultiArray()
        message.data = [float(value) for value in values]
        self._commands.publish(message)
'''

DEV_MOCK_ROBOT_XML = '''\
<mujoco model="heywalk_dev_mock">
  <compiler angle="radian"/>
  <option timestep="0.002"/>
  <worldbody>
    <body name="base" pos="0 0 0">
      <geom name="base_geom" type="cylinder" size="0.05 0.02"
            contype="0" conaffinity="0"/>
      <body name="upper_arm" pos="0 0 0.02" gravcomp="1">
        <joint name="shoulder" type="hinge" axis="0 1 0" range="-1.2 1.2"
               damping="0.6"/>
        <geom name="upper_arm_geom" type="capsule" fromto="0 0 0 0.22 0 0"
              size="0.015" mass="0.4" contype="0" conaffinity="0"/>
        <body name="fore_arm" pos="0.22 0 0" gravcomp="1">
          <joint name="elbow" type="hinge" axis="0 1 0" range="-1.2 1.2"
                 damping="0.3"/>
          <geom name="fore_arm_geom" type="capsule" fromto="0 0 0 0.18 0 0"
                size="0.012" mass="0.25" contype="0" conaffinity="0"/>
          <body name="tip" pos="0.18 0 0">
            <geom name="tip_geom" type="sphere" size="0.02"
                  contype="1" conaffinity="1"/>
          </body>
        </body>
      </body>
    </body>
  </worldbody>
  <actuator>
    <position name="shoulder_position" joint="shoulder" kp="40" kv="2"/>
    <position name="elbow_position" joint="elbow" kp="25" kv="1"/>
  </actuator>
</mujoco>
'''


class DevMockMetaModelClient:
    """Deterministic fake for frontend development and tests ONLY.

    Every field it returns is labeled as a mock. ``build_client`` never
    returns this class; use explicit injection in dev/test code. Presenting
    its output as a live model result would violate AGENTS.md section 14.
    """

    model_name = "dev-mock-0.1 (NOT live Muse Spark)"

    async def generate_controller(
        self, context: GenerationContext
    ) -> GenerationResult:
        return GenerationResult(
            summary=f"[{DEV_MOCK_LABEL}] canned two-joint tracking controller.",
            assumptions=["Mock ignores the task text."],
            files=[
                GeneratedFile(
                    path="controller.py", content=DEV_MOCK_CONTROLLER
                ),
                GeneratedFile(
                    path="robot.xml", content=DEV_MOCK_ROBOT_XML
                ),
            ],
            expected_behavior=(
                f"[{DEV_MOCK_LABEL}] both joints track the published targets."
            ),
            safety_considerations=(
                f"[{DEV_MOCK_LABEL}] no live safety review performed."
            ),
        )

    async def propose_fix(self, evidence: FixEvidence) -> FixResult:
        return FixResult(
            failure_hypothesis=f"[{DEV_MOCK_LABEL}] unknown; canned response.",
            evidence=[f"[{DEV_MOCK_LABEL}] run_status={evidence.run_status}"],
            proposed_change=f"[{DEV_MOCK_LABEL}] no real diagnosis.",
            updated_files=[
                GeneratedFile(
                    path="controller.py", content=DEV_MOCK_CONTROLLER
                )
            ],
            expected_improvement=f"[{DEV_MOCK_LABEL}] none claimed.",
        )

    async def interpret_task(
        self, task_description: str
    ) -> TaskInterpretation:
        return TaskInterpretation(
            goal=f"[{DEV_MOCK_LABEL}] {task_description[:200]}",
            safety_requirements=[DEV_MOCK_LABEL],
            assumptions=[DEV_MOCK_LABEL],
            needs_clarification=False,
            clarification_question="",
            suggested_task=task_description[:500],
        )


def build_client(settings: Settings) -> MetaModelClient:
    """Return the live AI client for the configured Muse model.

    Raises MetaModelNotConfiguredError when no API key is present, so callers
    surface an explicit integration error (HTTP 503). Never returns the dev
    mock: mock output must never be presented as a live model result.
    """
    if not settings.model_api_key:
        raise MetaModelNotConfiguredError(
            "Muse Spark API is not configured: set MODEL_API_KEY on the server."
        )
    return HttpMetaModelClient(settings)
