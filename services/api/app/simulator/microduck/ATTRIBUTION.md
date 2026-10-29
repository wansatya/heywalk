# MicroDuck Model Attribution

The robot model vendored in this directory is the official open-source
MicroDuck model from Pollen Robotics (Hugging Face).

## Sources

- Sim model, scenes, and training stack:
  https://github.com/pollen-robotics/microduck_rl
  (vendored at commit `bae39f5900b318f7e87fc485c30a31213cfa14eb`)
- Robot software and kinematics reference:
  https://github.com/pollen-robotics/microduck
  (inspected at commit `5def2f3e66501f0f365d4fb1183020b8ee875443`)
- Browser simulator:
  https://huggingface.co/spaces/pollen-robotics/microduck-simulator

## Vendored files

- `velstand.onnx` — the upstream velocity/stand locomotion policy from
  https://huggingface.co/pollen-robotics/microduck-policies
  (sha256 `2fc7f8ee6acd7d0d3f66a99927b5d5a56589aeed02ec714de329b252d5ccf492`,
  downloaded 2026-10-28). 50 Hz feed-forward actor: 61-D observation
  (trunk gyro, projected gravity, 14 joint pos/vel rel. STAND, last
  action, twist/head/body commands) → 14-D position-target residual
  added to the STAND keyframe. **Apache License 2.0**,
  copyright Pollen Robotics. HeyWalk runs this policy inside its own
  harness as the low-level walking controller; it is not retrained or
  modified.

- `robot_groundcontact.xml` — byte-identical copy of
  `src/mjlab_microduck/robot/microduck/robot_groundcontact.xml`.
  This is the model the official default scene (`scene.xml`, used by
  `sim/body_server.py`) loads: 14 hinge joints, 14 position actuators,
  6 IMU sensors, CAD inertias, foot-sole ground contacts plus head/leg/trunk
  collision geoms.
- `robot_walk.xml` — byte-identical copy of
  `src/mjlab_microduck/robot/microduck/robot_walk.xml`, kept as the
  upstream locomotion reference (identical joints/actuators/sensors; fewer
  collision geoms). Not loaded by HeyWalk runs.
- `scene_walk.xml`, `scene.xml` — byte-identical copies of the official
  scene files. HeyWalk reads the floor definition and the
  `INIT`/`STAND`/`SIT`/`FOLD` keyframes from `scene.xml`.
- `assets/*.stl` (38 files) — the meshes `robot_groundcontact.xml`
  references, copied unchanged from
  `src/mjlab_microduck/robot/microduck/assets/`.

## Licenses (inspected separately, as required)

- MJCF `.xml` files: **Apache License 2.0**,
  copyright Pollen Robotics. See https://github.com/pollen-robotics/microduck_rl/blob/main/LICENSE
- `assets/*.stl` meshes: **Creative Commons BY-NC-SA 4.0**
  (Attribution-NonCommercial-ShareAlike 4.0 International).
  See `LICENSE-MODELS.md` (copy of the upstream file) and
  https://creativecommons.org/licenses/by-nc-sa/4.0/
- When sharing these meshes or an adaptation: credit Pollen Robotics, link
  the source repository and the CC BY-NC-SA 4.0 license, note modifications,
  and license adaptations under CC BY-NC-SA 4.0.

## What is NOT included

No hardware CAD sources, manufacturing files, or Onshape documents are
vendored. The simulation assets grant no hardware manufacturing rights.

## HeyWalk compatibility changes

HeyWalk loads the official robot XML unchanged except for the composition
performed by `model.compose_scene_xml()`: the official `<worldbody>` robot
subtree is preserved element-for-element, and a ground plane plus the
official keyframes (from `scene.xml`) are added so the model simulates
stand-alone. Joint hierarchy, bodies, inertias, actuators, sensors, and
contact classes are untouched. See `docs/microduck-integration.md`.
