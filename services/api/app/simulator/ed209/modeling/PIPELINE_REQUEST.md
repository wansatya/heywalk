# ED-209 Mesh Rebuild — Pipeline Request

## Goal

Replace the 12 placeholder `ed209/assets/*.stl` meshes (boxes/cylinders,
276 KB total) with detailed meshes adapted from the licensed source set in
`/home/wan/werk/2026/museforge/ocp-ed209/` (CC BY-NC-SA 4.0, Al Mon "OCP
ED209" 1/10 figure; attribution files are added by the maintainer after the
build). Same 12 filenames, same body-local frames, same `robot.xml` —
only the STL bytes change (plus `.obj` interchange copies).

## Visual target

- `visual-plan/refs/reference.png`: the user's figure photo — **back
  three-quarter view (left) and front view (right)** of the assembled
  ED-209. Match proportions, paneling, vents, gun barrels, sensor canopy.
- Colors (user override — the reference is blue-gray, we ship RoboCop
  silver + black): silver armor `#B8BDC7`-ish metallic on torso, pods,
  legs, feet; near-black `#121316` on the neck canopy and cannons. The
  build renders in THESE colors so review judges what ships; the hue
  shift vs the reference is intentional.
- Reference renders in `modeling/parts/*.png` (one per source file).

## Source → target mapping (initial; PRD refines against visuals)

| Target (body frame) | Source | Notes | Tri budget |
|---|---|---|---|
| torso.stl | head_front (main 102K shell + 20K plate; DELETE 28 dust shells) + head_back + visor_s + hip_assembly (91K + 10K shells) + hip_rubber | hull + waist; largest part | 130K |
| neck_bellows.stl | TBD: black roof canopy, likely split from head_back top or clean-modeled to match reference | only part that may need fresh modeling | 15K |
| left/right_weapon_pod.stl | arms.stl shells (1 each, 246K → heavy!) + shoulder_armor shell + shoulders_v3 joint shells | cylindrical gun housings | 45K ea |
| left/right_cannons.stl | guns_v3.stl shells (91K triple-barrel + 59K twin-barrel — KEEP the film asymmetry, one design per side) | barrels point +X from pod origin | 25K ea |
| left/right_upper_leg.stl | l/r-leg_split shell 1 (19K thigh) | | 30K ea |
| left/right_lower_leg.stl | l/r-leg_split shell 2 (9.5K shin) | | 25K ea |
| left/right_foot.stl | l/r-leg_split shell 3 (8K foot) | | 15K ea |

Skipped sources (no MJCF bodies): rocket launchers, ammo boxes, radioactive
canisters, wing rods/covers, loose greeblies.

Total ≤ ~425K tris (~21 MB, MicroDuck parity for web delivery).

## Hard constraints (do not break)

1. **Frame**: every STL is authored in its body's MuJoCo frame, meters:
   +X forward, +Y robot-left, +Z up. Blender default faces -Y — the build
   script must author/export in the MuJoCo frame (model with X-forward).
2. **Fit envelopes** (body-local meters, from collision proxies + current
   meshes — stay inside unless noted):
   - torso: x[-0.39,+0.43] y[-0.48,+0.48] z[-0.42,+0.43]
   - neck: x/y ±0.25, z[-0.11,+0.46]
   - pods: x[-0.48,+0.36] y ±0.20 z[-0.35,+0.26]
   - cannons: x[+0.06,+0.59] y ±0.13 z[-0.22,+0.17] (forward of pod origin)
   - upper legs: x[-0.19,+0.26] y ±0.15 z[-0.52,+0.12]
   - lower legs: x[-0.18,+0.24] y ±0.15 z[-0.54,+0.04]
   - feet: x[-0.20,+0.68] y ±0.24 z[-0.26,+0.06]
   - Source parts are millimeters in PRINT orientation (all min-z = 0):
     reorient each to assembled pose, then uniform-scale to fit.
     Uniform scale per part (no non-uniform stretch).
   - If a part's natural scaled size differs >30% from its envelope, resize
     that part's COLLISION PROXY in robot.xml to match (torso/pods/cannons
     only — never legs/feet, whose clearances are tuned) and flag it loudly.
3. **Filenames**: exact 12 names in `assets/`; binary STL, correct tri count
   in header bytes 80:84. Also write matching `.obj` copies (not served,
   interchange only).
4. **robot.xml otherwise untouched** (joints, masses, keyframe). Maintainer
   applies silver/black `rgba` + runs `pytest tests/test_ed209.py` after.
5. **Build script**: one idempotent `modeling/build_ed209.py` —
   `blender -b --factory-startup --python-exit-code 1 -P build_ed209.py`
   reproduces STLs + OBJs + proof renders + `.blend` from scratch.
   Blender 5.2, EEVEE id `BLENDER_EEVEE`, numpy available, no GPU,
   OCIO config broken (renders still work; ignore that error).

## Deliverable proof

- `modeling/render_front34.png` (1200px, silver/black, white studio bg):
  full robot at STAND, front three-quarter, matching the reference's right
  figure. Plus `render_back34.png` for the left figure.
- Assembled-pose check: parts posed at body offsets must form the coherent
  figure (no floats/gaps) — the renders prove it.
