# Third-Party Notices

HeyWalk includes third-party robot descriptions, 3D mesh assets, a pretrained
locomotion policy, and simulation/runtime software. Those components remain
under their own licenses. The MIT license in [`LICENSE`](LICENSE) applies only
to HeyWalk's original application code and documentation, not to the
third-party material listed below.

This notice was verified against the upstream sources on **2026-10-31**. The
upstream MicroDuck repository does **not** use one blanket license: its MJCF
robot descriptions are Apache-2.0, while its listed STL meshes are
CC BY-NC-SA 4.0.

## At-a-glance inventory

| Component | Local path | Upstream source | License |
|---|---|---|---|
| MicroDuck MJCF robot descriptions | `services/api/app/simulator/microduck/robot_groundcontact.xml`, `robot_walk.xml` | [`pollen-robotics/microduck_rl`](https://github.com/pollen-robotics/microduck_rl), `src/mjlab_microduck/robot/microduck/` | Apache-2.0 |
| MicroDuck MJCF scene files | `services/api/app/simulator/microduck/scene.xml`, `scene_walk.xml` | [`pollen-robotics/microduck_rl`](https://github.com/pollen-robotics/microduck_rl), `src/mjlab_microduck/robot/microduck/` | Apache-2.0 |
| MicroDuck 3D mesh assets | `services/api/app/simulator/microduck/assets/*.stl` (38 files) | [`pollen-robotics/microduck_rl`](https://github.com/pollen-robotics/microduck_rl), `src/mjlab_microduck/robot/microduck/assets/*.stl` | CC BY-NC-SA 4.0 |
| Upstream mesh license notice | `services/api/app/simulator/microduck/LICENSE-MODELS.md` | Same path in `microduck_rl` | Notice file (not a separate work license) |
| Upstream Apache license text | `services/api/app/simulator/microduck/LICENSE` | Same path in `microduck_rl` | Apache-2.0 text |
| MicroDuck walking policy | `services/api/app/simulator/microduck/velstand.onnx` | [`pollen-robotics/microduck-policies`](https://huggingface.co/pollen-robotics/microduck-policies) | Apache-2.0 |
| MuJoCo physics engine | Python package `mujoco` 3.17.0 | [`google-deepmind/mujoco`](https://github.com/google-deepmind/mujoco) | Apache-2.0 |
| ONNX Runtime | Python package `onnxruntime` 1.31.1 | [`microsoft/onnxruntime`](https://github.com/microsoft/onnxruntime) | MIT |

The installed Python and JavaScript dependency trees contain additional
packages under their own licenses. Their exact versions are recorded in
`services/api/uv.lock` and `apps/web/package-lock.json`; each installed
package retains its own license and notice files.

## 1. Pollen Robotics MicroDuck model

### 1.1 MJCF robot descriptions and scenes — Apache-2.0

The following vendored files are part of Pollen Robotics' MicroDuck MJCF
description:

- `robot_groundcontact.xml`
- `robot_walk.xml`
- `scene.xml`
- `scene_walk.xml`

Upstream location:
`https://github.com/pollen-robotics/microduck_rl/tree/bae39f5900b318f7e87fc485c30a31213cfa14eb/src/mjlab_microduck/robot/microduck`

Copyright Pollen Robotics. These files are licensed under the **Apache
License, Version 2.0**:

- Upstream license: <https://github.com/pollen-robotics/microduck_rl/blob/bae39f5900b318f7e87fc485c30a31213cfa14eb/LICENSE>
- Local copy of the Apache-2.0 text: [`services/api/app/simulator/microduck/LICENSE`](services/api/app/simulator/microduck/LICENSE)
- Apache-2.0 canonical text: <https://www.apache.org/licenses/LICENSE-2.0>

The upstream `LICENSE-MODELS.md` states that everything in the repository
other than the listed STL files — including the MJCF robot descriptions
(`.xml`), export configs, and Onshape part metadata — is Apache-2.0. The XML
license does not relicense the STL meshes that the XML files reference: those
meshes remain CC BY-NC-SA 4.0 as described below.

**Modifications:** the four vendored XML files are byte-identical copies of
the upstream files at commit `bae39f5900b318f7e87fc485c30a31213cfa14eb`.
HeyWalk does not edit them in place. At runtime, `model.compose_scene_xml()`
creates a stand-alone scene by adding a ground plane, HeyWalk environment
bodies, and the upstream keyframes around the unchanged robot subtree. That
generated scene is a derivative of the Apache-2.0 MJCF and keeps an
attribution header naming the upstream repository and commit. The robot
subtree, joints, inertias, actuators, sensors, and contact classes are not
changed. HeyWalk's original additions remain subject to HeyWalk's MIT
license; the upstream Apache-2.0 material is not relicensed.

### 1.2 STL mesh assets — CC BY-NC-SA 4.0

The 38 vendored STL meshes under
`services/api/app/simulator/microduck/assets/` are licensed under the
**Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International
License (CC BY-NC-SA 4.0)**.

Copyright Pollen Robotics.

- License summary: <https://creativecommons.org/licenses/by-nc-sa/4.0/>
- Full legal code: <https://creativecommons.org/licenses/by-nc-sa/4.0/legalcode>
- Upstream notice (byte-identical local copy):
  [`LICENSE-MODELS.md`](services/api/app/simulator/microduck/LICENSE-MODELS.md)
- Upstream source:
  <https://github.com/pollen-robotics/microduck_rl/tree/bae39f5900b318f7e87fc485c30a31213cfa14eb/src/mjlab_microduck/robot/microduck/assets>

All 38 files are within the upstream
`src/mjlab_microduck/robot/microduck/assets/*.stl` path that
`LICENSE-MODELS.md` explicitly covers. HeyWalk does not claim ownership of
these assets. A point-of-distribution notice is kept beside the files at
[`services/api/app/simulator/microduck/assets/README.md`](services/api/app/simulator/microduck/assets/README.md)
so the attribution and non-commercial terms travel with the meshes.

Some filenames reference third-party hardware components (for example
Raspberry Pi, Seeed Studio bearings, and Dynamixel XL330 servos). Those names
identify the modeled parts only. The CC BY-NC-SA 4.0 license above covers
Pollen Robotics' model files, not the hardware designs, vendor CAD files, or
trademarks of those manufacturers.

Vendored files:

```text
ankle_left.stl
ankle_right.stl
banana_pcb_locker.stl
bearing_roll.stl
bottom_head_shell.stl
elec_rpi_robot_hat_pcb.stl
face_part.stl
foot_left.stl
foot_right.stl
hip_l.stl
jaw_soft.stl
jaw.stl
left_shell.stl
leg.stl
lens.stl
m12_lens_holder.stl
motor_support.stl
neck_pitch.stl
neck.stl
noenoeil.stl
np_f970.stl
pcb__raspberry_pi_zero_2_w.stl
power_support.stl
right_shell.stl
seeed_bearing__configuration__22x16x4.stl
seeed_bearing__configuration_default.stl
soft_mouth_top.stl
sole_left.stl
sole_right.stl
speaker.stl
top_head_shell.stl
trunk_base.stl
upper_leg_left.stl
upper_leg_right.stl
upper_leg_rigidity_plate.stl
xl330.stl
yaw2roll.stl
yaw_roll_motion.stl
```

**Modifications:** all 38 files are byte-identical copies of the upstream
files at the pinned commit. HeyWalk neither modifies nor converts the meshes.
The browser loads the STL files directly, and the runner stages unchanged
copies into run workspaces.

**Attribution and ShareAlike:** when these meshes or an adaptation of them
(for example decimated or converted meshes) are shared, credit Pollen
Robotics, link to the upstream repository and to CC BY-NC-SA 4.0, state
whether changes were made, and license the adaptation under CC BY-NC-SA 4.0.
This is a summary; the linked legal code controls.

**Non-commercial restriction:** CC BY-NC-SA 4.0 includes a NonCommercial
condition. It does not grant commercial use or commercial redistribution
rights for the meshes. Commercial use or redistribution of a product that
includes these meshes should be reviewed with the rights holder, or the
meshes should be replaced with assets under suitable terms. This notice does
not grant any such permission.

**Maintaining this asset set:** treat `assets/` as a verbatim upstream copy.
If an STL is added, removed, converted, decimated, or edited, record the
upstream path and commit, describe the change, keep the CC BY-NC-SA notice
and attribution beside the files, update the list and count in this notice
and in `assets/README.md`, and re-run the byte comparison. Do not add STLs
from another source unless their license is compatible and recorded. Do not
use a modified mesh commercially without permission.

**Not vendored:** upstream contains 43 STL files in this asset directory.
HeyWalk vendors 38. The five upstream meshes not included are
`ankle_l_v1.stl`, `ankle_r_v1.stl`, `rim.stl`, `roller_blade.stl`, and
`tire.stl`. The upstream `xl330_test_bench` meshes are also not vendored.

### 1.3 Upstream notice files

`LICENSE-MODELS.md` is a byte-identical copy of the upstream notice.
`LICENSE` is a byte-identical copy of the upstream Apache-2.0 license text,
including its Pollen Robotics copyright line. The upstream repository has no
separate `NOTICE` file at the pinned commit, so there is no upstream `NOTICE`
text to reproduce. The Apache-2.0 license and the mesh notice must be kept
with redistributed copies of the corresponding material.

## 2. MicroDuck walking policy — Apache-2.0

`velstand.onnx` is the upstream velocity/stand locomotion policy from the
Pollen Robotics policy repository:

- Source: <https://huggingface.co/pollen-robotics/microduck-policies>
- File: <https://huggingface.co/pollen-robotics/microduck-policies/blob/main/velstand.onnx>
- Revision inspected: `665ae4eb20bfe13bca1ef4ea6d4bb6b4a096e448`
- Declared license: **Apache-2.0** (model-card metadata:
  <https://huggingface.co/pollen-robotics/microduck-policies/raw/main/README.md>)
- Local SHA-256:
  `2fc7f8ee6acd7d0d3f66a99927b5d5a56589aeed02ec714de329b252d5ccf492`

Copyright Pollen Robotics. HeyWalk distributes the policy unchanged, runs it
through ONNX Runtime, and does not retrain or fine-tune it. No other policies
from that repository are bundled. `velstand.onnx` is not part of
`microduck_rl` and is not covered by that repository's Apache-2.0 license; its
license comes from the separate Hugging Face policy repository. That
repository declares Apache-2.0 in its model-card metadata and does not
contain a separate `LICENSE` file. The Apache-2.0 text is included locally at
[`services/api/app/simulator/microduck/LICENSE`](services/api/app/simulator/microduck/LICENSE).

## 3. Simulation and inference software

The versions below are the ones resolved in `services/api/uv.lock` when this
notice was prepared. The runner image installs compatible version ranges, so
a rebuilt image may contain a newer release under the same license.

| Component | Version inspected | License | Upstream |
|---|---|---|---|
| MuJoCo | 3.17.0 | Apache-2.0 | <https://github.com/google-deepmind/mujoco> |
| ONNX Runtime | 1.31.1 | MIT | <https://github.com/microsoft/onnxruntime> |

MuJoCo is distributed with its own `LICENSES_THIRD_PARTY.md`; ONNX Runtime is
distributed with its own `ThirdPartyNotices.txt`. Those files are included in
the installed Python packages and govern their respective bundled
third-party components. Container base images and operating-system packages
used by the Dockerfiles are distributed under their own licenses.

## 4. Separation of intellectual property

- **Original HeyWalk application code and documentation:** MIT, as stated in
  [`LICENSE`](LICENSE). This includes the application source, the integration
  layer, and HeyWalk-authored documentation.
- **MicroDuck MJCF robot descriptions and scenes:** Apache-2.0, copyright
  Pollen Robotics.
- **MicroDuck STL meshes:** CC BY-NC-SA 4.0, copyright Pollen Robotics.
- **MicroDuck walking policy:** Apache-2.0, copyright Pollen Robotics.
- **Generated composed MuJoCo scenes:** derived from the Apache-2.0 MicroDuck
  MJCF, with HeyWalk additions; the upstream attribution header is retained.
- **Hardware and manufacturing files:** none are included. The simulation
  assets grant no rights to manufacture MicroDuck hardware.

The names Pollen Robotics and MicroDuck, and any associated logos or
trademarks, belong to their respective owners. Their use here is for
attribution and identification only. Pollen Robotics does not endorse HeyWalk.

## 5. Verification record

The upstream repository was downloaded at commit
`bae39f5900b318f7e87fc485c30a31213cfa14eb`, and the vendored files were
compared byte-for-byte:

- All four vendored XML files match their upstream files exactly.
- All 38 vendored STL files match their upstream files exactly.
- The MJCF references exactly those 38 STLs: no referenced mesh is missing
  and no vendored mesh is unreferenced.
- The STL binary headers contain only color/material metadata, not separate
  author or copyright text. The upstream `LICENSE-MODELS.md` is the
  controlling notice for the meshes.
- `LICENSE-MODELS.md` matches the upstream notice exactly.
- `LICENSE` matches the upstream Apache-2.0 text exactly.
- `velstand.onnx` matches the current upstream Hugging Face file by SHA-256
  (`2fc7f8ee6acd7d0d3f66a99927b5d5a56589aeed02ec714de329b252d5ccf492`).

Upstream license statements inspected:

- <https://github.com/pollen-robotics/microduck_rl/blob/bae39f5900b318f7e87fc485c30a31213cfa14eb/LICENSE>
- <https://github.com/pollen-robotics/microduck_rl/blob/bae39f5900b318f7e87fc485c30a31213cfa14eb/LICENSE-MODELS.md>
- <https://github.com/pollen-robotics/microduck_rl/blob/bae39f5900b318f7e87fc485c30a31213cfa14eb/pyproject.toml> (`license = "Apache-2.0 AND CC-BY-NC-SA-4.0"`)
- <https://huggingface.co/pollen-robotics/microduck-policies/raw/main/README.md> (`license: apache-2.0`)

This notice is a good-faith compliance record, not legal advice. Before any
commercial distribution, obtain legal review and the necessary permission
from the rights holders, especially for the CC BY-NC-SA 4.0 meshes.
