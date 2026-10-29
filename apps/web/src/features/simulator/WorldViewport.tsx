import { useEffect, useRef, useState } from 'react'
import { RotateCcw } from 'lucide-react'
import { useExperimentStore } from '../../stores/experiment'
import { CONTROLLER_PATH, useWorkspaceStore } from '../../stores/workspace'
import { DEFAULT_WORLD } from './defaultWorld'
import { WorldScene } from './three/worldScene'
import { clearSavedCamera } from './three/cameraState'
import { isDiffDriveWorld, isTerminalStatus } from '../../api/types'

/** 3D arena view for the legacy deterministic differential-drive backend. */
export function WorldViewport() {
  const containerRef = useRef<HTMLDivElement>(null)
  const sceneRef = useRef<WorldScene | null>(null)
  const lastWorldRef = useRef<object | null>(null)
  const [webglError, setWebglError] = useState<string | null>(null)
  const telemetry = useExperimentStore((s) => s.telemetry)
  const runDetail = useExperimentStore((s) => s.runDetail)
  const cameraResetToken = useExperimentStore((s) => s.cameraResetToken)
  const runWorld = runDetail?.world_config
  const world = isDiffDriveWorld(runWorld) ? runWorld : DEFAULT_WORLD
  const hasControllerFile = useWorkspaceStore((s) => s.files.some((f) => f.path === CONTROLLER_PATH))
  const controllerBlank = useWorkspaceStore((s) => s.controllerBlank)
  const controllerMissing = !hasControllerFile || controllerBlank === true
  const runActive = runDetail !== null && !isTerminalStatus(runDetail.status)
  // Idle showcase: no runnable controller and nothing recorded yet, so the
  // empty arena slowly orbits until there is real work to watch.
  const idleSpin = controllerMissing && telemetry.length === 0 && !runActive

  const resetToDefaultView = () => {
    clearSavedCamera('world')
    sceneRef.current?.resetCamera()
  }

  // SimControls "Reset view" restores the default camera (token starts at 0).
  const seenTokenRef = useRef(0)
  useEffect(() => {
    if (cameraResetToken === seenTokenRef.current) return
    seenTokenRef.current = cameraResetToken
    resetToDefaultView()
  }, [cameraResetToken])

  useEffect(() => {
    const container = containerRef.current
    if (!container || sceneRef.current) return
    try {
      const scene = new WorldScene(container, world)
      scene.start()
      sceneRef.current = scene
      lastWorldRef.current = world
    } catch (err) {
      setWebglError(err instanceof Error ? err.message : 'WebGL is unavailable.')
    }
    return () => {
      sceneRef.current?.dispose()
      sceneRef.current = null
      lastWorldRef.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    const scene = sceneRef.current
    if (!scene) return
    if (lastWorldRef.current !== world) {
      scene.setWorld(world)
      scene.resetRun()
      lastWorldRef.current = world
    }
    scene.sync(telemetry)
  }, [telemetry, world])

  useEffect(() => {
    sceneRef.current?.setAutoOrbit(idleSpin)
  }, [idleSpin])

  const latest = telemetry.length > 0 ? telemetry[telemetry.length - 1] : null
  const collisions = telemetry.filter((s) => s.collision).length
  const goalReached = latest?.goal_reached === true

  if (webglError) {
    return (
      <div className="flex h-full w-full flex-col items-center justify-center gap-2 bg-base p-4 text-center">
        <p className="text-sm font-medium text-slate-200">3D view unavailable</p>
        <p className="max-w-sm text-xs text-slate-400">
          WebGL could not start ({webglError}). Telemetry, logs, and metrics below still show the real run result.
        </p>
      </div>
    )
  }

  return (
    <div className="relative h-full w-full">
      <div ref={containerRef} className="h-full w-full" role="img" aria-label="3D robot simulation view" />
      <div className="pointer-events-none absolute left-3 top-3 flex gap-2">
        {collisions > 0 && (
          <span className="rounded-lg bg-red-500 px-2.5 py-1 text-xs font-bold text-white shadow-md">
            Collision ×{collisions}
          </span>
        )}
        {goalReached && (
          <span className="rounded-lg bg-emerald-500 px-2.5 py-1 text-xs font-bold text-slate-950 shadow-md">
            Goal reached
          </span>
        )}
      </div>
      <div className="absolute right-3 top-3 flex items-center gap-2">
        <span className="pointer-events-none hidden rounded-lg border border-border-subtle/60 bg-black/70 px-2.5 py-1 font-mono text-xs text-slate-400 backdrop-blur-md md:block">
          drag orbit · scroll zoom · right-drag pan
        </span>
        <button
          type="button"
          onClick={() => sceneRef.current?.focusRobot()}
          className="rounded-lg border border-border-subtle/80 bg-elevated/90 px-2.5 py-1 text-xs font-medium text-slate-200 backdrop-blur-md transition-colors hover:bg-elevated hover:text-white shadow-xs"
          title="Focus the camera on the robot"
        >
          Focus
        </button>
        <button
          type="button"
          onClick={resetToDefaultView}
          className="flex items-center gap-1.5 rounded-lg border border-border-subtle/80 bg-elevated/90 px-2.5 py-1 text-xs font-medium text-slate-200 backdrop-blur-md transition-colors hover:bg-elevated hover:text-white shadow-xs"
          title="Reset camera to the default view (clears your saved view)"
        >
          <RotateCcw size={13} />
          View
        </button>
      </div>
      <div className="pointer-events-none absolute bottom-3 right-3 rounded-lg border border-border-subtle/60 bg-black/70 px-3 py-1.5 font-mono text-xs text-slate-300 backdrop-blur-md shadow-md">
        {latest
          ? `x ${latest.x.toFixed(2)} · y ${latest.y.toFixed(2)} · θ ${latest.theta.toFixed(2)} · v ${latest.v.toFixed(2)}`
          : `start (${world.start.x.toFixed(1)}, ${world.start.y.toFixed(1)}) · no telemetry yet`}
      </div>
    </div>
  )
}
