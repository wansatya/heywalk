import * as THREE from 'three'

// Stylised white humanoid twin (~0.27 m tall): rounded white shell, grey
// joint accents, glowing blue eyes and a chest emblem, facing +X at
// rotation.y = 0 to match the sim heading convention
// (sim (x, y, theta) -> three (x, 0, -y), rotation.y = theta).
// Built from primitives; fine mechanical detail is intentionally omitted.

export const MINIBOT_HEIGHT_M = 0.27

const STEP_PHASE_PER_M = 55
const MAX_SWING_RAD = 0.5

export interface Minibot {
  group: THREE.Group
  /** Advance the gait by the distance just travelled (metres). */
  stride: (distanceM: number, speedMps: number) => void
  /** Per-frame housekeeping: gait easing, idle motion, collision flash decay. */
  update: (dt: number) => void
  setCollisionFlash: () => void
  setIdleEnabled: (enabled: boolean) => void
  dispose: () => void
}

export function buildMinibot(): Minibot {
  const group = new THREE.Group()
  const materials = new Set<THREE.Material>()
  const mat = <T extends THREE.Material>(m: T): T => {
    materials.add(m)
    return m
  }

  const white = mat(new THREE.MeshStandardMaterial({ color: 0xf2f4f6, roughness: 0.35, metalness: 0 }))
  const grey = mat(new THREE.MeshStandardMaterial({ color: 0x9aa0a8, roughness: 0.5, metalness: 0.1 }))
  const darkGrey = mat(new THREE.MeshStandardMaterial({ color: 0x3a3f47, roughness: 0.6, metalness: 0 }))
  const glowBlue = mat(
    new THREE.MeshStandardMaterial({ color: 0x2f9df0, emissive: 0x2f9df0, emissiveIntensity: 1.4 }),
  )

  const add = (mesh: THREE.Mesh, parent: THREE.Object3D = group) => {
    mesh.castShadow = true
    parent.add(mesh)
    return mesh
  }
  const box = (w: number, h: number, d: number, material: THREE.Material) =>
    new THREE.Mesh(new THREE.BoxGeometry(w, h, d), material)
  const cylX = (r: number, h: number, material: THREE.Material, seg = 16) => {
    const m = new THREE.Mesh(new THREE.CylinderGeometry(r, r, h, seg), material)
    m.rotation.z = Math.PI / 2
    return m
  }
  const cylZ = (r: number, h: number, material: THREE.Material, seg = 16) => {
    const m = new THREE.Mesh(new THREE.CylinderGeometry(r, r, h, seg), material)
    m.rotation.x = Math.PI / 2
    return m
  }

  // ---- Legs (pivot at the hips) ----
  const makeLeg = (side: -1 | 1) => {
    const hip = new THREE.Group()
    hip.position.set(0, 0.095, side * 0.033)
    const thigh = add(box(0.055, 0.035, 0.055, white), hip)
    thigh.position.y = -0.0175
    const shin = add(box(0.05, 0.032, 0.05, white), hip)
    shin.position.y = -0.051
    const knee = add(box(0.004, 0.02, 0.038, grey), hip)
    knee.position.set(0.026, -0.05, 0)
    const foot = add(box(0.09, 0.023, 0.05, white), hip)
    foot.position.set(0.012, -0.066, 0)
    const sole = add(box(0.09, 0.007, 0.05, grey), hip)
    sole.position.set(0.012, -0.0745, 0)
    group.add(hip)
    return hip
  }
  const legL = makeLeg(-1)
  const legR = makeLeg(1)

  // ---- Pelvis + torso ----
  const pelvis = add(box(0.09, 0.04, 0.1, white))
  pelvis.position.y = 0.115
  const waist = add(box(0.092, 0.012, 0.102, grey))
  waist.position.y = 0.14
  const torso = add(box(0.1, 0.055, 0.11, white))
  torso.position.y = 0.1625
  const chest = add(box(0.012, 0.045, 0.056, grey))
  chest.position.set(0.052, 0.165, 0)

  // Chest emblem: dark ring + blue dots, facing +X.
  const ring = add(new THREE.Mesh(new THREE.TorusGeometry(0.012, 0.0032, 10, 24), darkGrey))
  ring.rotation.y = Math.PI / 2
  ring.position.set(0.06, 0.169, 0)
  for (let i = 0; i < 4; i++) {
    const a = (i / 4) * Math.PI * 2 + Math.PI / 4
    const dot = add(new THREE.Mesh(new THREE.SphereGeometry(0.0026, 8, 8), glowBlue))
    dot.position.set(0.06, 0.169 + Math.sin(a) * 0.006, Math.cos(a) * 0.006)
  }
  const centerDot = add(new THREE.Mesh(new THREE.SphereGeometry(0.0026, 8, 8), glowBlue))
  centerDot.position.set(0.06, 0.169, 0)
  const chestDot = add(new THREE.Mesh(new THREE.SphereGeometry(0.0022, 8, 8), darkGrey))
  chestDot.position.set(0.0585, 0.148, 0)

  // ---- Arms (pivot at the shoulders) ----
  const makeArm = (side: -1 | 1) => {
    const shoulder = new THREE.Group()
    shoulder.position.set(0, 0.18, side * 0.068)
    const cap = add(new THREE.Mesh(new THREE.SphereGeometry(0.02, 14, 12), white), shoulder)
    cap.position.set(0, 0, 0)
    const upper = add(new THREE.Mesh(new THREE.CylinderGeometry(0.015, 0.015, 0.045, 12), white), shoulder)
    upper.position.y = -0.0225
    const elbow = add(cylZ(0.017, 0.013, grey), shoulder)
    elbow.position.y = -0.048
    const fore = add(new THREE.Mesh(new THREE.CylinderGeometry(0.013, 0.013, 0.04, 12), white), shoulder)
    fore.position.y = -0.07
    const hand = add(new THREE.Mesh(new THREE.SphereGeometry(0.018, 12, 10), white), shoulder)
    hand.position.y = -0.098
    group.add(shoulder)
    return shoulder
  }
  const armL = makeArm(-1)
  const armR = makeArm(1)

  // ---- Neck + head ----
  const neck = add(new THREE.Mesh(new THREE.CylinderGeometry(0.02, 0.02, 0.018, 14), grey))
  neck.position.y = 0.198
  const head = new THREE.Group()
  head.position.y = 0.207
  group.add(head)
  const skull = add(box(0.085, 0.055, 0.08, white), head)
  skull.position.y = 0.0275
  const capTop = add(box(0.09, 0.018, 0.085, grey), head)
  capTop.position.y = 0.06
  const sensor = add(box(0.004, 0.012, 0.012, darkGrey), head)
  sensor.position.set(0.0435, 0.05, 0)
  for (const side of [-1, 1]) {
    const ear = add(cylZ(0.014, 0.01, grey), head)
    ear.position.set(0, 0.028, side * 0.043)
    const eyeWhite = add(cylX(0.011, 0.004, white), head)
    eyeWhite.position.set(0.0435, 0.031, side * 0.02)
    const pupil = add(new THREE.Mesh(new THREE.SphereGeometry(0.0055, 10, 10), glowBlue), head)
    pupil.position.set(0.0465, 0.031, side * 0.02)
  }
  const mouth = add(box(0.002, 0.003, 0.014, darkGrey), head)
  mouth.position.set(0.043, 0.015, 0)

  let phase = 0
  let swing = 0
  let flash = 0
  let idleEnabled = true
  let idleT = 0

  return {
    group,
    stride: (distanceM: number, speedMps: number) => {
      phase += Math.max(0, distanceM) * STEP_PHASE_PER_M
      const target = THREE.MathUtils.clamp(speedMps / 0.35, 0, 1)
      swing += (target - swing) * 0.25
      const s = Math.sin(phase) * MAX_SWING_RAD * swing
      legL.rotation.z = s
      legR.rotation.z = -s
      armL.rotation.z = -s * 0.7
      armR.rotation.z = s * 0.7
      group.position.y = Math.abs(Math.cos(phase)) * 0.005 * swing
      torso.rotation.x = Math.sin(phase) * 0.04 * swing
    },
    update: (dt: number) => {
      if (idleEnabled) {
        idleT += dt
        head.rotation.x = Math.sin(idleT * 1.7) * 0.04
      } else {
        head.rotation.x = 0
      }
      if (flash > 0) {
        flash = Math.max(0, flash - dt * 2.5)
        white.emissive.setRGB(flash * 0.8, flash * 0.08, flash * 0.08)
      } else {
        swing += (0 - swing) * Math.min(1, dt * 3)
        if (swing < 0.01) {
          legL.rotation.z = 0
          legR.rotation.z = 0
          armL.rotation.z = 0
          armR.rotation.z = 0
        }
      }
    },
    setCollisionFlash: () => {
      flash = 1
    },
    setIdleEnabled: (enabled: boolean) => {
      idleEnabled = enabled
    },
    dispose: () => {
      group.traverse((child) => {
        if (child instanceof THREE.Mesh) child.geometry.dispose()
      })
      materials.forEach((m) => m.dispose())
    },
  }
}
