# ED-209-inspired MuJoCo prototype

This is an approximate, image-derived articulated walker prototype. It is not an exact reconstruction of the original film prop and is not suitable for fabrication or real-world control without engineering validation.

## Coordinate convention
- +X: forward (weapon muzzles and toes)
- +Y: robot's left
- +Z: up
- Approximate standing height: about 2 m

## Contents
- `robot.xml`: MuJoCo model with free root, six leg joints, stiff position actuators, and a STAND keyframe.
- `model.py`: the registry entry point (metadata, scene composition, asset staging, 3D-view payloads).
- `assets/*.stl`: per-part triangle meshes used by the MJCF.
- `assets/*.obj`: editable interchange copies of the same per-part meshes.

## Simulator notes
- The composed scene stages the shared arena (floor, walls, crates, ball, flag from `simulator/scene.py`) around the robot; the bundled `robot.xml` carries only the robot subtree plus its STAND keyframe.
- Joint tests run on a lifted stand (`fix_base` + `fix_base_lift=0.30` in the catalog defaults) so the heavy legs swing free of floor friction and clear the shared arena crates.
- Collision proxies are massless (`density="0"`); the explicit mesh masses are each body's whole inertia.
- No walking policy exists for this morphology: locomotion runs stay MicroDuck-only.

## Load
```python
import mujoco
model = mujoco.MjModel.from_xml_path("robot.xml")
data = mujoco.MjData(model)
mujoco.mj_step(model, data)
```

## Important limitations
Geometry, scale, masses, inertias and joint limits are estimates from one perspective image. Weapon assemblies are decorative fixed meshes; no firing mechanism is modeled. Meshes are simplified block/cylinder approximations rather than a detailed scan. Collision geometry is intentionally simpler than visual geometry. Validate balance, contacts, inertias, actuator gains and joint limits before using this model for locomotion research.
