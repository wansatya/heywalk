# ED-209 simulator mesh rebuild: visual PRD

## 1. Goal and visual pillars

Rebuild the 12 placeholder ED-209 simulator meshes as the realistic OCP ED-209
figure in the reference photo, adapted from the licensed source set in
`ocp-ed209/` (CC BY-NC-SA 4.0), and ship them in RoboCop silver armor + black
mechanical colors (a deliberate user override of the reference's blue-gray
tint). Deliverable: 12 binary STLs in `ed209/assets/` plus `.obj` copies, proof
renders, and one idempotent `modeling/build_ed209.py` that reproduces
STLs + OBJs + proof renders + `.blend` from scratch. `robot.xml` stays
untouched; the figure must read as the same robot as the reference photo at
first glance, only silver/black instead of blue-gray.

Pillars:
- Film-faithful ED-209 silhouette: wide hunched torso hull, big cylindrical
  side gun pods, digitigrade legs on flat pad feet, triple/twin cannon
  asymmetry kept.
- RoboCop silver armor + black mechanics: bright silver shell panels over
  near-black joints, canopy, and gun faces; tiny red marking accents only.
- MicroDuck-grade figure density: crisp panel lines, vents, bolts, and cable
  bundles, total ≤ ~425K tris for web delivery.

## 2. Reference index

| File | Shows | Authoritative for | Source |
|---|---|---|---|
| `visual-plan/refs/reference.png` (left figure) | back three-quarter view of assembled figure | rear silhouette, back panel layout, leg backs, feet, pod backs; wins all back-view conflicts | user figure photo |
| `visual-plan/refs/reference.png` (right figure) | front view of assembled figure | front silhouette, black roof canopy, front grille band, gun pod faces, cannon muzzles, leg fronts; wins all front-view conflicts | user figure photo |
| `modeling/parts/head_front.png` | head_front source shells (102K main shell + 20K plate + 28 dust shells) | torso upper hull paneling after dust-shell deletion | licensed source part render |
| `modeling/parts/arms.png` | arms.stl twin cylindrical weapon-pod shells | weapon pod housings (one shell per side) | licensed source part render |
| `modeling/parts/guns_v3.png` | guns_v3.stl triple-barrel + twin-barrel clusters | cannon designs incl. film asymmetry (one design per side) | licensed source part render |
| `modeling/parts/hip_assembly.png` | hip_assembly shells (91K + 10K) | torso waist / lower hull forms | licensed source part render |
| `modeling/parts/l-leg_split.png` | l-leg_split thigh + shin + foot shells | upper leg, lower leg, foot shapes (r-leg_split mirrors) | licensed source part render |
| `PIPELINE_REQUEST.md` | source→target mapping, tri budgets, fit envelopes, frame/filename rules | every number, name, and hard constraint; wins all conflicts on spec | build specification |

Assumptions for parts no reference shows:
- Neck bellows (black roof canopy underside / neck joint) is barely visible in
  either view; model as a matte-black ribbed canopy block filling the torso
  roof opening, matching the right figure's black roof pad outline.
- Pod rear caps mirror pod front cap construction (domed cap + dark inset
  panel) as seen on the left figure's pod backs.
- Gun muzzle interior is plain dark bore; no emissive.
- Underside of torso/feet: flat dark panels with bolt rows; never hero-visible.

## 3. Global look

- Style: clean realistic PBR figure render, sharp hard-surface paneling with
  fine vents, bolts, seams; light battle-worn edge wear only (no heavy rust or
  dirt). No texture noise.
- Palette (SHIP colors — silver/black override the reference blue-gray):
  - Silver armor `#B8BDC7`, metallic 0.9, roughness 0.35 — torso hull, pod
    shells, shoulder armor, legs, feet tops.
  - Near-black mechanics `#121316`, metallic 0.2, roughness 0.6 — joints,
    cannon barrels/muzzles, rear vents, foot soles, waist gaps.
  - Canopy matte black `#0E0F11`, metallic 0.0, roughness 0.9 — neck bellows /
    roof pad (the big black pad on the right figure's torso top).
  - Accent red `#B01E24` (small rectangular markings on torso top/rear and leg
    plates), cable yellow `#C9A227` + cable red `#A02323` (short exposed wire
    bundles at pod shoulder joints, both figures).
  - Reference sampling note: the photo's armor samples ~`#C3CCD4` lit /
    ~`#8E9AA5` shadowed (true base ~`#AEB9C2`); we intentionally shift brighter
    and neutral to `#B8BDC7` silver. Canopy samples ~`#151619` — matches ship
    black.
- Lighting: large soft key from front-left-above (~45° azimuth, ~50°
  elevation); low fill from right; faint rim from behind; white world ~0.6
  strength; soft contact shadow directly under the feet, darkest at the pads.
- Camera (proof views): front three-quarter for `render_front34.png` matching
  the reference's right figure; rear three-quarter from the opposite side for
  `render_back34.png` matching the reference's left figure; ~20–25° above robot
  center, normal lens (~50 mm feel); robot fills ~70% of frame width, centered,
  fully in frame with margin.
- Background / ground: plain white `#FFFFFF`, seamless; shadow-catcher floor.
- Render: Blender 5.2, EEVEE id `BLENDER_EEVEE`, 1200 px wide, white studio
  background; OCIO config error ignored. CPU only, no GPU.

## 4. Objects

Base unit: overall robot height H = 1.0 (≈ 0.9 m at STAND). Measured ratios
from `reference.png` (right figure ~735 px tall, ~760 px wide incl. pods):
total width ≈ 1.03 H; torso hull width ≈ 0.65 H; pod outer Ø ≈ 0.27 H, pod
length ≈ 0.34 H; black roof pad ≈ 0.45 H wide × 0.30 H deep; foot pad
≈ 0.22 H wide. All parts modeled in body-local MuJoCo frame: +X forward,
+Y robot-left, +Z up, meters, uniform scale only.

### 4.1 torso (refs: reference.png left + right, head_front.png, hip_assembly.png)

Shape: wide hunched hull, 0.65 H wide × 0.40 H tall × 0.45 H deep; flat armored
rear plate with center raised spine panel and red tick marks; front face has a
recessed dark grille band under the canopy lip; waist narrows to the hip drum
below.

Parts:

| Part | Count | Size (in H) | Position | Material | Notes |
|---|---|---|---|---|---|
| upper hull shell | 1 | 0.65 × 0.45 × 0.28 | torso origin, x centered | silver armor | head_front main 102K shell + 20K plate; DELETE 28 dust shells; rear panel lines, bolt rows, 2 red top ticks `#B01E24` |
| rear plate detail | 1 | 0.50 × 0.30 × 0.03 | rear face (+X behind? rear = −X), flush | silver armor | head_back shell; center spine, vents left/right as in left figure |
| front grille band | 1 | 0.40 × 0.10 × 0.02 | front face (+X), below canopy lip | near-black | visor_s shell; horizontal slats per right figure |
| waist / hip drum | 1 | 0.40 × 0.30 × 0.25 | below hull, z −0.25 H | silver armor + black gaps | hip_assembly 91K + 10K shells + hip_rubber bellows rings visible in leg gap |

Details: panel seams ~0.004 H wide; bolt heads Ø0.006 H in rows along plate
edges; rear vents: 2 banks of 5 vertical slats each side (left figure); small
red rectangle decals on top shoulders; light edge wear on plate borders only.
Tri budget: 130K. Fit envelope: x[−0.39,+0.43] y[−0.48,+0.48] z[−0.42,+0.43].

### 4.2 neck_bellows (refs: reference.png right)

Shape: matte-black ribbed roof pad, 0.45 H wide × 0.30 H deep × 0.08 H thick,
slightly domed, filling the torso roof opening; front lip overhangs the grille
band. Only part allowed fresh modeling (or a clean split from head_back top).

Parts:

| Part | Count | Size (in H) | Position | Material | Notes |
|---|---|---|---|---|---|
| canopy pad | 1 | 0.45 × 0.30 × 0.08 | torso top, z +0.30 H | canopy matte black | fine fabric/rib normal detail; straight front edge, rounded rear corners per right figure |

Tri budget: 15K. Fit envelope: x/y ±0.25, z[−0.11,+0.46].

### 4.3 left_weapon_pod / right_weapon_pod (refs: reference.png left + right, arms.png)

Shape: cylindrical gun housing, axis fore-aft, Ø 0.27 H × 0.34 H long; domed
front cap with dark inset muzzle plate; rear cap domed with dark inset panel
(left figure pod backs); side sensor box + short red/yellow cable bundle at the
shoulder joint (both figures); top shoulder armor plate overlapping the pod.

Parts (per side):

| Part | Count | Size (in H) | Position | Material | Notes |
|---|---|---|---|---|---|
| pod shell | 1 | Ø0.27 × 0.34 long | pod origin, axis along X | silver armor | arms.stl one shell per side (246K for 2 → decimate to budget); rectangular side inset panel per arms render |
| shoulder armor plate | 1 | 0.20 × 0.12 × 0.02 | top of pod joint | silver armor | shoulder_armor shell + shoulders_v3 joint shells |
| cable bundle | 3 cables | Ø0.015 × 0.12 long | pod inner-top shoulder joint | cable yellow/red + black | exposed U-loop wires as in both figures |

Details: front cap ring seam; 4–6 bolt heads around cap rim; small dark sensor
notch on cap top (right figure). Tri budget: 45K each. Fit envelope (each):
x[−0.48,+0.36] y ±0.20 z[−0.35,+0.26].

### 4.4 left_cannons / right_cannons (refs: reference.png right, guns_v3.png)

Shape: gun cluster protruding forward (+X) from pod front cap; KEEP film
asymmetry — one side triple-barrel, other side twin-barrel, exactly as
guns_v3.stl provides (91K triple + 59K twin); barrels Ø0.035 H, length 0.20 H;
muzzle face dark with bore holes; barrels near-black.

Parts (per side):

| Part | Count | Size (in H) | Position | Material | Notes |
|---|---|---|---|---|---|
| barrel cluster | 1 (3 or 2 barrels) | 0.12 × 0.12 face, 0.20 long | pod front, barrels along +X from pod origin | near-black | triple on one side, twin on the other; stepped barrel shrouds + muzzle rings per guns render |
| muzzle plate | 1 | Ø0.10 × 0.02 | barrel base | near-black | dark inset disc on pod cap |

Details: muzzle bores Ø0.018 H recessed; thin status tick (red) beside one
muzzle per right figure. Tri budget: 25K each. Barrels point +X from pod
origin. Fit envelope (each): x[+0.06,+0.59] y ±0.13 z[−0.22,+0.17].

### 4.5 left_upper_leg / right_upper_leg (refs: reference.png left + right, l-leg_split.png)

Shape: armored thigh, tapered box-column 0.16 H wide × 0.30 H long, slight
forward cant; front shin-guard plate over twin actuator cylinders; side pivot
disc at hip.

Parts (per side):

| Part | Count | Size (in H) | Position | Material | Notes |
|---|---|---|---|---|---|
| thigh shell | 1 | 0.16 × 0.20 × 0.30 | upper leg origin | silver armor | leg_split shell 1 (19K); front guard plate + side disc |
| actuator cylinders | 2 | Ø0.05 × 0.22 | rear of thigh | near-black | partially exposed behind guard |

Tri budget: 30K each. Fit envelope (each): x[−0.19,+0.26] y ±0.15
z[−0.52,+0.12]. Never resize collision proxies for legs.

### 4.6 left_lower_leg / right_lower_leg (refs: reference.png left + right, l-leg_split.png)

Shape: slim shin column 0.13 H wide × 0.28 H long continuing the thigh cant;
front ladder-vent strip; ankle hinge barrel at bottom.

Parts (per side):

| Part | Count | Size (in H) | Position | Material | Notes |
|---|---|---|---|---|---|
| shin shell | 1 | 0.13 × 0.16 × 0.28 | lower leg origin | silver armor | leg_split shell 2 (9.5K) |
| front vent strip | 1 | 0.06 × 0.01 × 0.16 | shin front (+X) | near-black | 5-step ladder slats per left figure shin backs/fronts |

Tri budget: 25K each. Fit envelope (each): x[−0.18,+0.24] y ±0.15
z[−0.54,+0.04]. Never resize collision proxies for legs.

### 4.7 left_foot / right_foot (refs: reference.png left + right, l-leg_split.png)

Shape: flat wide pad foot 0.22 H wide × 0.30 H long × 0.10 H tall; pointed
front toe cap; dark sole tread; ankle barrel socket on top center.

Parts (per side):

| Part | Count | Size (in H) | Position | Material | Notes |
|---|---|---|---|---|---|
| pad shell | 1 | 0.22 × 0.30 × 0.10 | foot origin, sole at envelope bottom | silver armor top | leg_split shell 3 (8K); toe cap seam + 4 top bolts |
| sole tread | 1 | 0.20 × 0.28 × 0.02 | bottom | near-black | tread grooves |

Tri budget: 15K each. Fit envelope (each): x[−0.20,+0.68] y ±0.24
z[−0.26,+0.06]. Never resize collision proxies for feet.

Skipped sources (no MuJoCo bodies, never built): rocket launchers, ammo boxes,
radioactive canisters, wing rods/covers, loose greeblies, ammo_box_greeblies,
hip_greblies, heat shields.

## 6. Technical constraints

- Tool: Blender 5.2 bpy, EEVEE (`BLENDER_EEVEE`), numpy available, no GPU.
- Frames: every STL authored in its body-local MuJoCo frame, meters (+X
  forward, +Y robot-left, +Z up). Blender default faces −Y — the build script
  authors/exports in the MuJoCo frame (model X-forward). Source parts arrive in
  mm in PRINT orientation (all min-z = 0): reorient each to assembled pose,
  then uniform-scale to fit. Uniform scale per part, no non-uniform stretch.
- Filenames: exact 12 names in `ed209/assets/`: `torso.stl`,
  `neck_bellows.stl`, `left_weapon_pod.stl`, `right_weapon_pod.stl`,
  `left_cannons.stl`, `right_cannons.stl`, `left_upper_leg.stl`,
  `right_upper_leg.stl`, `left_lower_leg.stl`, `right_lower_leg.stl`,
  `left_foot.stl`, `right_foot.stl`. Binary STL with correct tri count in
  header bytes 80:84. Matching `.obj` interchange copies alongside.
- Tri budgets: torso 130K; neck 15K; pods 45K ea; cannons 25K ea; upper legs
  30K ea; lower legs 25K ea; feet 15K ea. Total ≤ ~425K tris (~21 MB).
- Fit envelopes (body-local meters; torso/pods/cannons collision proxies may
  be resized only if natural scaled size differs >30% and flagged loudly;
  legs/feet proxies never): torso x[−0.39,+0.43] y[−0.48,+0.48] z[−0.42,+0.43];
  neck x/y ±0.25 z[−0.11,+0.46]; pods x[−0.48,+0.36] y ±0.20 z[−0.35,+0.26];
  cannons x[+0.06,+0.59] y ±0.13 z[−0.22,+0.17]; upper legs x[−0.19,+0.26]
  y ±0.15 z[−0.52,+0.12]; lower legs x[−0.18,+0.24] y ±0.15 z[−0.54,+0.04];
  feet x[−0.20,+0.68] y ±0.24 z[−0.26,+0.06].
- `robot.xml` otherwise untouched (joints, masses, keyframe).
- Build script: one idempotent `modeling/build_ed209.py` run as
  `blender -b --factory-startup --python-exit-code 1 -P build_ed209.py`
  reproduces STLs + OBJs + proof renders + `.blend` from scratch.
- Required outputs: 12 STLs + 12 OBJs in `ed209/assets/`,
  `modeling/render_front34.png` (1200 px, silver/black, white studio, full
  robot at STAND, front three-quarter matching the reference's right figure),
  `modeling/render_back34.png` (same, rear three-quarter matching the
  reference's left figure), `.blend` scene.
- Performance: web delivery parity (~21 MB total); renders run CPU-only in
  minutes.

## 7. Work breakdown

1. Scene scaffold: white studio, shadow catcher, lighting rig, both proof
   cameras; gray massing volumes at envelope sizes to lock framing.
2. Torso: import head_front (delete 28 dust shells), head_back, visor_s,
   hip_assembly + hip_rubber; reorient, uniform-scale, pose; silver/black
   materials.
3. Neck bellows: split from head_back top or fresh-model ribbed black canopy
   pad to the right figure's roof outline.
4. Weapon pods: one arms.stl shell per side + shoulder_armor + shoulders_v3
   joints + cable bundles; decimate pair from 246K to 90K.
5. Cannons: guns_v3 triple on one side, twin on the other, +X from pod
   origins, near-black.
6. Legs + feet: leg_split shells per side (thigh / shin + vent / pad + sole),
   posed to STAND without gaps.
7. Materials + colors (silver `#B8BDC7` / black `#121316` / canopy `#0E0F11` /
   red `#B01E24` ticks), assembled-pose gap check, proof renders, STL header
   tri-count verification.

## 8. Acceptance criteria

- AC-01 (reference.png right): front proof render matches front-figure
  silhouette — wide hull 0.65 H, pods Ø0.27 H flanking, legs splayed, no
  floats or gaps at pod shoulders, waist, hips, ankles.
- AC-02 (reference.png left): back proof render matches back-figure layout —
  rear spine plate, side rear vents, pod rear caps, shin-back and foot
  shapes all present.
- AC-03 (reference.png right): black roof canopy pad reads matte black
  `#0E0F11`, spans ~0.45 H of torso top with the reference's straight-front /
  rounded-rear outline.
- AC-04 (reference.png right): front grille band of horizontal slats sits
  under the canopy lip, dark against the silver hull.
- AC-05 (reference.png both): armor reads silver `#B8BDC7` metallic under
  scene light (not blue, not gray primer); joints, cannon barrels, and soles
  read near-black `#121316`.
- AC-06 (reference.png right): cannon asymmetry visible — one pod triple
  muzzle, the other twin; all barrels point +X.
- AC-07 (reference.png both): pod shoulder cable bundles (red/yellow) and red
  tick decals present but small (<2% of frame area each).
- AC-08 (PIPELINE_REQUEST.md): exact 12 filenames, binary STL, header tri
  counts correct, total ≤ ~425K; per-part budgets met.
- AC-09 (PIPELINE_REQUEST.md): every STL inside its fit envelope in the
  body-local MuJoCo frame (+X fwd, +Y left, +Z up, meters); `robot.xml`
  untouched.
- AC-10 (PIPELINE_REQUEST.md): `build_ed209.py` rerun from scratch
  reproduces STLs + OBJs + both 1200 px proof renders + `.blend`.
- AC-final (reference.png): side by side with the reference photo, a viewer
  calls the renders the same ED-209 figure in RoboCop silver/black, with no
  major visible difference except the intended hue shift.

## Polish round 1

Independent review of round 1 (`visual-plan/reviews/accept-r1-shape.md`,
`accept-r1-look.md`: 21 major + 9 moderate) vs `reference.png`. Highest
impact first. Tone-compare numbers are directional only (dual-figure
reference vs single render). Two review notes are OVERRIDDEN and not
applied: (a) "tint armor blue-gray #B7C3C8" — ship stays RoboCop silver
#b8bdc7-ish per §3 user override; (b) "gray background #E3E3E3" — §3
specifies plain white seamless. Stance/leg-length notes are PARTIALLY
applicable: hip/ankle offsets (±0.29) are fixed by `robot.xml` (untouched),
so widening/splay/shortening are maxed inside the leg fit envelopes.

Framing / silhouette / missing parts:

- Camera straight-on (az −12°) vs front-3/4; subject 0.49 frame wide vs
  ~0.70 -> front cam az +22° (lit-flank side) el 16° dist 3.0, back cam
  az 158° el 16° dist 3.0 (`main`).
- Feet read as one fused center dome vs 2 separate flat pads -> ROOT
  CAUSE: the 12 body-local originals rendered at the scene origin as a
  half-buried ghost duplicate (its torso roof/grille poked 0.2-0.4 m out
  of the floor between the feet); `hide_render` on all originals after
  export (`build_assembly`). Feet additionally rebuilt: fork shells
  reposed as ankle housings (no flat pad source exists in the set),
  fresh split toe boxes + dark notch tongue + full-length soles form
  the pads (`build_leg` foot).
- Cannon barrels small nubs vs protruding triple/twin clusters ~1 pod
  radius -> S_GUN 0.0080, barrels based at new cap face +0.28, full 25K
  budget (bezel 8K + barrels 16K), centered on pod axis, crisper black
  (`build_cannon`; SWAP: left=triple, right=twin — reference front figure
  shows triple on robot-left).
- Front grille band invisible vs dark louvered band 0.40 wide -> flat
  black backing plate (front x=0.30, guaranteed proud — the visor shell
  proved concave-forward, burying visor-seated slats) + 6 louver slats
  flush on its face, z −0.08..+0.17 (`build_torso`).
- Inter-leg gap empty rectangle vs central hip mass -> hip drum up 1.2x,
  new black pelvis block (torso frame x/y ±0.14/0.19, z −0.40..−0.26)
  closing the upper gap (`build_torso`).
- Torso ~30% too narrow, pods ~50% too small in Ø (envelopes cap uniform
  growth) -> front hull/plate scale 0.0074 (back 0.0070, stepped plates),
  pods S_POD 0.0090 + POD_OUT 0.02 (Ø 0.35, env Ø 0.40); bolts/disc/armor/
  joint-block resized to match (`build_torso`, `build_pod`).
- Legs long/straight vs splayed squat stance -> thigh cant 2° + shin 1°
  more about hip pivot, feet shifted outboard; chunkier feet + pelvis fill
  shorten the read (offsets fixed, cant maxed by y ±0.15 envs) (`build_leg`).
- Cowl thin slab vs thick rounded pad -> pad 0.10 thick, bevel 0.028,
  real bmesh subdivision + dome/ribs (`build_neck`).
- Gray torus cable loops vs red/yellow draped looms -> 3 bezier U-loop
  cables (yellow/black/red) arcing pod-top into hull (`build_pod`).
- Side hip cylinders poke outboard with no counterpart -> barrel depth
  0.29 -> 0.24, tucked (`build_leg`).

Color / material:

- Mirror chrome (metallic 0.9 / rough 0.35, blown streaks) vs satin
  silver -> metallic 0.55 / rough 0.55 + subtle noise roughness/color wear
  (`build_materials`).
- Joints/canopy/bores mid-gray (dark5 96 vs 22) vs near-black -> black
  metallic 0.0 / rough 0.8; canopy keeps 0.0/0.9 with real ribbed geometry
  (`build_materials`, `build_neck`).
- Red decals + colored cables missing -> pod-top red ticks added;
  torso ticks reseated (top pair was under the canopy -> y ±0.30, rear
  pair was drum-intercepted -> back-shell-only rays); cable mats
  yellow/red/black on the new looms (`build_torso`, `build_pod`).
- Melted-smooth hard surfaces vs crisp plates -> all fresh-modeled bits
  (ticks, slats, soles, bolts, discs, blocks, vents) flat-shaded; shells
  stay smooth (`shade_flat` + call sites).

Lighting / shadow / background:

- Flat-washed (contrast 35 vs 50, mean +23, bright5 +22.8) -> key
  170 -> 110 (size 3.2), fill 25 -> 10, rim 1.2 -> 1.8, ambient 0.10 ->
  0.06; GTAO attempted (`build_studio`).
- Small hard blob shadow vs broad soft shadow under/behind feet -> blobs
  recentered under actual feet, broadened (rx 0.48) + softened, torso cast
  widened (`build_blob_shadow`).

Detail:

- Vents/muzzles chrome mush vs dark recessed detail -> true-black barrels
  + full tri budgets + GTAO (`build_cannons`, `build_studio`).
- Toe/ankle/shin merged vs distinct parts -> split toe caps, recessed
  dark tongue, ankle barrel set back/up clear of the toes (`build_leg`).
