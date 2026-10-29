import * as THREE from 'three'
import { OrbitControls } from 'three/addons/controls/OrbitControls.js'
import { loadSavedCamera, saveCamera } from './cameraState'
import { STLLoader } from 'three/addons/loaders/STLLoader.js'
import type { BodyPose, MicroduckEnvGeom, MicroduckModel, SimLayout } from '../../../api/types'

// MicroDuck 3D view: official STL meshes posed strictly from MuJoCo body
// poses, plus the composed environment (crates, ball, goal flag).
//
// Frames: everything MuJoCo-flavoured lives inside `mujocoFrame`, a group
// rotated -90° about X, so raw MuJoCo positions (x, y, z) and quaternions
// (w, x, y, z) can be applied verbatim: MuJoCo +x -> three +x (forward),
// +y -> three -z (left), +z -> three +y (up). Mesh STL vertices, geom
// offsets, and body poses all share that frame; only the floor, grid,
// lights, and camera live in three's own frame.

const BG_COLOR = 0x0b0c10
const FLOOR_COLOR = 0x0e0f13
const GRID_MINOR = 0x23262e
const CRATE_EDGE = 0x2a1503
const FALLBACK_COLOR = 0xe8b339
/** Arrange drag bounds (MuJoCo floor metres, matches the 4 m grid). */
const LAYOUT_LIMIT = 2
/** Pointer must leave this radius around the duck before alt-drag turns it. */
const YAW_DEADZONE = 0.05
/**
 * Motion smoothing rate (1/s). Telemetry poses arrive at ~20 Hz; each
 * rendered frame eases body groups toward the latest sample with
 * k = 1 - exp(-dt * RATE), so a walk renders as continuous motion instead
 * of 20 snaps a second. 14 settles in ~80 ms: smooth, no floaty lag.
 */
const SMOOTHING_RATE = 14
/** Teleports bigger than this snap instead of easing (new run, respawn). */
const SNAP_DISTANCE_M = 0.5
/**
 * Arena-wall billboards: images at `/ads/side_<px|nx|py|ny>_<1..6>.png`
 * (or `.jpg`) are laid out along the matching wall's inner face, up to
 * {@link ADS_PER_SIDE} per side, aspect-preserved (contain-fit, no
 * stretching). Missing images are skipped silently; a side with no
 * images keeps its plain wall.
 */
const AD_BASE_URL = '/ads'
const ADS_PER_SIDE = 6
const AD_EXTENSIONS = ['png', 'jpg']
/** Gap between panel and wall face, and row insets (MuJoCo metres). */
const AD_FACE_GAP = 0.006
const AD_EDGE_MARGIN = 0.06
const AD_TOP_MARGIN = 0.02
/**
 * Idle showcase spin (OrbitControls units: 2.0 = one orbit per ~30 s).
 * Slow enough to inspect the duck while it reads as alive.
 */
const AUTO_ORBIT_SPEED = 0.9

export type LayoutChangeHandler = (layout: SimLayout) => void

function mujocoQuatToThree(
  quat: [number, number, number, number],
  out = new THREE.Quaternion(),
): THREE.Quaternion {
  // MuJoCo [w, x, y, z] -> three (x, y, z, w); the parent mujocoFrame
  // carries the frame rotation, so no basis change is needed here.
  out.set(quat[1], quat[2], quat[3], quat[0])
  return out
}

/**
 * Classic truncated-icosahedron soccer ball, drawn in code (no assets).
 *
 * The 12 black pentagons sit on the icosahedron vertices with corners
 * pointing at their edge neighbours (as real truncation cuts them), at
 * the true angular radius, so the ball reads correctly from every side.
 * Pentagons are sampled as 3D directions and projected to equirectangular
 * UVs matching THREE.SphereGeometry, drawn triple-wide so the seam wraps.
 */
function makeSoccerTexture(): THREE.CanvasTexture {
  const W = 512
  const H = 256
  const canvas = document.createElement('canvas')
  canvas.width = W
  canvas.height = H
  const ctx = canvas.getContext('2d')
  if (!ctx) throw new Error('Canvas 2D is unavailable.')
  ctx.fillStyle = '#f4f4f2'
  ctx.fillRect(0, 0, W, H)

  interface Vec3 { x: number; y: number; z: number }
  const norm = (v: Vec3): Vec3 => {
    const n = Math.hypot(v.x, v.y, v.z) || 1
    return { x: v.x / n, y: v.y / n, z: v.z / n }
  }
  const dot = (a: Vec3, b: Vec3): number => a.x * b.x + a.y * b.y + a.z * b.z
  const cross = (a: Vec3, b: Vec3): Vec3 => ({
    x: a.y * b.z - a.z * b.y,
    y: a.z * b.x - a.x * b.z,
    z: a.x * b.y - a.y * b.x,
  })
  // SphereGeometry UVs: x = -cos(phi) sin(theta), z = sin(phi) sin(theta).
  const project = (d: Vec3): [number, number] => {
    const u = (((Math.atan2(d.z, -d.x) / (Math.PI * 2)) % 1) + 1) % 1
    const v = Math.acos(Math.min(1, Math.max(-1, d.y))) / Math.PI
    return [u * W, v * H]
  }

  const golden = (1 + Math.sqrt(5)) / 2
  const verts: Vec3[] = [
    [0, 1, golden], [0, 1, -golden], [0, -1, golden], [0, -1, -golden],
    [1, golden, 0], [1, -golden, 0], [-1, golden, 0], [-1, -golden, 0],
    [golden, 0, 1], [golden, 0, -1], [-golden, 0, 1], [-golden, 0, -1],
  ].map(([x, y, z]) => norm({ x, y, z }))

  // Truncation edge: icosahedron edge / 3, pentagon circumradius from it.
  const edge = 1 / Math.sin((2 * Math.PI) / 5)
  const rho = Math.asin((edge / 3 / (2 * Math.sin(Math.PI / 5))) / 1)
  const cosRho = Math.cos(rho)
  const sinRho = Math.sin(rho)
  const EDGE_STEPS = 8

  ctx.fillStyle = '#17181c'
  ctx.strokeStyle = 'rgba(0, 0, 0, 0.4)'
  ctx.lineWidth = 2
  for (const v of verts) {
    const up = Math.abs(v.y) > 0.9 ? { x: 1, y: 0, z: 0 } : { x: 0, y: 1, z: 0 }
    const t1 = norm(cross(v, up))
    const t2 = cross(v, t1)
    // Neighbour directions = pentagon corner angles.
    const angles = verts
      .filter((o) => o !== v)
      .map((o) => ({ o, d: dot(v, o) }))
      .sort((a, b) => b.d - a.d)
      .slice(0, 5)
      .map(({ o }) => Math.atan2(dot(o, t2), dot(o, t1)))
      .sort((a, b) => a - b)
    const corners = angles.map((a) =>
      norm({
        x: v.x * cosRho + (t1.x * Math.cos(a) + t2.x * Math.sin(a)) * sinRho,
        y: v.y * cosRho + (t1.y * Math.cos(a) + t2.y * Math.sin(a)) * sinRho,
        z: v.z * cosRho + (t1.z * Math.cos(a) + t2.z * Math.sin(a)) * sinRho,
      }),
    )
    // Sample great-circle edges so polar pentagons keep their shape.
    const ring: Array<[number, number]> = []
    for (let k = 0; k < 5; k++) {
      const a = corners[k]
      const b = corners[(k + 1) % 5]
      const omega = Math.acos(Math.min(1, Math.max(-1, dot(a, b))))
      for (let s = 0; s < EDGE_STEPS; s++) {
        const t = s / EDGE_STEPS
        const g = norm({
          x: (a.x * Math.sin((1 - t) * omega) + b.x * Math.sin(t * omega)) / Math.sin(omega),
          y: (a.y * Math.sin((1 - t) * omega) + b.y * Math.sin(t * omega)) / Math.sin(omega),
          z: (a.z * Math.sin((1 - t) * omega) + b.z * Math.sin(t * omega)) / Math.sin(omega),
        })
        ring.push(project(g))
      }
    }
    // Unwrap the ring so seam-crossing pentagons don't smear across the map.
    for (let i = 1; i < ring.length; i++) {
      while (ring[i][0] - ring[i - 1][0] > W / 2) ring[i][0] -= W
      while (ring[i][0] - ring[i - 1][0] < -W / 2) ring[i][0] += W
    }
    for (const dx of [-W, 0, W]) {
      ctx.beginPath()
      ring.forEach(([px, py], i) => {
        if (i === 0) ctx.moveTo(px + dx, py)
        else ctx.lineTo(px + dx, py)
      })
      ctx.closePath()
      ctx.fill()
      ctx.stroke()
    }
  }
  const texture = new THREE.CanvasTexture(canvas)
  texture.colorSpace = THREE.SRGBColorSpace
  texture.anisotropy = 4
  return texture
}

export interface MicroduckSceneOptions {
  /** Resolves a mesh filename (e.g. "sole_left.stl") to its download URL. */
  assetUrl: (filename: string) => string
  /** Called as each unique STL finishes (loaded, total). */
  onProgress?: (loaded: number, total: number) => void
}

export class MicroduckScene {
  private renderer: THREE.WebGLRenderer
  private scene = new THREE.Scene()
  private camera: THREE.PerspectiveCamera
  private controls: OrbitControls
  private mujocoFrame = new THREE.Group()
  private bodyGroups = new Map<string, THREE.Group>()
  private rafId = 0
  private running = false
  private resizeObserver: ResizeObserver
  private container: HTMLElement
  private materials = new Map<string, THREE.Material>()
  private geometries: THREE.BufferGeometry[] = []
  private soccerTexture: THREE.Texture | null = null
  private adTextures: THREE.Texture[] = []
  private disposed = false
  private layout: SimLayout = { duck: null, props: {} }
  private arrangeCb: LayoutChangeHandler | null = null
  private raycaster = new THREE.Raycaster()
  private dragPlane = new THREE.Plane(new THREE.Vector3(0, 1, 0), 0)
  private dragId: string | null = null
  private dragTurn = false
  private pointerNdc = new THREE.Vector2()
  private planeHit = new THREE.Vector3()
  private targets = new Map<string, { pos: THREE.Vector3; quat: THREE.Quaternion }>()
  private settledBodies = new Set<string>()
  private lastFrameNow = 0

  constructor(
    container: HTMLElement,
    private model: MicroduckModel,
    private options: MicroduckSceneOptions,
  ) {
    this.container = container
    this.scene.background = new THREE.Color(BG_COLOR)
    this.scene.fog = new THREE.Fog(BG_COLOR, 6, 16)

    this.camera = new THREE.PerspectiveCamera(45, 1, 0.01, 100)

    this.renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'high-performance' })
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))
    this.renderer.outputColorSpace = THREE.SRGBColorSpace
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping
    this.renderer.shadowMap.enabled = true
    this.renderer.shadowMap.type = THREE.PCFSoftShadowMap
    container.appendChild(this.renderer.domElement)

    this.controls = new OrbitControls(this.camera, this.renderer.domElement)
    this.controls.enableDamping = true
    this.controls.dampingFactor = 0.08
    this.controls.minDistance = 0.15
    this.controls.maxDistance = 6
    this.controls.maxPolarAngle = Math.PI / 2 - 0.02
    // 'end' fires after user orbit/zoom/pan gestures only, never after
    // programmatic moves, so this persists the user's view, not ours.
    this.controls.addEventListener('end', this.persistCamera)

    const hemi = new THREE.HemisphereLight(0xdfe8ff, 0x14161c, 0.55)
    this.scene.add(hemi)
    const key = new THREE.DirectionalLight(0xfff1de, 1.7)
    key.position.set(0.9, 1.5, 0.6)
    key.castShadow = true
    key.shadow.mapSize.set(1024, 1024)
    key.shadow.camera.left = -1.2
    key.shadow.camera.right = 1.2
    key.shadow.camera.top = 1.2
    key.shadow.camera.bottom = -1.2
    key.shadow.camera.far = 6
    key.shadow.bias = -0.0004
    this.scene.add(key, key.target)
    const rim = new THREE.DirectionalLight(0x88aaff, 0.4)
    rim.position.set(-0.7, 0.5, -0.9)
    this.scene.add(rim)

    // Floor + faint minor grid; the orange major grid is gone (disturbing).
    const floor = new THREE.Mesh(
      new THREE.PlaneGeometry(30, 30),
      new THREE.MeshStandardMaterial({ color: FLOOR_COLOR, roughness: 1 }),
    )
    floor.rotation.x = -Math.PI / 2
    floor.receiveShadow = true
    this.scene.add(floor)
    const minor = new THREE.GridHelper(4, 20, GRID_MINOR, GRID_MINOR)
    minor.position.y = 0.001
    minor.material.transparent = true
    minor.material.opacity = 0.55
    this.scene.add(minor)

    // MuJoCo frame: raw MuJoCo pos/quat apply verbatim inside it.
    this.mujocoFrame.rotation.x = -Math.PI / 2
    this.scene.add(this.mujocoFrame)

    for (const name of model.bodies) this.groupFor(name)
    this.buildEnvironment(model)

    this.resetCamera()
    this.restoreCamera()
    this.resizeObserver = new ResizeObserver(() => this.resize())
    this.resizeObserver.observe(container)
    this.resize()
    // Capture on the container so a drag grab can pre-empt OrbitControls,
    // which listens on the canvas itself.
    container.addEventListener('pointerdown', this.onPointerDown, true)
    container.addEventListener('pointermove', this.onHover)
    container.addEventListener('pointerleave', this.onHoverEnd)
  }

  private groupFor(name: string): THREE.Group {
    let group = this.bodyGroups.get(name)
    if (!group) {
      group = new THREE.Group()
      group.name = name
      this.bodyGroups.set(name, group)
      this.mujocoFrame.add(group)
    }
    return group
  }

  private materialFor(rgba: number[], role: string): THREE.Material {
    const key = `${role}:${rgba.map((v) => v.toFixed(3)).join(',')}`
    const cached = this.materials.get(key)
    if (cached) return cached
    const color = new THREE.Color(rgba[0], rgba[1], rgba[2])
    const opacity = rgba[3] ?? 1
    let material: THREE.Material
    if (role === 'ball') {
      if (!this.soccerTexture) this.soccerTexture = makeSoccerTexture()
      material = new THREE.MeshStandardMaterial({
        map: this.soccerTexture,
        roughness: 0.55,
        metalness: 0.0,
      })
    } else {
      material = new THREE.MeshStandardMaterial({
        color,
        roughness: role === 'flag' ? 0.85 : 0.5,
        metalness: 0.08,
        transparent: opacity < 1,
        opacity,
        side: role === 'flag' ? THREE.DoubleSide : THREE.FrontSide,
      })
    }
    this.materials.set(key, material)
    return material
  }

  private track<T extends THREE.BufferGeometry>(geometry: T): T {
    this.geometries.push(geometry)
    return geometry
  }

  /** Environment props from the composed scene (same data as physics). */
  private buildEnvironment(model: MicroduckModel): void {
    for (const env of model.environment) {
      const group = this.groupFor(env.body)
      group.position.set(env.pos[0], env.pos[1], env.pos[2])
      // Walls are fixed arena bounds: rendered, never draggable.
      const fixed = env.geoms.length > 0 && env.geoms.every((geom) => geom.role === 'wall')
      for (const geom of env.geoms) {
        const mesh = this.buildEnvGeom(geom)
        if (mesh) {
          if (!fixed) mesh.userData.dragId = env.body
          group.add(mesh)
        }
      }
      if (fixed && env.geoms.length > 0) this.addWallAds(group, env.body, env.geoms[0])
    }
  }

  /** Billboard panels on a wall's inner face (async; plain wall fallback). */
  private addWallAds(group: THREE.Group, body: string, geom: MicroduckEnvGeom): void {
    const side = body.startsWith('env_wall_') ? body.slice('env_wall_'.length) : ''
    if (!['px', 'nx', 'py', 'ny'].includes(side)) return
    if (geom.type !== 'box' || geom.size.length < 3) return
    void this.layoutWallAds(group, side, geom)
  }

  private async layoutWallAds(
    group: THREE.Group,
    side: string,
    geom: MicroduckEnvGeom,
  ): Promise<void> {
    const loader = new THREE.TextureLoader()
    const slots = Array.from({ length: ADS_PER_SIDE }, (_, i) => i + 1)
    const settled = await Promise.all(
      slots.map(async (n) => {
        for (const ext of AD_EXTENSIONS) {
          const url = `${AD_BASE_URL}/side_${side}_${n}.${ext}`
          try {
            const texture = await loader.loadAsync(url)
            texture.colorSpace = THREE.SRGBColorSpace
            texture.anisotropy = 4
            return { n, url, texture }
          } catch {
            // Try the next extension, then leave the slot empty.
          }
        }
        return null
      }),
    )
    const found = settled
      .flatMap((s) => (s === null ? [] : [s]))
      .sort((a, b) => a.n - b.n)
    if (this.disposed) {
      for (const { texture } of found) texture.dispose()
      return
    }
    if (found.length === 0) return

    const [sx, sy, sz] = geom.size
    const alongX = sx > sy
    const halfLen = alongX ? sx : sy
    const halfThick = alongX ? sy : sx
    // Inward face normal (arena centre is at the origin).
    const inward = new THREE.Vector3(-group.position.x, -group.position.y, 0)
    if (inward.lengthSq() < 1e-9) return
    inward.normalize()
    const yAxis = new THREE.Vector3(0, 0, 1)
    const xAxis = new THREE.Vector3().crossVectors(yAxis, inward)
    const basis = new THREE.Matrix4().makeBasis(xAxis, yAxis, inward)
    const lengthDir = alongX ? new THREE.Vector3(1, 0, 0) : new THREE.Vector3(0, 1, 0)

    const usable = halfLen * 2 - AD_EDGE_MARGIN * 2
    const slotW = usable / found.length
    const maxH = sz * 2 - AD_TOP_MARGIN * 2
    const face = new THREE.Vector3(geom.pos[0], geom.pos[1], geom.pos[2])
      .addScaledVector(inward, halfThick + AD_FACE_GAP)
    found.forEach(({ url, texture }, i) => {
      const image = texture.image as { width?: number; height?: number } | undefined
      const aspect =
        image?.width && image?.height ? image.width / image.height : 1
      // Contain-fit: full slot width unless the image is too tall — never
      // stretched, letterboxed by the wall behind it.
      const pw = Math.min(slotW * 0.94, Math.max(0.01, maxH * aspect))
      const ph = pw / Math.max(0.01, aspect)
      const mesh = new THREE.Mesh(
        this.track(new THREE.PlaneGeometry(pw, ph)),
        this.adMaterialFor(url, texture),
      )
      mesh.position
        .copy(face)
        .addScaledVector(lengthDir, -usable / 2 + slotW * (i + 0.5))
      mesh.quaternion.setFromRotationMatrix(basis)
      group.add(mesh)
      this.adTextures.push(texture)
    })
  }

  private adMaterialFor(url: string, texture: THREE.Texture): THREE.Material {
    const key = `ad:${url}`
    const cached = this.materials.get(key)
    if (cached) return cached
    // Unlit so ad artwork stays crisp under any scene lighting;
    // transparent so alpha artwork blends into the wall (no black box).
    const material = new THREE.MeshBasicMaterial({ map: texture, transparent: true })
    this.materials.set(key, material)
    return material
  }

  private buildEnvGeom(geom: MicroduckEnvGeom): THREE.Object3D | null {
    let geometry: THREE.BufferGeometry | null = null
    if (geom.type === 'box' && geom.size.length >= 3) {
      geometry = this.track(
        new THREE.BoxGeometry(geom.size[0] * 2, geom.size[1] * 2, geom.size[2] * 2),
      )
    } else if (geom.type === 'sphere' && geom.size.length >= 1) {
      geometry = this.track(new THREE.SphereGeometry(geom.size[0], 28, 20))
    } else if (geom.type === 'cylinder' && geom.size.length >= 2) {
      // MuJoCo cylinders run along Z; three's run along Y.
      const cylinder = new THREE.CylinderGeometry(geom.size[0], geom.size[0], geom.size[1] * 2, 20)
      cylinder.rotateX(Math.PI / 2)
      geometry = this.track(cylinder)
    }
    if (!geometry) return null
    const mesh = new THREE.Mesh(geometry, this.materialFor(geom.rgba, geom.role))
    mesh.position.set(geom.pos[0], geom.pos[1], geom.pos[2])
    mujocoQuatToThree(geom.quat, mesh.quaternion)
    mesh.castShadow = true
    mesh.receiveShadow = true
    if (geom.role === 'crate') {
      const edges = new THREE.LineSegments(
        this.track(new THREE.EdgesGeometry(geometry)),
        new THREE.LineBasicMaterial({ color: CRATE_EDGE }),
      )
      mesh.add(edges)
    }
    return mesh
  }

  /**
   * Fetch every unique STL and attach meshes to their body groups.
   * Resolves with the filenames that failed (failed placements get a
   * marker sphere so the pose stays readable).
   */
  async load(): Promise<{ failed: string[] }> {
    const loader = new STLLoader()
    const files = [...new Set(this.model.visual_geoms.map((g) => g.file))]
    const total = files.length
    let loaded = 0
    const failed: string[] = []
    const geometries = new Map<string, THREE.BufferGeometry>()
    const report = () => this.options.onProgress?.(loaded, total)

    await Promise.all(
      files.map(async (file) => {
        if (this.disposed) return
        try {
          const response = await fetch(this.options.assetUrl(file))
          if (!response.ok) throw new Error(`HTTP ${response.status}`)
          const buffer = await response.arrayBuffer()
          const geometry = loader.parse(buffer)
          geometry.computeVertexNormals()
          this.geometries.push(geometry)
          geometries.set(file, geometry)
        } catch {
          failed.push(file)
        } finally {
          loaded += 1
          report()
        }
      }),
    )
    if (this.disposed) return { failed }

    const fallbackGeometry = this.track(new THREE.SphereGeometry(0.007, 12, 10))
    const fallbackMaterial = new THREE.MeshStandardMaterial({ color: FALLBACK_COLOR, roughness: 0.5 })
    this.materials.set('__fallback__', fallbackMaterial)

    for (const placement of this.model.visual_geoms) {
      const group = this.groupFor(placement.body)
      const geometry = geometries.get(placement.file)
      if (!geometry) {
        const marker = new THREE.Mesh(fallbackGeometry, fallbackMaterial)
        marker.position.set(placement.pos[0], placement.pos[1], placement.pos[2])
        marker.userData.dragId = 'duck'
        group.add(marker)
        continue
      }
      const mesh = new THREE.Mesh(geometry, this.materialFor(placement.rgba, 'duck'))
      mesh.userData.dragId = 'duck'
      mesh.position.set(placement.pos[0], placement.pos[1], placement.pos[2])
      mujocoQuatToThree(placement.quat, mesh.quaternion)
      mesh.castShadow = true
      mesh.receiveShadow = false
      group.add(mesh)
    }
    return { failed }
  }

  /** Pose every known body group from one telemetry frame (or the preview). */
  /**
   * Retarget live body groups from a telemetry sample. Poses apply through
   * the render loop's easing (see start()), so 20 Hz samples render as
   * continuous motion. First sightings and teleports snap immediately.
   */
  sync(poses: BodyPose[]): void {
    for (const pose of poses) {
      const group = this.bodyGroups.get(pose.body)
      if (!group) continue
      let target = this.targets.get(pose.body)
      if (!target) {
        target = { pos: new THREE.Vector3(), quat: new THREE.Quaternion() }
        this.targets.set(pose.body, target)
      }
      target.pos.set(pose.pos[0], pose.pos[1], pose.pos[2])
      mujocoQuatToThree(pose.quat, target.quat)
      if (
        !this.settledBodies.has(pose.body) ||
        group.position.distanceTo(target.pos) > SNAP_DISTANCE_M
      ) {
        group.position.copy(target.pos)
        group.quaternion.copy(target.quat)
        this.settledBodies.add(pose.body)
      }
    }
  }

  /** Replace the working scene layout (Arrange mode preview state). */
  setLayout(layout: SimLayout): void {
    this.layout = {
      duck: layout.duck ? { ...layout.duck } : null,
      props: Object.fromEntries(
        Object.entries(layout.props).map(([body, pos]) => [body, { ...pos }]),
      ),
    }
  }

  /**
   * Draw the STAND preview under the working layout: the whole duck moves
   * as one rigid body to its spawn, props sit on their arranged spots.
   * Live telemetry never goes through here — it always wins via sync().
   */
  syncPreview(): void {
    const duck = this.layout.duck
    if (!duck) {
      this.sync(this.model.default_poses)
    } else {
      const cos = Math.cos(duck.yaw)
      const sin = Math.sin(duck.yaw)
      const yawQ = new THREE.Quaternion(0, 0, Math.sin(duck.yaw / 2), Math.cos(duck.yaw / 2))
      const local = new THREE.Vector3()
      const quat = new THREE.Quaternion()
      for (const pose of this.model.default_poses) {
        const group = this.bodyGroups.get(pose.body)
        if (!group) continue
        local.set(
          pose.pos[0] * cos - pose.pos[1] * sin + duck.x,
          pose.pos[0] * sin + pose.pos[1] * cos + duck.y,
          pose.pos[2],
        )
        group.position.copy(local)
        mujocoQuatToThree(pose.quat, quat)
        group.quaternion.copy(yawQ).multiply(quat)
      }
    }
    for (const env of this.model.environment) {
      const group = this.bodyGroups.get(env.body)
      if (!group) continue
      const spot = this.layout.props[env.body]
      group.position.set(spot?.x ?? env.pos[0], spot?.y ?? env.pos[1], env.pos[2])
      group.quaternion.identity()
    }
    // Preview places groups directly; pull the easing targets along so the
    // render loop never drifts the preview back toward stale live poses.
    for (const [body, group] of this.bodyGroups) {
      let target = this.targets.get(body)
      if (!target) {
        target = { pos: new THREE.Vector3(), quat: new THREE.Quaternion() }
        this.targets.set(body, target)
      }
      target.pos.copy(group.position)
      target.quat.copy(group.quaternion)
    }
  }

  /**
   * Idle showcase spin: slowly orbit the camera until real work starts.
   * Honors prefers-reduced-motion (never spins there) and never persists:
   * the 'end' listener only fires after user gestures, not programmatic
   * moves, so the saved viewpoint survives the showcase.
   */
  setAutoOrbit(enabled: boolean): void {
    const calm = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    this.controls.autoRotate = enabled && !calm
    this.controls.autoRotateSpeed = AUTO_ORBIT_SPEED
  }

  /**
   * Enable Arrange dragging (handler receives every layout change) or pass
   * null to return to orbit-only navigation.
   */
  setArrange(onChange: LayoutChangeHandler | null): void {
    this.arrangeCb = onChange
    this.dragId = null
    this.controls.enabled = true
    this.renderer.domElement.style.cursor = ''
  }

  private pickDraggable(event: PointerEvent): string | null {
    const rect = this.renderer.domElement.getBoundingClientRect()
    this.pointerNdc.set(
      ((event.clientX - rect.left) / rect.width) * 2 - 1,
      -((event.clientY - rect.top) / rect.height) * 2 + 1,
    )
    this.raycaster.setFromCamera(this.pointerNdc, this.camera)
    const hits = this.raycaster.intersectObjects(this.mujocoFrame.children, true)
    for (const hit of hits) {
      let node: THREE.Object3D | null = hit.object
      while (node && !node.userData.dragId) node = node.parent
      if (node?.userData.dragId) return node.userData.dragId as string
    }
    return null
  }

  private floorPoint(event: PointerEvent): { x: number; y: number } | null {
    const rect = this.renderer.domElement.getBoundingClientRect()
    this.pointerNdc.set(
      ((event.clientX - rect.left) / rect.width) * 2 - 1,
      -((event.clientY - rect.top) / rect.height) * 2 + 1,
    )
    this.raycaster.setFromCamera(this.pointerNdc, this.camera)
    if (!this.raycaster.ray.intersectPlane(this.dragPlane, this.planeHit)) return null
    // Three floor (x, z) back to MuJoCo floor (x, y).
    const clamp = (v: number) => Math.max(-LAYOUT_LIMIT, Math.min(LAYOUT_LIMIT, v))
    return { x: clamp(this.planeHit.x), y: clamp(-this.planeHit.z) }
  }

  private onPointerDown = (event: PointerEvent): void => {
    if (!this.arrangeCb || event.button !== 0) return
    const hit = this.pickDraggable(event)
    if (!hit) return
    event.stopPropagation()
    event.preventDefault()
    this.dragId = hit
    this.dragTurn = event.altKey && hit === 'duck'
    this.controls.enabled = false
    this.renderer.domElement.style.cursor = 'grabbing'
    window.addEventListener('pointermove', this.onDragMove)
    window.addEventListener('pointerup', this.onDragEnd, { once: true })
  }

  private onDragMove = (event: PointerEvent): void => {
    if (!this.arrangeCb || !this.dragId) return
    const point = this.floorPoint(event)
    if (!point) return
    if (this.dragId === 'duck') {
      const center = this.layout.duck ?? { x: 0, y: 0, yaw: 0 }
      if (this.dragTurn) {
        const dx = point.x - center.x
        const dy = point.y - center.y
        if (Math.hypot(dx, dy) < YAW_DEADZONE) return
        this.layout.duck = { x: center.x, y: center.y, yaw: Math.atan2(dy, dx) }
      } else {
        this.layout.duck = { x: point.x, y: point.y, yaw: center.yaw }
      }
    } else {
      this.layout.props[this.dragId] = { x: point.x, y: point.y }
    }
    this.syncPreview()
    this.arrangeCb({
      duck: this.layout.duck ? { ...this.layout.duck } : null,
      props: Object.fromEntries(
        Object.entries(this.layout.props).map(([body, pos]) => [body, { ...pos }]),
      ),
    })
  }

  private onDragEnd = (): void => {
    this.dragId = null
    this.dragTurn = false
    this.controls.enabled = true
    this.renderer.domElement.style.cursor = ''
    window.removeEventListener('pointermove', this.onDragMove)
  }

  private onHover = (event: PointerEvent): void => {
    if (!this.arrangeCb || this.dragId) return
    this.renderer.domElement.style.cursor = this.pickDraggable(event) ? 'grab' : ''
  }

  private onHoverEnd = (): void => {
    if (!this.dragId) this.renderer.domElement.style.cursor = ''
  }

  resetCamera(): void {
    this.camera.position.set(0.85, 0.55, 0.95)
    this.controls.target.set(0.15, 0.08, 0)
    this.controls.update()
  }

  private persistCamera = (): void => {
    saveCamera('microduck', this.camera.position, this.controls.target)
  }

  private restoreCamera(): void {
    const saved = loadSavedCamera('microduck')
    if (!saved) return
    this.camera.position.set(...saved.position)
    this.controls.target.set(...saved.target)
    this.controls.update()
  }

  start(): void {
    if (this.running) return
    this.running = true
    this.lastFrameNow = 0
    const loop = (now: number) => {
      if (!this.running) return
      this.rafId = requestAnimationFrame(loop)
      if (this.lastFrameNow > 0) {
        // Frame-rate-independent damping toward the latest telemetry pose.
        const dt = Math.min((now - this.lastFrameNow) / 1000, 0.1)
        const k = 1 - Math.exp(-dt * SMOOTHING_RATE)
        if (k > 0.0001) {
          for (const [body, target] of this.targets) {
            const group = this.bodyGroups.get(body)
            if (!group) continue
            group.position.lerp(target.pos, k)
            group.quaternion.slerp(target.quat, k)
          }
        }
      }
      this.lastFrameNow = now
      this.controls.update()
      this.renderer.render(this.scene, this.camera)
    }
    this.rafId = requestAnimationFrame(loop)
  }

  stop(): void {
    this.running = false
    cancelAnimationFrame(this.rafId)
  }

  resize(): void {
    const rect = this.container.getBoundingClientRect()
    const w = Math.max(rect.width, 50)
    const h = Math.max(rect.height, 50)
    this.camera.aspect = w / h
    this.camera.updateProjectionMatrix()
    this.renderer.setSize(w, h)
  }

  dispose(): void {
    this.disposed = true
    this.stop()
    this.resizeObserver.disconnect()
    this.container.removeEventListener('pointerdown', this.onPointerDown, true)
    this.container.removeEventListener('pointermove', this.onHover)
    this.container.removeEventListener('pointerleave', this.onHoverEnd)
    window.removeEventListener('pointermove', this.onDragMove)
    window.removeEventListener('pointerup', this.onDragEnd)
    this.controls.removeEventListener('end', this.persistCamera)
    this.controls.dispose()
    for (const geometry of this.geometries) geometry.dispose()
    for (const material of this.materials.values()) material.dispose()
    this.soccerTexture?.dispose()
    for (const texture of this.adTextures) texture.dispose()
    this.renderer.dispose()
    this.renderer.domElement.remove()
  }
}
