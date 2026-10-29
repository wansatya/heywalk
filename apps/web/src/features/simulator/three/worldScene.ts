import * as THREE from 'three'
import { OrbitControls } from 'three/addons/controls/OrbitControls.js'
import { loadSavedCamera, saveCamera } from './cameraState'
import type { DiffDriveWorldConfig, TelemetrySample } from '../../../api/types'
import { buildMinibot, type Minibot } from './minibot'

// Coordinate map (pure, single place): sim +x -> three +x, sim +y -> three -z,
// sim heading theta (CCW from +x) -> rotation.y. A bot built facing +X then
// points along (cos theta, 0, -sin theta), matching the sim heading vector.
export function simToThree(x: number, y: number, out = new THREE.Vector3()): THREE.Vector3 {
  out.set(x, 0, -y)
  return out
}

export const MAX_TRAJECTORY_POINTS = 5000
const BG_COLOR = 0x111318
const FLOOR_COLOR = 0x1b1e24
const WALL_COLOR = 0x2b303b
const OBSTACLE_COLOR = 0x3a4150
const EDGE_COLOR = 0x5b6478
const TRAJECTORY_COLOR = 0xe8b339
const GOAL_COLOR = 0x34d399
const START_COLOR = 0x7dd3fc
/**
 * Idle showcase spin (OrbitControls units: 2.0 = one orbit per ~30 s).
 * Slow enough to inspect the arena while it reads as alive.
 */
const AUTO_ORBIT_SPEED = 0.9

export class WorldScene {
  private renderer: THREE.WebGLRenderer
  private scene = new THREE.Scene()
  private camera: THREE.PerspectiveCamera
  private controls: OrbitControls
  private bot: Minibot
  private staticGroup = new THREE.Group()
  private markerGroup = new THREE.Group()
  private trajectory: THREE.Line
  private trajectoryPositions: Float32Array
  private renderedSamples = 0
  private world: DiffDriveWorldConfig
  private clock = new THREE.Clock()
  private rafId = 0
  private running = false
  private resizeObserver: ResizeObserver
  private container: HTMLElement

  constructor(container: HTMLElement, world: DiffDriveWorldConfig) {
    this.container = container
    this.world = world
    this.scene.background = new THREE.Color(BG_COLOR)
    this.scene.fog = new THREE.Fog(BG_COLOR, 30, 80)

    this.camera = new THREE.PerspectiveCamera(50, 1, 0.05, 200)

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
    this.controls.maxPolarAngle = Math.PI / 2 - 0.03
    this.controls.minDistance = 1.5
    this.controls.maxDistance = 40
    // 'end' fires after user orbit/zoom/pan gestures only, never after
    // programmatic moves, so this persists the user's view, not ours.
    this.controls.addEventListener('end', this.persistCamera)

    // Key + fill + ambient so the bot reads as a solid object.
    const hemi = new THREE.HemisphereLight(0xdfe8ff, 0x1a1d23, 0.55)
    this.scene.add(hemi)
    const key = new THREE.DirectionalLight(0xfff2df, 1.6)
    key.castShadow = true
    key.shadow.mapSize.set(2048, 2048)
    key.shadow.bias = -0.0004
    this.scene.add(key)
    this.scene.add(key.target)
    this.scene.userData.keyLight = key

    this.scene.add(this.staticGroup, this.markerGroup)

    this.trajectoryPositions = new Float32Array(MAX_TRAJECTORY_POINTS * 3)
    const trajectoryGeometry = new THREE.BufferGeometry()
    trajectoryGeometry.setAttribute('position', new THREE.BufferAttribute(this.trajectoryPositions, 3))
    trajectoryGeometry.setDrawRange(0, 0)
    this.trajectory = new THREE.Line(
      trajectoryGeometry,
      new THREE.LineBasicMaterial({ color: TRAJECTORY_COLOR }),
    )
    this.trajectory.frustumCulled = false
    this.scene.add(this.trajectory)

    this.bot = buildMinibot()
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      this.bot.setIdleEnabled(false)
    }
    this.scene.add(this.bot.group)

    this.setWorld(world)
    this.resetRun()

    this.resizeObserver = new ResizeObserver(() => this.resize())
    this.resizeObserver.observe(container)
    this.resize()
  }

  setWorld(world: DiffDriveWorldConfig): void {
    this.world = world
    this.staticGroup.clear()
    const { width, height } = world
    const cx = width / 2
    const cz = -height / 2

    // Far ground (no void at any orbit angle) + arena floor + metre grid.
    const ground = new THREE.Mesh(
      new THREE.PlaneGeometry(300, 300),
      new THREE.MeshStandardMaterial({ color: 0x0c0d11, roughness: 1 }),
    )
    ground.rotation.x = -Math.PI / 2
    ground.position.set(cx, -0.02, cz)
    ground.receiveShadow = true
    const floor = new THREE.Mesh(
      new THREE.PlaneGeometry(width, height),
      new THREE.MeshStandardMaterial({ color: FLOOR_COLOR, roughness: 0.95 }),
    )
    floor.rotation.x = -Math.PI / 2
    floor.position.set(cx, 0, cz)
    floor.receiveShadow = true
    // Metre grid clipped exactly to the arena rectangle.
    const gridPoints: number[] = []
    for (let gx = 0; gx <= width; gx += 1) gridPoints.push(gx, 0.001, 0, gx, 0.001, -height)
    for (let gy = 0; gy <= height; gy += 1) gridPoints.push(0, 0.001, -gy, width, 0.001, -gy)
    const gridGeometry = new THREE.BufferGeometry()
    gridGeometry.setAttribute('position', new THREE.BufferAttribute(new Float32Array(gridPoints), 3))
    const grid = new THREE.LineSegments(
      gridGeometry,
      new THREE.LineBasicMaterial({ color: 0x232833, transparent: true, opacity: 0.8 }),
    )
    this.staticGroup.add(ground, floor, grid)

    // Walls.
    const wallMaterial = new THREE.MeshStandardMaterial({ color: WALL_COLOR, roughness: 0.8 })
    const wallHeight = 0.5
    const wallThickness = 0.12
    const mkWall = (w: number, h: number, d: number, x: number, y: number, z: number) => {
      const wall = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), wallMaterial)
      wall.position.set(x, y, z)
      wall.castShadow = true
      wall.receiveShadow = true
      this.staticGroup.add(wall)
    }
    mkWall(width + wallThickness * 2, wallHeight, wallThickness, cx, wallHeight / 2, -height - wallThickness / 2)
    mkWall(width + wallThickness * 2, wallHeight, wallThickness, cx, wallHeight / 2, wallThickness / 2)
    mkWall(wallThickness, wallHeight, height, -wallThickness / 2, wallHeight / 2, cz)
    mkWall(wallThickness, wallHeight, height, width + wallThickness / 2, wallHeight / 2, cz)

    // Obstacles as solid blocks with edge lines.
    const obstacleMaterial = new THREE.MeshStandardMaterial({ color: OBSTACLE_COLOR, roughness: 0.75 })
    const edgeMaterial = new THREE.LineBasicMaterial({ color: EDGE_COLOR })
    for (const o of world.obstacles) {
      const h = 0.6
      const block = new THREE.Mesh(new THREE.BoxGeometry(o.w, h, o.h), obstacleMaterial)
      block.position.set(o.x + o.w / 2, h / 2, -(o.y + o.h / 2))
      block.castShadow = true
      block.receiveShadow = true
      const edges = new THREE.LineSegments(new THREE.EdgesGeometry(block.geometry), edgeMaterial)
      block.add(edges)
      this.staticGroup.add(block)
    }

    // Start disc.
    const start = new THREE.Mesh(
      new THREE.CircleGeometry(0.14, 32),
      new THREE.MeshBasicMaterial({ color: START_COLOR, transparent: true, opacity: 0.85 }),
    )
    start.rotation.x = -Math.PI / 2
    start.position.set(world.start.x, 0.005, -world.start.y)
    this.staticGroup.add(start)

    // Goal: translucent disc + ring + beacon pole so it reads from any angle.
    const goalDisc = new THREE.Mesh(
      new THREE.CircleGeometry(world.goal.radius, 48),
      new THREE.MeshBasicMaterial({ color: GOAL_COLOR, transparent: true, opacity: 0.18 }),
    )
    goalDisc.rotation.x = -Math.PI / 2
    goalDisc.position.set(world.goal.x, 0.004, -world.goal.y)
    const goalRing = new THREE.Mesh(
      new THREE.RingGeometry(world.goal.radius - 0.03, world.goal.radius, 48),
      new THREE.MeshBasicMaterial({ color: GOAL_COLOR, side: THREE.DoubleSide }),
    )
    goalRing.rotation.x = -Math.PI / 2
    goalRing.position.set(world.goal.x, 0.006, -world.goal.y)
    const beacon = new THREE.Mesh(
      new THREE.CylinderGeometry(0.02, 0.02, 1.2, 12),
      new THREE.MeshStandardMaterial({
        color: GOAL_COLOR,
        emissive: GOAL_COLOR,
        emissiveIntensity: 0.9,
        transparent: true,
        opacity: 0.55,
      }),
    )
    beacon.position.set(world.goal.x, 0.6, -world.goal.y)
    this.staticGroup.add(goalDisc, goalRing, beacon)

    // Fit the key light's shadow frustum to the arena.
    const key = this.scene.userData.keyLight as THREE.DirectionalLight
    const span = Math.max(width, height)
    key.position.set(cx + span * 0.5, span * 0.9, cz + span * 0.45)
    key.target.position.set(cx, 0, cz)
    key.shadow.camera.left = -span * 0.75
    key.shadow.camera.right = span * 0.75
    key.shadow.camera.top = span * 0.75
    key.shadow.camera.bottom = -span * 0.75
    key.shadow.camera.far = span * 3
    key.shadow.camera.updateProjectionMatrix()

    this.controls.target.set(cx, 0, cz)
    this.resetCamera()
    // Rebuilding the world must not steal the user's viewpoint: a new run
    // starts from the saved view unless it was explicitly reset.
    this.restoreCamera()
  }

  resetRun(): void {
    this.renderedSamples = 0
    this.trajectory.geometry.setDrawRange(0, 0)
    ;(this.trajectory.geometry.getAttribute('position') as THREE.BufferAttribute).needsUpdate = true
    this.markerGroup.clear()
    const { start } = this.world
    this.placeBot(start.x, start.y, start.theta)
  }

  /** Reconcile the scene with the full telemetry array (handles reset/shrink). */
  sync(samples: TelemetrySample[]): void {
    if (samples.length < this.renderedSamples) this.resetRun()
    const position = this.trajectory.geometry.getAttribute('position') as THREE.BufferAttribute
    let prev: TelemetrySample | null =
      this.renderedSamples > 0 ? samples[this.renderedSamples - 1] : null
    for (let i = this.renderedSamples; i < samples.length && i < MAX_TRAJECTORY_POINTS; i++) {
      const s = samples[i]
      simToThree(s.x, s.y, WorldScene.scratch).setY(0.02)
      this.trajectoryPositions[i * 3] = WorldScene.scratch.x
      this.trajectoryPositions[i * 3 + 1] = WorldScene.scratch.y
      this.trajectoryPositions[i * 3 + 2] = WorldScene.scratch.z
      if (prev) {
        const dist = Math.hypot(s.x - prev.x, s.y - prev.y)
        this.bot.stride(dist, Math.abs(s.v))
      }
      if (s.collision) this.addCollisionMarker(s.x, s.y)
      prev = s
    }
    this.renderedSamples = Math.min(samples.length, MAX_TRAJECTORY_POINTS)
    this.trajectory.geometry.setDrawRange(0, this.renderedSamples)
    position.needsUpdate = true
    const latest = samples[samples.length - 1]
    if (latest) {
      this.placeBot(latest.x, latest.y, latest.theta)
      if (latest.collision) this.bot.setCollisionFlash()
    }
  }

  private static scratch = new THREE.Vector3()

  private placeBot(x: number, y: number, theta: number): void {
    simToThree(x, y, WorldScene.scratch)
    this.bot.group.position.set(WorldScene.scratch.x, 0, WorldScene.scratch.z)
    this.bot.group.rotation.y = theta
  }

  private addCollisionMarker(x: number, y: number): void {
    const marker = new THREE.Mesh(
      new THREE.RingGeometry(0.12, 0.2, 24),
      new THREE.MeshBasicMaterial({ color: 0xf87171, side: THREE.DoubleSide }),
    )
    marker.rotation.x = -Math.PI / 2
    marker.position.set(x, 0.01, -y)
    this.markerGroup.add(marker)
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

  focusRobot(): void {
    const target = this.bot.group.position.clone()
    this.controls.target.copy(target)
    this.camera.position.set(target.x + 0.9, target.y + 0.75, target.z + 0.9)
    this.controls.update()
  }

  resetCamera(): void {
    const { width, height } = this.world
    const span = Math.max(width, height)
    // Framed close enough that the ~0.27 m robot reads clearly; the user
    // can still zoom out to the full arena.
    this.camera.position.set(width / 2 + span * 0.22, span * 0.62, -height / 2 + span * 0.68)
    this.controls.target.set(width / 2 - 0.6, 0, -height / 2)
    this.controls.update()
  }

  private persistCamera = (): void => {
    saveCamera('world', this.camera.position, this.controls.target)
  }

  private restoreCamera(): void {
    const saved = loadSavedCamera('world')
    if (!saved) return
    this.camera.position.set(...saved.position)
    this.controls.target.set(...saved.target)
    this.controls.update()
  }

  start(): void {
    if (this.running) return
    this.running = true
    this.clock.start()
    const loop = () => {
      if (!this.running) return
      this.rafId = requestAnimationFrame(loop)
      const dt = Math.min(this.clock.getDelta(), 0.1)
      this.controls.update()
      this.bot.update(dt)
      this.renderer.render(this.scene, this.camera)
    }
    loop()
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
    this.stop()
    this.resizeObserver.disconnect()
    this.controls.removeEventListener('end', this.persistCamera)
    this.controls.dispose()
    this.bot.dispose()
    this.scene.traverse((child) => {
      if (child instanceof THREE.Mesh || child instanceof THREE.Line) {
        child.geometry.dispose()
        const material = child.material as THREE.Material | THREE.Material[]
        if (Array.isArray(material)) material.forEach((m) => m.dispose())
        else material.dispose()
      }
    })
    this.renderer.dispose()
    this.renderer.domElement.remove()
  }
}
