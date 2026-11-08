import { useEffect, useRef, useState } from 'react'
import * as THREE from 'three'
import { OrbitControls } from 'three/addons/controls/OrbitControls.js'
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js'
import { STLLoader } from 'three/addons/loaders/STLLoader.js'
import type { FoundryComponent, FoundryJoint } from '../../api/types'

/** World position of every component, in MuJoCo coordinates. */
function worldPositions(components: FoundryComponent[]): Map<string, THREE.Vector3> {
  const byId = new Map(components.map((component) => [component.id, component]))
  const resolved = new Map<string, THREE.Vector3>()
  const resolving = new Set<string>()

  const resolve = (id: string): THREE.Vector3 => {
    const existing = resolved.get(id)
    if (existing) return existing
    const component = byId.get(id)
    if (!component) return new THREE.Vector3()
    if (resolving.has(id)) return new THREE.Vector3()
    resolving.add(id)
    const placement = component.metadata.placement ?? {}
    const local = component.parent_id
      ? (placement.body_pos ?? [0, 0, 0])
      : (placement.world_center ?? placement.body_pos ?? [0, 0, 0])
    const vector = new THREE.Vector3(
      Number(local[0] ?? 0),
      Number(local[1] ?? 0),
      Number(local[2] ?? 0),
    )
    if (component.parent_id) vector.add(resolve(component.parent_id))
    resolving.delete(id)
    resolved.set(id, vector)
    return vector
  }

  for (const component of components) resolve(component.id)
  return resolved
}

/** Box placeholder for a component without a reconstructed preview mesh. */
function placeholderFor(component: FoundryComponent): THREE.Object3D {
  const dims = component.metadata.dimensions ?? [0.1, 0.1, 0.1]
  const geometry = new THREE.BoxGeometry(
    Math.max(0.01, Number(dims[0] ?? 0.1)),
    Math.max(0.01, Number(dims[2] ?? 0.1)),
    Math.max(0.01, Number(dims[1] ?? 0.1)),
  )
  const material = new THREE.MeshStandardMaterial({
    color: 0xd6d3d1,
    roughness: 0.75,
    metalness: 0.05,
    transparent: true,
    opacity: 0.55,
  })
  return new THREE.Mesh(geometry, material)
}

function disposeObject(object: THREE.Object3D) {
  object.traverse((child) => {
    const mesh = child as THREE.Mesh
    mesh.geometry?.dispose()
    const material = mesh.material
    if (Array.isArray(material)) material.forEach((entry) => entry.dispose())
    else material?.dispose()
  })
}

function setWireframe(object: THREE.Object3D, enabled: boolean) {
  object.traverse((child) => {
    const mesh = child as THREE.Mesh
    if (!mesh.isMesh) return
    const materials = Array.isArray(mesh.material) ? mesh.material : [mesh.material]
    for (const material of materials) {
      if (material && 'wireframe' in material) {
        ;(material as THREE.MeshStandardMaterial).wireframe = enabled
      }
    }
  })
}

const JOINT_COLORS: Record<string, number> = {
  hinge: 0xf59e0b,
  slide: 0x0ea5e9,
  fixed: 0xa8a29e,
}

class FoundryScene {
  private readonly container: HTMLDivElement
  private readonly renderer: THREE.WebGLRenderer
  private readonly scene = new THREE.Scene()
  private readonly camera = new THREE.PerspectiveCamera(42, 1, 0.01, 100)
  private readonly controls: OrbitControls
  private readonly robotFrame = new THREE.Group()
  private readonly jointGroup = new THREE.Group()
  private readonly objects = new Map<string, THREE.Object3D>()
  private readonly collisionObjects = new Map<string, THREE.Object3D>()
  private readonly loader = new GLTFLoader()
  private readonly stlLoader = new STLLoader()
  private readonly raycaster = new THREE.Raycaster()
  private readonly pointer = new THREE.Vector2()
  private selectedId: string | null = null
  private components: FoundryComponent[] = []
  private joints: FoundryJoint[] = []
  private wireframe = false
  private showCollision = false
  private showJoints = true
  private boxHelper: THREE.BoxHelper | null = null
  private frame = 0
  private disposed = false
  private readonly resizeObserver: ResizeObserver
  private readonly onSelect: (id: string | null) => void

  constructor(container: HTMLDivElement, onSelect: (id: string | null) => void) {
    this.container = container
    this.onSelect = onSelect
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true })
    this.renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1))
    this.renderer.setSize(container.clientWidth || 1, container.clientHeight || 1)
    this.renderer.outputColorSpace = THREE.SRGBColorSpace
    container.appendChild(this.renderer.domElement)

    this.scene.background = new THREE.Color(0xf7f5f0)
    this.scene.fog = new THREE.Fog(0xf7f5f0, 6, 14)
    this.camera.position.set(1.9, 1.5, 2.4)
    this.camera.lookAt(0, 0.8, 0)

    this.controls = new OrbitControls(this.camera, this.renderer.domElement)
    this.controls.target.set(0, 0.8, 0)
    this.controls.enableDamping = true
    this.controls.dampingFactor = 0.08
    this.controls.minDistance = 0.5
    this.controls.maxDistance = 8
    this.controls.maxPolarAngle = Math.PI / 2 - 0.02

    const hemi = new THREE.HemisphereLight(0xffffff, 0xbcb8b0, 1.7)
    this.scene.add(hemi)
    const key = new THREE.DirectionalLight(0xffffff, 2.0)
    key.position.set(2.5, 4, 2)
    key.castShadow = true
    this.scene.add(key)
    const fill = new THREE.DirectionalLight(0xf3e7d3, 0.8)
    fill.position.set(-3, 2, -2)
    this.scene.add(fill)

    const ground = new THREE.Mesh(
      new THREE.PlaneGeometry(12, 12),
      new THREE.MeshStandardMaterial({ color: 0xe7e2d8, roughness: 1 }),
    )
    ground.rotation.x = -Math.PI / 2
    ground.receiveShadow = true
    this.scene.add(ground)
    const grid = new THREE.GridHelper(12, 24, 0xb8b2a7, 0xd8d3c9)
    this.scene.add(grid)

    // MuJoCo is z-up; rotate the robot frame so raw placement coordinates
    // (x forward, y left, z up) render upright in three's y-up world.
    this.robotFrame.rotation.x = -Math.PI / 2
    this.scene.add(this.robotFrame)
    this.robotFrame.add(this.jointGroup)

    this.resizeObserver = new ResizeObserver(() => this.resize())
    this.resizeObserver.observe(container)
    this.renderer.domElement.addEventListener('pointerdown', this.handlePointer)
    this.animate()
  }

  private resize() {
    const width = this.container.clientWidth || 1
    const height = this.container.clientHeight || 1
    this.renderer.setSize(width, height, false)
    this.camera.aspect = width / height
    this.camera.updateProjectionMatrix()
  }

  private animate = () => {
    if (this.disposed) return
    this.frame = requestAnimationFrame(this.animate)
    this.controls.update()
    this.boxHelper?.update()
    this.renderer.render(this.scene, this.camera)
  }

  private handlePointer = (event: PointerEvent) => {
    const rect = this.renderer.domElement.getBoundingClientRect()
    this.pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1
    this.pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1
    this.raycaster.setFromCamera(this.pointer, this.camera)
    const hits = this.raycaster.intersectObjects([...this.objects.values()], true)
    let found: string | null = null
    for (const hit of hits) {
      let cursor: THREE.Object3D | null = hit.object
      while (cursor && !found) {
        const id = cursor.userData.foundryComponentId as string | undefined
        if (id) found = id
        cursor = cursor.parent
      }
      if (found) break
    }
    this.onSelect(found)
  }

  private clearObjects() {
    for (const object of this.objects.values()) {
      this.robotFrame.remove(object)
      disposeObject(object)
    }
    this.objects.clear()
    for (const object of this.collisionObjects.values()) {
      this.robotFrame.remove(object)
      disposeObject(object)
    }
    this.collisionObjects.clear()
    for (const child of [...this.jointGroup.children]) {
      this.jointGroup.remove(child)
      disposeObject(child)
    }
    if (this.boxHelper) {
      this.scene.remove(this.boxHelper)
      this.boxHelper.geometry.dispose()
      this.boxHelper = null
    }
  }

  setComponents(components: FoundryComponent[], joints: FoundryJoint[]) {
    if (this.disposed) return
    this.clearObjects()
    this.components = components
    this.joints = joints
    const positions = worldPositions(components)
    for (const component of components) {
      const position = positions.get(component.id) ?? new THREE.Vector3()
      const object = component.preview_asset_url
        ? new THREE.Group()
        : placeholderFor(component)
      object.userData.foundryComponentId = component.id
      object.position.copy(position)
      object.visible = component.metadata.visible !== false
      this.robotFrame.add(object)
      this.objects.set(component.id, object)
      if (component.preview_asset_url) {
        const url = component.preview_asset_url
        this.loader.load(
          url,
          (gltf) => {
            if (this.disposed) return
            const model = gltf.scene
            model.userData.foundryComponentId = component.id
            model.traverse((child) => {
              child.userData.foundryComponentId = component.id
              const mesh = child as THREE.Mesh
              if (mesh.isMesh) {
                mesh.castShadow = true
                mesh.receiveShadow = true
              }
            })
            if (this.wireframe) setWireframe(model, true)
            object.add(model)
          },
          undefined,
          () => {
            // A missing preview must not hide the part: show its placeholder.
            if (this.disposed || !object.parent) return
            const fallback = placeholderFor(component)
            fallback.userData.foundryComponentId = component.id
            if (this.wireframe) setWireframe(fallback, true)
            object.add(fallback)
          },
        )
      }
    }
    this.rebuildJoints(positions)
    if (this.showCollision) this.loadCollisionMeshes(positions)
    else this.applyVisibility()
    this.setSelected(this.selectedId)
  }

  setOptions(options: { wireframe?: boolean; collision?: boolean; joints?: boolean }) {
    if (this.disposed) return
    if (options.wireframe !== undefined) {
      this.wireframe = options.wireframe
      for (const object of this.objects.values()) setWireframe(object, this.wireframe)
      for (const object of this.collisionObjects.values()) setWireframe(object, this.wireframe)
    }
    if (options.collision !== undefined) {
      this.showCollision = options.collision
      if (this.showCollision) {
        this.loadCollisionMeshes(worldPositions(this.components))
      } else {
        this.applyVisibility()
      }
    }
    if (options.joints !== undefined) {
      this.showJoints = options.joints
      this.jointGroup.visible = this.showJoints
    }
  }

  /** Load each part's real collision STL; visuals stay until it arrives. */
  private loadCollisionMeshes(positions: Map<string, THREE.Vector3>) {
    for (const component of this.components) {
      if (!component.collision_asset_url || this.collisionObjects.has(component.id)) continue
      const url = component.collision_asset_url
      const position = positions.get(component.id)?.clone() ?? new THREE.Vector3()
      this.stlLoader.load(
        url,
        (geometry) => {
          if (this.disposed) return
          if (!this.objects.has(component.id) || this.collisionObjects.has(component.id)) {
            geometry.dispose()
            return
          }
          const material = new THREE.MeshStandardMaterial({
            color: 0xef4444,
            roughness: 0.6,
            metalness: 0.05,
            transparent: true,
            opacity: 0.85,
          })
          const mesh = new THREE.Mesh(geometry, material)
          mesh.userData.foundryComponentId = component.id
          mesh.position.copy(position)
          if (this.wireframe) setWireframe(mesh, true)
          this.robotFrame.add(mesh)
          this.collisionObjects.set(component.id, mesh)
          this.applyVisibility()
        },
        undefined,
        () => {
          // A missing STL keeps the visual mesh; nothing to hide.
        },
      )
    }
    this.applyVisibility()
  }

  private applyVisibility() {
    for (const component of this.components) {
      const shown = component.metadata.visible !== false
      const visual = this.objects.get(component.id)
      const collision = this.collisionObjects.get(component.id)
      if (visual) visual.visible = shown && !(this.showCollision && collision)
      if (collision) collision.visible = shown && this.showCollision
    }
  }

  private rebuildJoints(positions: Map<string, THREE.Vector3>) {
    for (const child of [...this.jointGroup.children]) {
      this.jointGroup.remove(child)
      disposeObject(child)
    }
    this.jointGroup.visible = this.showJoints
    if (this.joints.length === 0) return
    const avgDim =
      this.components.reduce(
        (sum, component) =>
          sum + Math.max(0.02, ...(component.metadata.dimensions ?? [0.1])),
        0,
      ) / Math.max(1, this.components.length)
    const radius = Math.min(0.02, Math.max(0.004, avgDim * 0.03))
    const axisLength = Math.min(0.12, Math.max(0.03, avgDim * 0.18))
    const sphere = new THREE.SphereGeometry(radius, 16, 12)
    for (const joint of this.joints) {
      const base = positions.get(joint.component_id)
      if (!base) continue
      const anchor = new THREE.Vector3(
        base.x + Number(joint.position?.[0] ?? 0),
        base.y + Number(joint.position?.[1] ?? 0),
        base.z + Number(joint.position?.[2] ?? 0),
      )
      const color = JOINT_COLORS[joint.joint_type] ?? 0xf59e0b
      const marker = new THREE.Mesh(
        sphere,
        new THREE.MeshStandardMaterial({ color, roughness: 0.4, metalness: 0.1 }),
      )
      marker.position.copy(anchor)
      this.jointGroup.add(marker)
      if (joint.joint_type === 'hinge' || joint.joint_type === 'slide') {
        const axis = new THREE.Vector3(
          Number(joint.axis?.[0] ?? 0),
          Number(joint.axis?.[1] ?? 0),
          Number(joint.axis?.[2] ?? 1),
        )
        if (axis.lengthSq() > 1e-6) {
          axis.normalize()
          const arrow = new THREE.ArrowHelper(axis, anchor, axisLength, color, axisLength * 0.35, axisLength * 0.2)
          this.jointGroup.add(arrow)
        }
      }
    }
  }

  setSelected(id: string | null) {
    this.selectedId = id
    if (this.boxHelper) {
      this.scene.remove(this.boxHelper)
      this.boxHelper.geometry.dispose()
      this.boxHelper = null
    }
    if (!id) return
    const object =
      (this.showCollision && this.collisionObjects.get(id)) || this.objects.get(id)
    if (!object || !object.visible) return
    this.boxHelper = new THREE.BoxHelper(object, 0xf59e0b)
    this.scene.add(this.boxHelper)
  }

  resetCamera() {
    this.camera.position.set(1.9, 1.5, 2.4)
    this.controls.target.set(0, 0.8, 0)
    this.controls.update()
  }

  dispose() {
    this.disposed = true
    cancelAnimationFrame(this.frame)
    this.resizeObserver.disconnect()
    this.renderer.domElement.removeEventListener('pointerdown', this.handlePointer)
    this.clearObjects()
    this.controls.dispose()
    this.renderer.dispose()
    this.renderer.domElement.remove()
  }
}

export function FoundryViewport({
  components,
  joints,
  selectedId,
  onSelect,
  className,
}: {
  components: FoundryComponent[]
  joints: FoundryJoint[]
  selectedId: string | null
  onSelect: (id: string | null) => void
  className?: string
}) {
  const containerRef = useRef<HTMLDivElement>(null)
  const sceneRef = useRef<FoundryScene | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [wireframe, setWireframe] = useState(false)
  const [collision, setCollision] = useState(false)
  const [showJoints, setShowJoints] = useState(true)

  useEffect(() => {
    const container = containerRef.current
    if (!container) return
    let scene: FoundryScene | null = null
    try {
      scene = new FoundryScene(container, onSelect)
      sceneRef.current = scene
    } catch (err) {
      setError(err instanceof Error ? err.message : 'WebGL is unavailable.')
    }
    return () => {
      scene?.dispose()
      sceneRef.current = null
    }
    // onSelect is intentionally not a dependency: the scene keeps the first
    // handler and the parent passes a stable state setter.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    sceneRef.current?.setComponents(components, joints)
  }, [components, joints])

  useEffect(() => {
    sceneRef.current?.setSelected(selectedId)
  }, [selectedId])

  useEffect(() => {
    sceneRef.current?.setOptions({ wireframe, collision, joints: showJoints })
  }, [wireframe, collision, showJoints])

  if (error) {
    return (
      <div className={`flex items-center justify-center p-6 text-center ${className ?? ''}`}>
        <p className="max-w-xs text-xs leading-relaxed text-stone-500">
          3D preview unavailable ({error}). The generated meshes and MJCF are still available in
          the package.
        </p>
      </div>
    )
  }

  const toggleClass = (active: boolean) =>
    `rounded-lg border px-2.5 py-1 text-[11px] font-medium shadow-sm backdrop-blur transition-colors ${
      active
        ? 'border-amber-300 bg-amber-100 text-amber-800'
        : 'border-border-subtle/70 bg-white/90 text-stone-600 hover:bg-white'
    }`

  return (
    <div className={`relative ${className ?? ''}`}>
      <div ref={containerRef} className="h-full w-full" role="img" aria-label="Foundry robot 3D preview" />
      <div className="pointer-events-none absolute left-3 top-3 flex flex-wrap gap-2">
        <span className="rounded-lg bg-stone-900/80 px-2.5 py-1 text-[11px] font-semibold text-white shadow-md backdrop-blur">
          {components.length} part{components.length === 1 ? '' : 's'}
        </span>
        {components.some((component) => component.metadata.physics?.estimated !== false) && (
          <span className="rounded-lg bg-amber-500 px-2.5 py-1 text-[11px] font-semibold text-white shadow-md">
            estimated physics
          </span>
        )}
        {collision && (
          <span className="rounded-lg bg-red-500 px-2.5 py-1 text-[11px] font-semibold text-white shadow-md">
            collision meshes
          </span>
        )}
      </div>
      <div className="absolute bottom-3 left-3 flex gap-1.5">
        <button
          type="button"
          onClick={() => setWireframe((value) => !value)}
          title="Toggle wireframe rendering"
          aria-pressed={wireframe}
          className={toggleClass(wireframe)}
        >
          Wireframe
        </button>
        <button
          type="button"
          onClick={() => setCollision((value) => !value)}
          title="Switch between visual meshes and the real collision STLs"
          aria-pressed={collision}
          className={toggleClass(collision)}
        >
          Collision
        </button>
        <button
          type="button"
          onClick={() => setShowJoints((value) => !value)}
          title="Joint anchors: amber hinge, blue slide, gray fixed"
          aria-pressed={showJoints}
          className={toggleClass(showJoints)}
        >
          Joints
        </button>
      </div>
      <button
        type="button"
        onClick={() => sceneRef.current?.resetCamera()}
        className="absolute bottom-3 right-3 rounded-lg border border-border-subtle/70 bg-white/90 px-2.5 py-1 text-[11px] font-medium text-stone-700 shadow-sm backdrop-blur transition-colors hover:bg-white"
      >
        Reset view
      </button>
    </div>
  )
}
