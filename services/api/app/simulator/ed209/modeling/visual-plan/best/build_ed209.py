# ED-209 simulator mesh rebuild — one idempotent build script.
# Run: blender -b --factory-startup --python-exit-code 1 -P build_ed209.py
# Reproduces: 12 STLs + 12 OBJs in ../assets/, render_front34.png,
# render_back34.png, ed209.blend (this scene file).
#
# Source: /home/wan/werk/2026/museforge/ocp-ed209/*.stl (CC BY-NC-SA 4.0).
# Every target mesh is authored in its body-local MuJoCo frame, meters:
# +X forward, +Y robot-left, +Z up. Uniform scale per part only.
#
# Frame note: source files are millimeters in PRINT orientation (min-z = 0).
# Print axes observed from orientation probes (see /tmp/orient/*.png):
#   X = width, Y = depth, Z = up. Per-part rotations below map print->assembled.

import bpy
import os
import sys
import struct
from math import pi, sin, cos, atan2, radians

# ---------------------------------------------------------------- config

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = "/home/wan/werk/2026/museforge/ocp-ed209"
ASSETS = os.path.normpath(os.path.join(HERE, "..", "assets"))
RENDER_FRONT = os.path.join(HERE, "render_front34.png")
RENDER_BACK = os.path.join(HERE, "render_back34.png")
BLEND = os.path.join(HERE, "ed209.blend")

# Fit envelopes, body-local meters: (xmin, xmax, ymin, ymax, zmin, zmax)
ENVELOPES = {
    "torso":           (-0.39, 0.43, -0.48, 0.48, -0.42, 0.43),
    "neck_bellows":    (-0.25, 0.25, -0.25, 0.25, -0.11, 0.46),
    "left_weapon_pod": (-0.48, 0.36, -0.20, 0.20, -0.35, 0.26),
    "right_weapon_pod": (-0.48, 0.36, -0.20, 0.20, -0.35, 0.26),
    "left_cannons":    (0.06, 0.59, -0.13, 0.13, -0.22, 0.17),
    "right_cannons":   (0.06, 0.59, -0.13, 0.13, -0.22, 0.17),
    "left_upper_leg":  (-0.19, 0.26, -0.15, 0.15, -0.52, 0.12),
    "right_upper_leg": (-0.19, 0.26, -0.15, 0.15, -0.52, 0.12),
    "left_lower_leg":  (-0.18, 0.24, -0.15, 0.15, -0.54, 0.04),
    "right_lower_leg": (-0.18, 0.24, -0.15, 0.15, -0.54, 0.04),
    "left_foot":       (-0.20, 0.68, -0.24, 0.24, -0.26, 0.06),
    "right_foot":      (-0.20, 0.68, -0.24, 0.24, -0.26, 0.06),
}

BUDGETS = {
    "torso": 130000, "neck_bellows": 15000,
    "left_weapon_pod": 45000, "right_weapon_pod": 45000,
    "left_cannons": 25000, "right_cannons": 25000,
    "left_upper_leg": 30000, "right_upper_leg": 30000,
    "left_lower_leg": 25000, "right_lower_leg": 25000,
    "left_foot": 15000, "right_foot": 15000,
}

# STAND body offsets (from robot.xml; torso at world (0,0,1.49)).
# NOTE: robot.xml mounts left_upper_leg at y=-0.29 and right at +0.29
# (legs side-swapped vs pods); honored exactly for the assembly renders.
TORSO_POS = (0.0, 0.0, 1.49)
BODY_OFFSETS = {
    "torso": (0.0, 0.0, 0.0),
    "neck_bellows": (-0.05, 0.0, 0.34),
    "left_weapon_pod": (0.04, 0.39, 0.34),
    "right_weapon_pod": (0.04, -0.39, 0.34),
    "left_cannons": (0.04, 0.39, 0.34),
    "right_cannons": (0.04, -0.39, 0.34),
    "left_upper_leg": (0.0, -0.29, -0.36),
    "right_upper_leg": (0.0, 0.29, -0.36),
    "left_lower_leg": (0.02, -0.29, -0.81),
    "right_lower_leg": (0.02, 0.29, -0.81),
    "left_foot": (0.04, -0.29, -1.29),
    "right_foot": (0.04, 0.29, -1.29),
}

# Orientation params (verified against probe renders; flips available).
POD_FLIP = +1      # +1: print-top cap -> +X (front). -1: reversed.
GUN_FLIP = 0       # 1: muzzle was -Z in print (adds pre-flip). per design.
FOOT_FLIP = {"left": 0, "right": 0}  # 1: rotate foot 180 about Z (notch dir).

COLORS = {
    "silver":  (0xB8 / 255, 0xBD / 255, 0xC7 / 255, 1.0),
    "black":   (0x12 / 255, 0x13 / 255, 0x16 / 255, 1.0),
    "canopy":  (0x0E / 255, 0x0F / 255, 0x11 / 255, 1.0),
    "red":     (0xB0 / 255, 0x1E / 255, 0x24 / 255, 1.0),
    "cable_y": (0xC9 / 255, 0xA2 / 255, 0x27 / 255, 1.0),
    "cable_r": (0xA0 / 255, 0x23 / 255, 0x23 / 255, 1.0),
}

# ---------------------------------------------------------------- utils

from mathutils import Matrix, Vector


def log(msg):
    print(f"[build_ed209] {msg}", flush=True)


def clear_scene():
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for coll in (bpy.data.meshes, bpy.data.materials, bpy.data.lights,
                 bpy.data.cameras, bpy.data.curves):
        for x in list(coll):
            try:
                coll.remove(x)
            except Exception:
                pass


def rot(axis, deg):
    return Matrix.Rotation(radians(deg), 4, axis)


def mat_scale(s):
    return Matrix.Scale(s, 4)


def mat_trans(t):
    return Matrix.Translation(Vector(t))


def import_shells(filename):
    """Import an STL, split into loose shells, sort by (-tris, minx, miny)."""
    path = os.path.join(SRC, filename)
    before = set(bpy.context.scene.objects)
    bpy.ops.wm.stl_import(filepath=path)
    imported = [o for o in bpy.context.scene.objects if o not in before
                and o.type == "MESH"]
    if not imported:
        raise RuntimeError(f"import failed: {path}")
    pre_sep = set(bpy.context.scene.objects)
    bpy.ops.object.select_all(action="DESELECT")
    for o in imported:
        o.select_set(True)
    bpy.context.view_layer.objects.active = imported[0]
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.separate(type="LOOSE")
    bpy.ops.object.mode_set(mode="OBJECT")
    parts = [o for o in bpy.context.scene.objects
             if o.type == "MESH" and (o not in pre_sep or o in imported)]
    bpy.ops.object.select_all(action="DESELECT")

    def key(o):
        bb = bbox(o)
        return (-tri_count(o), bb[0], bb[2])
    parts.sort(key=key)
    # Tag shells for deterministic reuse; drop from assembly automatically.
    for i, o in enumerate(parts):
        o.name = f"{filename}.shell{i}"
    return parts


def bbox(o):
    ws = [o.matrix_world @ v.co for v in o.data.vertices]
    xs = [v.x for v in ws]
    ys = [v.y for v in ws]
    zs = [v.z for v in ws]
    return (min(xs), max(xs), min(ys), max(ys), min(zs), max(zs))


def tri_count(o):
    return sum(len(p.vertices) - 2 for p in o.data.polygons)


def apply_matrix(o, m):
    o.matrix_world = m @ o.matrix_world
    bpy.ops.object.select_all(action="DESELECT")
    o.select_set(True)
    bpy.context.view_layer.objects.active = o
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    bpy.ops.object.select_all(action="DESELECT")


def place(o, m):
    """Set o.matrix_world to m exactly (o must be at identity)."""
    o.matrix_world = m


def center_to(o, target):
    bb = bbox(o)
    c = ((bb[0] + bb[1]) / 2, (bb[2] + bb[3]) / 2, (bb[4] + bb[5]) / 2)
    d = (target[0] - c[0], target[1] - c[1], target[2] - c[2])
    apply_matrix(o, mat_trans(d))


def verts_np(o):
    import numpy as np
    n = len(o.data.vertices)
    arr = np.empty(n * 3, dtype=np.float64)
    o.data.vertices.foreach_get("co", arr)
    return arr.reshape((n, 3))


def measure_lean(o):
    """Lean angle (deg) of a leg column in the Y-Z plane from +Z toward +Y."""
    import numpy as np
    v = verts_np(o)
    zmin, zmax = v[:, 2].min(), v[:, 2].max()
    lo = v[v[:, 2] < zmin + 15]
    hi = v[v[:, 2] > zmax - 15]
    dy = hi[:, 1].mean() - lo[:, 1].mean()
    dz = hi[:, 2].mean() - lo[:, 2].mean()
    return float(np.degrees(np.arctan2(dy, dz)))


def prong_spread_angle(o, zmax_band=10.0):
    """Direction of fork-prong spread in X-Y plane (deg from +X), via k=2
    clustering on low-band verts. Returns angle of prong-center axis."""
    import numpy as np
    v = verts_np(o)
    zmin = v[:, 2].min()
    sel = v[v[:, 2] < zmin + zmax_band][:, :2]
    # deterministic k-means, k=2, seeded at x extremes
    c0 = sel[np.argmin(sel[:, 0])]
    c1 = sel[np.argmax(sel[:, 0])]
    for _ in range(25):
        d0 = ((sel - c0) ** 2).sum(axis=1)
        d1 = ((sel - c1) ** 2).sum(axis=1)
        g0 = sel[d0 <= d1]
        g1 = sel[d0 > d1]
        if len(g0) == 0 or len(g1) == 0:
            break
        c0, c1 = g0.mean(axis=0), g1.mean(axis=0)
    return float(np.degrees(np.arctan2(c1[1] - c0[1], c1[0] - c0[0])))


def cut_horizontal(o, zcut, name_below, name_above):
    """Split mesh into below/above zcut (object-local coords at identity).
    Returns (below, above). Best-effort hole fill on both (hidden in assembly)."""
    import bmesh
    bpy.ops.object.select_all(action="DESELECT")
    o.select_set(True)
    bpy.context.view_layer.objects.active = o
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="DESELECT")
    bpy.ops.object.mode_set(mode="OBJECT")
    me = o.data
    for v in me.vertices:
        v.select = v.co.z < zcut
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.separate(type="SELECTED")
    bpy.ops.object.mode_set(mode="OBJECT")
    sep = bpy.context.selected_objects
    below = [x for x in sep if x != o]
    above = o
    if not below:
        raise RuntimeError(f"cut failed (empty below): {o.name} @ {zcut}")
    below = below[0]
    below.name = name_below
    above.name = name_above
    for part in (below, above):
        try:
            bm = bmesh.new()
            bm.from_mesh(part.data)
            bmesh.ops.holes_fill(bm, edges=bm.edges)
            bm.to_mesh(part.data)
            bm.free()
            part.data.update()
        except Exception as e:
            log(f"hole fill skipped for {part.name}: {e}")
    bpy.ops.object.select_all(action="DESELECT")
    return below, above


def decimate_to(o, target_tris, name=""):
    cur = tri_count(o)
    if cur <= target_tris:
        return cur
    ratio = max(0.01, min(0.999, target_tris / cur))
    mod = o.modifiers.new("decimate", "DECIMATE")
    mod.decimate_type = "COLLAPSE"
    mod.ratio = ratio
    try:
        bpy.ops.object.mode_set(mode="OBJECT")
    except Exception:
        pass
    bpy.ops.object.select_all(action="DESELECT")
    o.select_set(True)
    bpy.context.view_layer.objects.active = o
    try:
        bpy.ops.object.modifier_apply(modifier=mod.name)
    except Exception as e:
        log(f"decimate failed on {name or o.name}: {e}")
        o.modifiers.remove(mod)
        return cur
    new = tri_count(o)
    log(f"decimate {name or o.name}: {cur} -> {new} (target {target_tris})")
    return new


def shade_all_smooth(o):
    bpy.ops.object.select_all(action="DESELECT")
    o.select_set(True)
    bpy.context.view_layer.objects.active = o
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.shade_smooth()
    bpy.ops.object.select_all(action="DESELECT")


def ray_hit(objects, origin, direction):
    """Max-projection surface point along a ray over given objects (world).
    Returns world Vector or None. Used to seat decals/plates on hull."""
    deps = bpy.context.evaluated_depsgraph_get()
    best = None
    best_d = None
    try:
        for o in objects:
            eo = o.evaluated_get(deps)
            m = eo.matrix_world
            org = m.inverted() @ Vector(origin)
            d = (m.inverted().to_3x3() @ Vector(direction)).normalized()
            hit, loc, _n, _i = eo.ray_cast(org, d)
            if hit:
                w = m @ loc
                dd = (w - Vector(origin)).dot(Vector(direction))
                if dd > 0 and (best_d is None or dd < best_d):
                    best_d = dd
                    best = w
    except Exception as e:
        log(f"raycast unavailable: {e}")
        return None
    return best


def recalc_outside(o):
    bpy.context.view_layer.objects.active = o
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.normals_make_consistent(inside=False)
    bpy.ops.object.mode_set(mode="OBJECT")


def make_material(name, color, metallic, roughness):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    bsdf.inputs["Base Color"].default_value = color
    bsdf.inputs["Metallic"].default_value = metallic
    bsdf.inputs["Roughness"].default_value = roughness
    return mat


def assign_material(o, mat):
    if mat.name not in o.data.materials:
        o.data.materials.append(mat)
    idx = list(o.data.materials).index(mat)
    for p in o.data.polygons:
        p.material_index = idx


def join_parts(parts, name, active_idx=0):
    bpy.ops.object.select_all(action="DESELECT")
    for o in parts:
        o.select_set(True)
    bpy.context.view_layer.objects.active = parts[active_idx]
    bpy.ops.object.join()
    joined = bpy.context.view_layer.objects.active
    joined.name = name
    # Deduplicate material slots, remap polygon indices.
    mats = [m for m in joined.data.materials]
    seen = []
    remap = {}
    for i, m in enumerate(mats):
        key = m.name if m else ""
        if key not in [s for s in seen]:
            seen.append(key)
            remap[i] = len(seen) - 1
        else:
            remap[i] = seen.index(key)
    for p in joined.data.polygons:
        p.material_index = remap[p.material_index]
    uniq = []
    for key in seen:
        uniq.append(bpy.data.materials.get(key))
    joined.data.materials.clear()
    for m in uniq:
        joined.data.materials.append(m)
    return joined


def stl_write(o, path):
    bpy.ops.object.select_all(action="DESELECT")
    o.select_set(True)
    bpy.context.view_layer.objects.active = o
    bpy.ops.wm.stl_export(filepath=path, check_existing=False,
                          export_selected_objects=True, ascii_format=False,
                          apply_modifiers=True)


def obj_write(o, path):
    bpy.ops.object.select_all(action="DESELECT")
    o.select_set(True)
    bpy.context.view_layer.objects.active = o
    bpy.ops.wm.obj_export(filepath=path, check_existing=False,
                          export_selected_objects=True, apply_modifiers=True,
                          forward_axis="Y", up_axis="Z",
                          export_materials=True, export_material_groups=True,
                          export_object_groups=True)


def red_tick(w=0.030, h=0.008, t=0.012):
    bpy.ops.object.select_all(action="DESELECT")
    bpy.ops.mesh.primitive_cube_add(size=1.0)
    o = bpy.context.active_object
    o.scale = (w, h, t)
    bpy.ops.object.transform_apply(scale=True)
    return o


def cable_ring(radius, tube=0.008):
    bpy.ops.object.select_all(action="DESELECT")
    bpy.ops.mesh.primitive_torus_add(major_radius=radius, minor_radius=tube,
                                     major_segments=20, minor_segments=8)
    return bpy.context.active_object


# ---------------------------------------------------------------- materials

def build_materials():
    return {
        "silver": make_material("M_silver", COLORS["silver"], 0.9, 0.35),
        "black": make_material("M_black", COLORS["black"], 0.2, 0.6),
        "canopy": make_material("M_canopy", COLORS["canopy"], 0.0, 0.9),
        "red": make_material("M_red", COLORS["red"], 0.3, 0.5),
        "cable_y": make_material("M_cable_y", COLORS["cable_y"], 0.1, 0.6),
        "cable_r": make_material("M_cable_r", COLORS["cable_r"], 0.1, 0.6),
    }


# ---------------------------------------------------------------- torso
# head_front main shell: outer face -Y in print -> +X (Rz+90).
# head_back: detail face -Y in print -> -X (Rz-90). visor: face applique.

S_TORSO = 0.0068  # uniform figure scale for hull shells
CHEST_X = 0.28    # torso front face (pod caps +0.22 stay ahead of chest)
BACK_X = -0.30    # torso rear face (torso wider than deep, per reference)


def build_torso(M):
    front_all = import_shells("head_front.stl")
    back = import_shells("head_back.stl")[0]
    visor = import_shells("visor_s.stl")[0]
    hip = import_shells("hip_assembly.stl")
    rubber = import_shells("hip_rubber.stl")
    # head_front shells: [main 102K, plate 20K]
    front, plate = front_all[0], front_all[1]
    drum, waistbox, hipcyl = hip[0], hip[1], hip[2]
    ring_l, ring_r = rubber[0], rubber[1]

    # --- front hull shell: Rz(+90), face front at CHEST_X. Bottom at
    # -0.20; roof lands ~+0.35.
    apply_matrix(front, rot("Z", 90) @ mat_scale(S_TORSO))
    bb = bbox(front)
    log(f"front shell size: dx={bb[1]-bb[0]:.3f} dy={bb[3]-bb[2]:.3f} "
        f"dz={bb[5]-bb[4]:.3f}")
    apply_matrix(front, mat_trans((CHEST_X - bb[1], -(bb[2] + bb[3]) / 2,
                                   -0.20 - bb[4])))
    decimate_to(front, 30000, "torso.front")
    assign_material(front, M["silver"])

    # --- rear hull shell: Rz(-90), rear face at BACK_X.
    apply_matrix(back, rot("Z", -90) @ mat_scale(S_TORSO))
    bb = bbox(back)
    log(f"back shell size: dx={bb[1]-bb[0]:.3f} dy={bb[3]-bb[2]:.3f} "
        f"dz={bb[5]-bb[4]:.3f}")
    apply_matrix(back, mat_trans((BACK_X - bb[0], -(bb[2] + bb[3]) / 2,
                                  -0.32 - bb[4])))
    decimate_to(back, 40000, "torso.back")
    assign_material(back, M["silver"])

    # --- front roof plate: flat applique, half-sunk into front roof slope
    apply_matrix(plate, rot("Z", 90) @ mat_scale(S_TORSO))
    bb = bbox(plate)
    log(f"torso plate size: dx={bb[1]-bb[0]:.3f} dy={bb[3]-bb[2]:.3f} "
        f"dz={bb[5]-bb[4]:.3f}")
    hit = ray_hit([front, back], (0.20, 0.0, 1.0), (0, 0, -1))
    pz = hit.z - 0.004 if hit else 0.31
    center_to(plate, (0.20, 0.0, pz))
    decimate_to(plate, 8000, "torso.plate")
    assign_material(plate, M["silver"])

    # --- visor: dark face/grille applique, front proud 5mm, top tucks
    # toward the canopy above
    apply_matrix(visor, rot("Z", 90) @ mat_scale(0.0036))
    bb = bbox(visor)
    log(f"visor size: dx={bb[1]-bb[0]:.3f} dy={bb[3]-bb[2]:.3f} "
        f"dz={bb[5]-bb[4]:.3f}")
    apply_matrix(visor, mat_trans((CHEST_X + 0.005 - bb[1],
                                   -(bb[2] + bb[3]) / 2, 0)))
    bb = bbox(visor)
    apply_matrix(visor, mat_trans((0, 0, 0.06 - (bb[4] + bb[5]) / 2)))
    decimate_to(visor, 15000, "torso.visor")
    assign_material(visor, M["black"])

    # --- grille slats: thin dark bars raycast-seated across the visor face
    slats = []
    for z in (-0.05, -0.01, 0.03, 0.07, 0.11, 0.15):
        hit = ray_hit([visor], (1.0, 0.0, z), (-1, 0, 0))
        s = red_tick(w=0.010, h=0.26, t=0.012)
        s.location = ((hit.x + 0.002) if hit else 0.412, 0.0, z)
        bpy.ops.object.transform_apply(location=True)
        assign_material(s, M["black"])
        slats.append(s)

    # --- hip drum: axis X -> Y, waist center
    apply_matrix(drum, rot("Z", 90) @ mat_scale(0.007))
    center_to(drum, (0.0, 0.0, -0.30))
    decimate_to(drum, 18000, "torso.drum")
    assign_material(drum, M["silver"])

    # --- waist box: sloped front belly plate
    apply_matrix(waistbox, rot("Z", 90) @ mat_scale(0.004))
    bb = bbox(waistbox)
    log(f"waistbox size: dx={bb[1]-bb[0]:.3f} dy={bb[3]-bb[2]:.3f} "
        f"dz={bb[5]-bb[4]:.3f}")
    center_to(waistbox, (0.16, 0.0, -0.22))
    decimate_to(waistbox, 6000, "torso.waistbox")
    assign_material(waistbox, M["silver"])

    # --- hip bellows rings: axis Z -> Y, at hips (torso frame)
    for i, ring in enumerate((ring_l, ring_r)):
        apply_matrix(ring, rot("X", 90) @ mat_scale(0.0040))
        y = 0.27 if i == 0 else -0.27
        center_to(ring, (0.0, y, -0.31))
        decimate_to(ring, 4000, f"torso.ring{i}")
        assign_material(ring, M["black"])

    # --- red tick decals (torso top + rear)
    ticks = []
    for (x, y) in ((-0.10, 0.18), (-0.10, -0.18)):
        hit = ray_hit([front, back], (x, y, 1.0), (0, 0, -1))
        t = red_tick(w=0.030, h=0.010, t=0.006)
        t.location = (x, y, (hit.z + 0.001) if hit else 0.36)
        bpy.ops.object.transform_apply(location=True)
        assign_material(t, M["red"])
        ticks.append(t)
    for (y, z) in ((0.15, 0.05), (-0.15, 0.05)):
        hit = ray_hit([front, back], (-1.0, y, z), (1, 0, 0))
        t = red_tick(w=0.008, h=0.030, t=0.012)
        t.location = ((hit.x - 0.002) if hit else BACK_X + 0.002, y, z)
        bpy.ops.object.transform_apply(location=True)
        assign_material(t, M["red"])
        ticks.append(t)

    parts = ([front, plate, back, visor, drum, waistbox, ring_l, ring_r]
             + slats + ticks)
    for o in parts:
        shade_all_smooth(o)
    torso = join_parts(parts, "torso")
    log(f"torso bbox: {[round(v, 3) for v in bbox(torso)]} "
        f"tris={tri_count(torso)}")
    return torso, {"hipcyl": hipcyl}


# ---------------------------------------------------------------- neck (fresh ribbed canopy pad)

def build_neck(M):
    # Matte-black ribbed roof pad in NECK-local frame: wider (Y) than
    # long (X), per the reference roof outline.
    w, d, h = 0.34, 0.48, 0.075
    bpy.ops.object.select_all(action="DESELECT")
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0.03, 0.0, 0.055))
    pad = bpy.context.active_object
    pad.name = "neck_pad"
    pad.scale = (w, d, h)
    bpy.ops.object.transform_apply(location=True, rotation=False, scale=True)
    # Subdivide top for dome + ribs (single pass: ~1.5K tris).
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.subdivide(number_cuts=10)
    bpy.ops.object.mode_set(mode="OBJECT")
    import numpy as np
    me = pad.data
    n = len(me.vertices)
    co = np.empty(n * 3)
    me.vertices.foreach_get("co", co)
    co = co.reshape((n, 3))
    # Dome top verts + sine ribs along Y; straight front edge, rounded rear.
    top = co[:, 2] > 0.055 - 0.005
    idx = np.nonzero(top)[0]
    xn = np.clip((co[idx, 0] - 0.03) / (w / 2), -1, 1)
    yn = np.clip(co[idx, 1] / (d / 2), -1, 1)
    co[idx, 2] += 0.018 * (1 - xn ** 2) * (1 - yn ** 2)
    co[idx, 2] += 0.0035 * np.sin(co[idx, 1] / d * np.pi * 14)
    # Round rear corners (x-): pull y toward center for x < -0.05.
    ridx = np.nonzero(co[:, 0] < -0.05)[0]
    co[ridx, 1] = co[ridx, 1] * 0.82
    me.vertices.foreach_set("co", co.ravel())
    me.update()
    recalc_outside(pad)
    shade_all_smooth(pad)
    assign_material(pad, M["canopy"])
    pad.name = "neck_bellows"
    log(f"neck bbox: {[round(v, 3) for v in bbox(pad)]} tris={tri_count(pad)}")
    return pad


# ---------------------------------------------------------------- weapon pods
# arms shells: vertical cylinders in print (axis Z) -> axis X (Ry+90),
# print-top cap -> +X front (POD_FLIP=-1 reverses via Ry-90).

S_POD = 0.0070
POD_DROP = 0.11  # pod shells sit low in frame so pod tops stay below canopy
POD_OUT = 0.06   # pod shells shift outward to a dressed seam at hull sides


def build_pod(M, side, shell, armor):
    # side: +1 robot-left (mounts +Y), -1 robot-right (mounts -Y)
    if POD_FLIP > 0:
        apply_matrix(shell, rot("Y", 90) @ mat_scale(S_POD))
    else:
        apply_matrix(shell, rot("Y", -90) @ mat_scale(S_POD))
    center_to(shell, (0.0, side * POD_OUT, -POD_DROP))
    bb = bbox(shell)
    log(f"pod shell size: dx={bb[1]-bb[0]:.3f} dy={bb[3]-bb[2]:.3f} "
        f"dz={bb[5]-bb[4]:.3f}")
    decimate_to(shell, 38000, "pod.shell")
    assign_material(shell, M["silver"])

    # shoulder armor plate: vertical print -> sloped shield bridging the
    # pod top (outer) down to the hull side (inner). Tilt sign per side.
    inner = -side  # toward torso in pod-local Y
    apply_matrix(armor, rot("X", side * 35.0) @ rot("Y", 90)
                 @ mat_scale(0.0050))
    center_to(armor, (-0.05, inner * 0.03, 0.03 - POD_DROP))
    decimate_to(armor, 5000, "pod.armor")
    assign_material(armor, M["silver"])

    # cable coils draped over the inner-top shoulder (torus loops in the
    # Y-Z plane: visible as loops from front/back, anchored inside the pod)
    cables = []
    inner = -side  # toward torso in pod-local Y
    for i, (r, mat) in enumerate(((0.045, M["cable_y"]), (0.055, M["black"]),
                                  (0.065, M["cable_r"]))):
        c = cable_ring(r)
        c.rotation_euler = (0, radians(90), 0)
        bpy.ops.object.transform_apply(rotation=True)
        c.location = (-0.08, inner * 0.02, 0.095 + 0.010 * i - POD_DROP)
        bpy.ops.object.transform_apply(location=True)
        assign_material(c, mat)
        cables.append(c)

    # shoulder joint block bridging the pod inner-bottom to the hull side
    jb = red_tick(w=0.16, h=0.12, t=0.22)
    jb.location = (-0.08, inner * 0.04, -0.03 - POD_DROP)
    bpy.ops.object.transform_apply(location=True)
    assign_material(jb, M["black"])

    parts = [shell, armor, jb] + cables
    for o in parts:
        shade_all_smooth(o)
    pod = join_parts(parts, "weapon_pod")
    log(f"pod bbox: {[round(v, 3) for v in bbox(pod)]} tris={tri_count(pod)}")
    return pod


def build_pods(M):
    arms = import_shells("arms.stl")
    armor = import_shells("shoulder_armor.stl")
    log(f"arms shells: {len(arms)}, armor shells: {len(armor)}")
    left = build_pod(M, +1, arms[0], armor[0])
    left.name = "left_weapon_pod"
    right = build_pod(M, -1, arms[1], armor[1])
    right.name = "right_weapon_pod"
    return left, right


# ---------------------------------------------------------------- cannons
# guns_v3: barrels point +Z in print, row along X.
# M = Rz(-90) @ Rx(-90): barrels -> +X, row -> horizontal Y.
# Split at print-z=5: mount bezel (visible gunport ring on cap face) +
# barrel cluster (protrudes forward). Left=twin, right=triple (reference).

S_GUN = 0.0065


def build_cannon(M, shell, name):
    if GUN_FLIP:
        pre = rot("Y", 180)
    else:
        pre = Matrix.Identity(4)
    bezel, barrels = cut_horizontal(shell, 5.0, f"{name}_bezel",
                                    f"{name}_barrels")
    for o in (bezel, barrels):
        apply_matrix(o, rot("Z", -90) @ rot("X", -90) @ pre
                     @ mat_scale(S_GUN))
    bb = bbox(barrels)
    log(f"{name} barrels size: dx={bb[1]-bb[0]:.3f} dy={bb[3]-bb[2]:.3f} "
        f"dz={bb[5]-bb[4]:.3f}")
    # Bezel ring proud of the pod cap face (cap at x=+0.22; cannon
    # frame shares the pod origin): bezel front +0.225, barrels from +0.22.
    bb = bbox(bezel)
    apply_matrix(bezel, mat_trans((0.225 - bb[1], -(bb[2] + bb[3]) / 2,
                                   -(bb[4] + bb[5]) / 2 - POD_DROP)))
    bb = bbox(barrels)
    apply_matrix(barrels, mat_trans((0.22 - bb[0], -(bb[2] + bb[3]) / 2,
                                     -(bb[4] + bb[5]) / 2 - POD_DROP)))
    decimate_to(bezel, 4000, f"{name}.bezel")
    decimate_to(barrels, 12000, f"{name}.barrels")
    assign_material(bezel, M["black"])
    assign_material(barrels, M["black"])
    parts = [bezel, barrels]
    if name == "right_cannons":
        # thin red status tick sunk into the barrel-cluster top near a muzzle
        t = red_tick(w=0.030, h=0.006, t=0.006)
        bb = bbox(barrels)
        t.location = (bb[1] - 0.025, 0.0, bb[5] - 0.004)
        bpy.ops.object.transform_apply(location=True)
        assign_material(t, M["red"])
        parts.append(t)
    for o in parts:
        shade_all_smooth(o)
    gun = join_parts(parts, name)
    log(f"{name} bbox: {[round(v, 3) for v in bbox(gun)]} "
        f"tris={tri_count(gun)}")
    return gun


def build_cannons(M):
    guns = import_shells("guns_v3.stl")
    # sorted desc: [triple 91K, twin 59K]
    triple, twin = guns[0], guns[1]
    left = build_cannon(M, twin, "left_cannons")
    right = build_cannon(M, triple, "right_cannons")
    return left, right


# ---------------------------------------------------------------- legs + feet
# l/r-leg_split shells (sorted desc): [leg0 19K thigh+knee-fork, leg1 9.5K
# shin pistons, leg2 8K ankle spring]. leg0 leans ~27 deg in Y-Z (print
# tilt): measured per shell and straightened. leg0 is cut at print-z=35:
# column -> thigh, fork base -> foot pad (two-toed).
# shoulders_v3 shells: [disc, disc, block, block] -> knee caps + knee blocks.

S_THIGH = 0.0039
S_SHIN = 0.0070
S_FOOT = 0.0025
S_KNEE = 0.0060
S_KNEECAP = 0.0050
S_ANKLE = 0.0070
LEG_CUT_Z = 35.0


def measure_mouth(o):
    """Mouth direction (deg, print X-Y) of the fork U: from crown centroid
    (z 18..30 band) toward prong-tips centroid (z < zmin+8)."""
    import numpy as np
    v = verts_np(o)
    zmin = v[:, 2].min()
    tips = v[v[:, 2] < zmin + 8]
    crown = v[(v[:, 2] > zmin + 18) & (v[:, 2] < zmin + 30)]
    if len(tips) == 0 or len(crown) == 0:
        return 90.0
    d = tips[:, :2].mean(axis=0) - crown[:, :2].mean(axis=0)
    return float(np.degrees(np.arctan2(d[1], d[0])))


def top_centroid(o, band=0.030):
    import numpy as np
    v = verts_np(o)
    # verts are object-local; object must be at identity.
    sel = v[v[:, 2] > v[:, 2].max() - band]
    c = sel.mean(axis=0)
    return (float(c[0]), float(c[1]), float(c[2]))


def build_leg(M, tag, shells, block, cap, ankle, outer_sign, flip):
    # tag: "left" / "right" (target file side)
    thigh_full, pistons, spring = shells[0], shells[1], shells[2]
    lean = measure_lean(thigh_full)
    log(f"{tag} leg0 lean: {lean:.1f} deg")
    fork, column = cut_horizontal(thigh_full, LEG_CUT_Z, f"{tag}_fork",
                                  f"{tag}_column")
    mouth = measure_mouth(fork)
    log(f"{tag} fork mouth angle: {mouth:.1f} deg")
    lean_col = measure_lean(column)
    log(f"{tag} column lean: {lean_col:.1f} deg")

    # --- thigh: straighten (Rx +lean moves top toward -Y), Rz(90) maps
    # print-X -> Y (width), print-Y -> -X (depth).
    apply_matrix(column, rot("Z", 90) @ rot("X", lean_col)
                 @ mat_scale(S_THIGH))
    # hip knob (top) centered on pivot origin
    tc = top_centroid(column)
    apply_matrix(column, mat_trans((-tc[0], -tc[1], -tc[2])))
    bb = bbox(column)
    log(f"{tag} thigh size: dx={bb[1]-bb[0]:.3f} dy={bb[3]-bb[2]:.3f} "
        f"dz={bb[5]-bb[4]:.3f} top z={bb[5]:.3f}")
    assign_material(column, M["silver"])

    # --- knee block: long axis -> Y (hinge axis)
    apply_matrix(block, rot("Z", 90) @ mat_scale(S_KNEE))
    center_to(block, (0.0, 0.0, -0.398))
    decimate_to(block, 8000, f"{tag}.knee")
    assign_material(block, M["black"])

    # --- knee outer cap: pancake axis -> Y
    apply_matrix(cap, rot("X", 90) @ mat_scale(S_KNEECAP))
    center_to(cap, (0.0, outer_sign * 0.125, -0.398))
    decimate_to(cap, 1200, f"{tag}.kneecap")
    assign_material(cap, M["black"])

    # --- thigh red tick seated on the front face via raycast
    hit = ray_hit([column], (1.0, 0.0, -0.15), (-1, 0, 0))
    t = red_tick(w=0.008, h=0.030, t=0.012)
    bb = bbox(column)
    t.location = ((hit.x + 0.002) if hit else bb[1] - 0.002, 0.0, -0.15)
    bpy.ops.object.transform_apply(location=True)
    assign_material(t, M["red"])

    upper = join_parts([column, block, cap, t], f"{tag}_upper_leg")
    for o in (upper,):
        shade_all_smooth(o)
    log(f"{tag} upper bbox: {[round(v, 3) for v in bbox(upper)]} "
        f"tris={tri_count(upper)}")

    # --- shin pistons (front) + spring (rear), tops at lower-frame -0.03
    apply_matrix(pistons, rot("Z", 90) @ mat_scale(S_SHIN))
    bb = bbox(pistons)
    apply_matrix(pistons, mat_trans((0.08 - (bb[0] + bb[1]) / 2,
                                     -(bb[2] + bb[3]) / 2, -0.03 - bb[5])))
    assign_material(pistons, M["silver"])
    apply_matrix(spring, rot("Z", 90) @ mat_scale(S_SHIN))
    bb = bbox(spring)
    apply_matrix(spring, mat_trans((-0.06 - (bb[0] + bb[1]) / 2,
                                    -(bb[2] + bb[3]) / 2, -0.03 - bb[5])))
    assign_material(spring, M["black"])
    hit = ray_hit([pistons], (1.0, 0.0, -0.25), (-1, 0, 0))
    bb = bbox(pistons)
    t2 = red_tick(w=0.008, h=0.020, t=0.010)
    t2.location = ((hit.x + 0.002) if hit else bb[1] - 0.002, 0.0, -0.25)
    bpy.ops.object.transform_apply(location=True)
    assign_material(t2, M["red"])
    lower = join_parts([pistons, spring, t2], f"{tag}_lower_leg")
    shade_all_smooth(lower)
    log(f"{tag} lower bbox: {[round(v, 3) for v in bbox(lower)]} "
        f"tris={tri_count(lower)}")

    # --- foot: fork rotated so mouth faces +X (toe notch forward)
    phi = -mouth + (180 if flip else 0)
    apply_matrix(fork, rot("Z", phi) @ mat_scale(S_FOOT))
    bb = bbox(fork)
    log(f"{tag} fork size: dx={bb[1]-bb[0]:.3f} dy={bb[3]-bb[2]:.3f} "
        f"dz={bb[5]-bb[4]:.3f}")
    apply_matrix(fork, mat_trans((-0.10 - bb[0], -(bb[2] + bb[3]) / 2,
                                  -0.20 - bb[4])))
    assign_material(fork, M["silver"])

    # --- ankle barrel: axis -> Y, bridging fork top (-0.16) and shin bottom
    apply_matrix(ankle, rot("X", 90) @ mat_scale(S_ANKLE))
    center_to(ankle, (0.0, 0.0, -0.085))
    assign_material(ankle, M["black"])

    foot = join_parts([fork, ankle], f"{tag}_foot")
    shade_all_smooth(foot)
    log(f"{tag} foot bbox: {[round(v, 3) for v in bbox(foot)]} "
        f"tris={tri_count(foot)}")
    return upper, lower, foot


def build_legs(M, hipcyl):
    shoulders = import_shells("shoulders_v3.stl")
    # sorted desc: [disc 44K, disc 44K, block 13K, block 13K]
    disc_l, disc_r, block_l, block_r = shoulders
    ankle_r = hipcyl.copy()
    ankle_r.data = hipcyl.data.copy()
    bpy.context.scene.collection.objects.link(ankle_r)
    # NOTE: robot.xml mounts the LEFT leg bodies at y=-0.29 (outer=-Y).
    upper_l, lower_l, foot_l = build_leg(
        M, "left", import_shells("l-leg_split.stl"), block_l, disc_l,
        hipcyl, -1, FOOT_FLIP["left"])
    upper_r, lower_r, foot_r = build_leg(
        M, "right", import_shells("r-leg_split.stl"), block_r, disc_r,
        ankle_r, +1, FOOT_FLIP["right"])
    upper_l.name = "left_upper_leg"
    lower_l.name = "left_lower_leg"
    foot_l.name = "left_foot"
    upper_r.name = "right_upper_leg"
    lower_r.name = "right_lower_leg"
    foot_r.name = "right_foot"
    return upper_l, lower_l, foot_l, upper_r, lower_r, foot_r


# ---------------------------------------------------------------- assembly

def build_assembly(parts):
    asm = []
    for name, o in parts.items():
        dup = o.copy()
        dup.data = o.data  # share mesh (render-only instance)
        bpy.context.scene.collection.objects.link(dup)
        off = BODY_OFFSETS[name]
        dup.location = (TORSO_POS[0] + off[0], TORSO_POS[1] + off[1],
                        TORSO_POS[2] + off[2])
        dup.name = f"ASM_{name}"
        asm.append(dup)
    return asm


# ---------------------------------------------------------------- studio + cameras

def build_white_room():
    """Small self-lit white floor card (emission 1.0 = 255). Its edges are
    invisible against the LightPath camera-white world; small area keeps
    under-light wash low so the subject keeps contrast."""
    mat = bpy.data.materials.new("M_room")
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    em = nt.nodes.new("ShaderNodeEmission")
    em.inputs["Color"].default_value = (1, 1, 1, 1)
    em.inputs["Strength"].default_value = 1.0
    nt.links.new(em.outputs[0], out.inputs[0])
    bpy.ops.mesh.primitive_plane_add(size=6.0, location=(0, 0, 0.0))
    floor = bpy.context.active_object
    floor.name = "RoomFloor"
    floor.data.materials.append(mat)


def build_blob_shadow():
    """Painted soft contact shadow (dark under the pads, fading out),
    offset back-right as if cast by the front-left key."""
    import numpy as np
    S = 256
    # world footprint of the 4x4 m plane centered at (-0.2, -0.1)
    cx, cy, size = -0.2, -0.1, 4.0
    ys, xs = np.mgrid[0:S, 0:S].astype(np.float64)
    wx = cx + (xs / (S - 1) - 0.5) * size
    wy = cy + (ys / (S - 1) - 0.5) * size

    def blob(x0, y0, rx, ry, peak):
        dx = (wx - x0) / rx
        dy = (wy - y0) / ry
        d2 = dx * dx + dy * dy
        return peak * np.clip(1.0 - d2, 0.0, 1.0) ** 1.5

    a = np.zeros((S, S))
    a = np.maximum(a, blob(-0.16, -0.45, 0.38, 0.34, 0.50))  # left pads
    a = np.maximum(a, blob(-0.16, 0.13, 0.38, 0.34, 0.50))   # right pads
    a = np.maximum(a, blob(-0.30, -0.16, 1.15, 0.85, 0.22))  # torso cast
    img = bpy.data.images.new("BlobShadow", S, S, alpha=True)
    buf = np.zeros((S, S, 4), dtype=np.float32)
    buf[:, :, 3] = a.astype(np.float32)
    flat = np.empty(S * S * 4, dtype=np.float32)
    flat[:] = buf.reshape(-1)
    img.pixels.foreach_set(flat)
    img.update()
    try:
        img.pack()
    except Exception as e:
        log(f"blob pack skipped: {e}")
    mat = bpy.data.materials.new("M_blob")
    mat.use_nodes = True
    mat.blend_method = "BLEND"
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    bsdf.inputs["Base Color"].default_value = (0, 0, 0, 1)
    bsdf.inputs["Roughness"].default_value = 1.0
    tex = mat.node_tree.nodes.new("ShaderNodeTexImage")
    tex.image = img
    mat.node_tree.links.new(tex.outputs["Alpha"], bsdf.inputs["Alpha"])
    bpy.ops.mesh.primitive_plane_add(size=size, location=(cx, cy, 0.002))
    plane = bpy.context.active_object
    plane.name = "BlobShadow"
    plane.data.materials.append(mat)


def build_studio():
    scene = bpy.context.scene
    scene.view_settings.view_transform = "Standard"
    scene.view_settings.look = "None"
    # Seamless white studio per studio-look.md: LightPath world shows the
    # camera pure white (255 bg) while the subject is lit by a dim ambient
    # plus the key/fill/rim rig. Grounding is a soft painted blob shadow
    # under the feet (like the reference's) over a white floor card.
    world = bpy.data.worlds.new("Studio")
    world.use_nodes = True
    nt = world.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputWorld")
    lp = nt.nodes.new("ShaderNodeLightPath")
    cambg = nt.nodes.new("ShaderNodeBackground")
    cambg.inputs["Color"].default_value = (1, 1, 1, 1)
    cambg.inputs["Strength"].default_value = 1.0
    amb = nt.nodes.new("ShaderNodeBackground")
    amb.inputs["Color"].default_value = (1, 1, 1, 1)
    amb.inputs["Strength"].default_value = 0.15
    mix = nt.nodes.new("ShaderNodeMixShader")
    nt.links.new(lp.outputs["Is Camera Ray"], mix.inputs[0])
    nt.links.new(amb.outputs[0], mix.inputs[1])
    nt.links.new(cambg.outputs[0], mix.inputs[2])
    nt.links.new(mix.outputs[0], out.inputs["Surface"])
    scene.world = world
    scene.render.film_transparent = False
    build_white_room()
    build_blob_shadow()
    # Aim target at robot center.
    bpy.ops.object.empty_add(location=(0, 0, 0.95))
    aim = bpy.context.active_object
    aim.name = "Aim"

    def track(o):
        c = o.constraints.new("TRACK_TO")
        c.target = aim
        c.track_axis = "TRACK_NEGATIVE_Z"
        c.up_axis = "UP_Y"

    key = bpy.data.objects.new("Key", bpy.data.lights.new("Key", "AREA"))
    key.data.energy = 220.0
    key.data.size = 3.0
    scene.collection.objects.link(key)
    key.location = (2.5, 2.0, 4.0)
    track(key)
    fill = bpy.data.objects.new("Fill", bpy.data.lights.new("Fill", "AREA"))
    fill.data.energy = 60.0
    fill.data.size = 2.0
    scene.collection.objects.link(fill)
    fill.location = (1.0, -2.5, 1.6)
    track(fill)
    rim = bpy.data.objects.new("Rim", bpy.data.lights.new("Rim", "SUN"))
    rim.data.energy = 1.2
    scene.collection.objects.link(rim)
    rim.location = (-4.0, 1.0, 5.0)
    track(rim)

    scene.render.engine = "BLENDER_EEVEE"
    try:
        scene.eevee.taa_render_samples = 16 if DRAFT else 64
    except Exception as e:
        log(f"eevee tweak skipped: {e}")
    return aim


def make_camera(name, az_deg, el_deg, dist, aim):
    scene = bpy.context.scene
    cam = bpy.data.objects.new(name, bpy.data.cameras.new(name))
    cam.data.lens = 50.0
    cam.data.sensor_fit = "HORIZONTAL"
    az, el = radians(az_deg), radians(el_deg)
    cam.location = (aim.location.x + dist * cos(el) * cos(az),
                    aim.location.y + dist * cos(el) * sin(az),
                    aim.location.z + dist * sin(el))
    scene.collection.objects.link(cam)
    c = cam.constraints.new("TRACK_TO")
    c.target = aim
    c.track_axis = "TRACK_NEGATIVE_Z"
    c.up_axis = "UP_Y"
    return cam


DRAFT = bool(os.environ.get("ED209_DRAFT"))


def render_view(cam, path):
    scene = bpy.context.scene
    scene.camera = cam
    if DRAFT:
        scene.render.resolution_x = 900
        scene.render.resolution_y = 1125
    else:
        scene.render.resolution_x = 1200
        scene.render.resolution_y = 1500
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    scene.render.filepath = path
    bpy.ops.render.render(write_still=True)
    log(f"rendered {path}")


# ---------------------------------------------------------------- export + verify

def export_parts(parts):
    os.makedirs(ASSETS, exist_ok=True)
    for name, o in parts.items():
        stl_write(o, os.path.join(ASSETS, f"{name}.stl"))
        obj_write(o, os.path.join(ASSETS, f"{name}.obj"))
        log(f"exported {name}.stl + .obj")


def verify(parts):
    total = 0
    print("=" * 78, flush=True)
    print(f"{'part':20s} {'tris':>8s} {'budget':>8s} {'env':5s} bbox",
          flush=True)
    for name, o in parts.items():
        p = os.path.join(ASSETS, f"{name}.stl")
        tris = tri_count(o)
        total += tris
        with open(p, "rb") as f:
            hdr = f.read(84)
            (hdrcount,) = struct.unpack("<I", hdr[80:84])
            f.seek(0, 2)
            size = f.tell()
        filecount = (size - 84) // 50
        ok_tri = (hdrcount == filecount == tris)
        bb = bbox(o)
        env = ENVELOPES[name]
        ok_env = (bb[0] >= env[0] - 1e-6 and bb[1] <= env[1] + 1e-6
                  and bb[2] >= env[2] - 1e-6 and bb[3] <= env[3] + 1e-6
                  and bb[4] >= env[4] - 1e-6 and bb[5] <= env[5] + 1e-6)
        ok_budget = tris <= BUDGETS[name]
        flag = "" if (ok_tri and ok_env and ok_budget) else "  <-- CHECK"
        print(f"{name:20s} {tris:8d} {BUDGETS[name]:8d} "
              f"{'OK' if ok_env else 'OVER'}  "
              f"x[{bb[0]:.2f},{bb[1]:.2f}] y[{bb[2]:.2f},{bb[3]:.2f}] "
              f"z[{bb[4]:.2f},{bb[5]:.2f}] hdr={hdrcount}{flag}", flush=True)
    print(f"TOTAL tris: {total} (limit ~425000)", flush=True)
    print("=" * 78, flush=True)


# ---------------------------------------------------------------- main

def sweep_leftovers():
    for o in [x for x in bpy.context.scene.objects
              if ".shell" in x.name and not x.name.startswith("ASM_")]:
        log(f"removing leftover {o.name}")
        bpy.data.objects.remove(o, do_unlink=True)


def main():
    clear_scene()
    M = build_materials()
    parts = {}
    torso, extras = build_torso(M)
    parts["torso"] = torso
    parts["neck_bellows"] = build_neck(M)
    pod_l, pod_r = build_pods(M)
    parts["left_weapon_pod"] = pod_l
    parts["right_weapon_pod"] = pod_r
    can_l, can_r = build_cannons(M)
    parts["left_cannons"] = can_l
    parts["right_cannons"] = can_r
    (upper_l, lower_l, foot_l,
     upper_r, lower_r, foot_r) = build_legs(M, extras["hipcyl"])
    parts["left_upper_leg"] = upper_l
    parts["left_lower_leg"] = lower_l
    parts["left_foot"] = foot_l
    parts["right_upper_leg"] = upper_r
    parts["right_lower_leg"] = lower_r
    parts["right_foot"] = foot_r

    sweep_leftovers()
    export_parts(parts)
    verify(parts)

    build_assembly(parts)
    aim = build_studio()
    cam_f = make_camera("CamFront", -12.0, 20.0, 3.35, aim)
    cam_b = make_camera("CamBack", 165.0, 20.0, 3.35, aim)
    render_view(cam_f, RENDER_FRONT)
    if not DRAFT:
        render_view(cam_b, RENDER_BACK)

    bpy.ops.wm.save_as_mainfile(filepath=BLEND)
    log(f"saved {BLEND}")
    log("DONE")


main()

