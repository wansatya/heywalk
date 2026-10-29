import { useEffect, useRef, useState } from 'react'
import { Move, RotateCcw } from 'lucide-react'
import { useExperimentStore } from '../../stores/experiment'
import { CONTROLLER_PATH, useWorkspaceStore } from '../../stores/workspace'
import { microduckAssetUrl } from '../../api/client'
import { isDefaultLayout, isTerminalStatus } from '../../api/types'
import { MicroduckScene } from './three/microduckScene'
import { clearSavedCamera } from './three/cameraState'
import { ErrorNotice, LoadingRow } from '../../components/Panel'

/**
 * MicroDuck 3D view: official STL meshes on a grid floor with the composed
 * environment (crates, ball, goal flag).
 *
 * - live: a run streams official body poses -> meshes follow MuJoCo exactly.
 * - preview: no telemetry yet -> the STAND keyframe pose, labelled as such.
 * - hint: the run streams joints but no body poses (generated rig) -> the
 *   duck cannot be posed, so explain instead of showing a wrong robot.
 * - arrange: explicit mode showing the STAND scene for dragging the duck
 *   spawn and prop spots; the layout applies to the next Start, never to
 *   live or recorded telemetry.
 */
export function MicroduckViewport() {
  const containerRef = useRef<HTMLDivElement>(null)
  const sceneRef = useRef<MicroduckScene | null>(null)
  const [webglError, setWebglError] = useState<string | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [loadProgress, setLoadProgress] = useState<{ loaded: number; total: number } | null>(null)
  const [meshesReady, setMeshesReady] = useState(false)
  const [arrange, setArrange] = useState(false)
  const telemetry = useExperimentStore((s) => s.telemetry)
  const runDetail = useExperimentStore((s) => s.runDetail)
  const cameraResetToken = useExperimentStore((s) => s.cameraResetToken)
  const seenTokenRef = useRef(0)
  const nextLayout = useExperimentStore((s) => s.nextLayout)
  const setNextLayout = useExperimentStore((s) => s.setNextLayout)
  const clearNextLayout = useExperimentStore((s) => s.clearNextLayout)
  const microduckModel = useWorkspaceStore((s) => s.microduckModel)
  const refreshMicroduckModel = useWorkspaceStore((s) => s.refreshMicroduckModel)

  useEffect(() => {
    void refreshMicroduckModel()
  }, [refreshMicroduckModel])

  const resetToDefaultView = () => {
    clearSavedCamera('microduck')
    sceneRef.current?.resetCamera()
  }

  // SimControls "Reset view" restores the default camera (token starts at 0).
  useEffect(() => {
    if (cameraResetToken === seenTokenRef.current) return
    seenTokenRef.current = cameraResetToken
    clearSavedCamera('microduck')
    sceneRef.current?.resetCamera()
  }, [cameraResetToken])

  const latest = telemetry.length > 0 ? telemetry[telemetry.length - 1] : null
  const bodies = latest?.bodies ?? null
  const joints = latest?.joints ?? []
  const model = microduckModel.data ?? null

  const officialBodies = model ? new Set(model.bodies) : new Set<string>()
  const livePoses = bodies?.filter((pose) => officialBodies.has(pose.body)) ?? []
  const envPoses = bodies?.filter((pose) => !officialBodies.has(pose.body)) ?? []
  const isLive = livePoses.length > 0
  const isRigRun = !isLive && joints.length > 0
  const runActive = runDetail !== null && !isTerminalStatus(runDetail.status)
  const customLayout = !isDefaultLayout(nextLayout)
  const hasControllerFile = useWorkspaceStore((s) => s.files.some((f) => f.path === CONTROLLER_PATH))
  const controllerBlank = useWorkspaceStore((s) => s.controllerBlank)
  const controllerMissing = !hasControllerFile || controllerBlank === true
  // Idle showcase: no runnable controller yet, so the STAND preview slowly
  // orbits until there is real work to watch. Live poses, a streaming run,
  // and arrange dragging all take over the camera instead.
  const idleSpin = controllerMissing && !isLive && !runActive && !arrange

  useEffect(() => {
    const container = containerRef.current
    if (!container || !model || sceneRef.current) return
    let cancelled = false
    try {
      const scene = new MicroduckScene(container, model, {
        assetUrl: microduckAssetUrl,
        onProgress: (loaded, total) => {
          if (!cancelled) setLoadProgress({ loaded, total })
        },
      })
      scene.start()
      sceneRef.current = scene
      void scene.load().then(({ failed }) => {
        if (cancelled) return
        setMeshesReady(true)
        if (failed.length > 0) {
          setLoadError(
            `${failed.length} mesh file(s) failed to load (${failed.slice(0, 3).join(', ')}${
              failed.length > 3 ? '…' : ''
            }); affected parts show as markers.`,
          )
        }
      })
    } catch (err) {
      setWebglError(err instanceof Error ? err.message : 'WebGL is unavailable.')
    }
    return () => {
      cancelled = true
      sceneRef.current?.dispose()
      sceneRef.current = null
    }
    // isRigRun toggles whether the container exists at all, so scene setup
    // must re-run when it flips (a rig run shows the hint, not the canvas).
  }, [model, isRigRun])

  // Pose from live telemetry when it carries official bodies, else hold the
  // STAND preview under the working layout. Arrange mode always previews.
  useEffect(() => {
    const scene = sceneRef.current
    if (!scene || !model) return
    scene.setLayout(nextLayout)
    if (isLive && bodies && !arrange) scene.sync(bodies)
    else if (!isRigRun || arrange) scene.syncPreview()
  }, [bodies, isLive, isRigRun, model, arrange, nextLayout])

  // Dragging reports layouts to the store; the pose effect mirrors them.
  useEffect(() => {
    sceneRef.current?.setArrange(arrange ? (layout) => setNextLayout(layout) : null)
  }, [arrange, setNextLayout])

  // Declared after scene setup so sceneRef exists when this first runs.
  useEffect(() => {
    sceneRef.current?.setAutoOrbit(idleSpin)
  }, [idleSpin, model, isRigRun])

  // A streaming run ends arrange mode; the layout was captured in the new
  // run's world config at Start. Finished runs keep arrange available.
  useEffect(() => {
    if (runActive && arrange) setArrange(false)
  }, [runActive, arrange])

  if (microduckModel.status === 'loading' && !model) {
    return (
      <div className="flex h-full w-full items-center justify-center p-3">
        <LoadingRow label="Loading MicroDuck model…" />
      </div>
    )
  }

  if (microduckModel.error && !model) {
    return (
      <div className="flex h-full w-full items-center justify-center p-3">
        <ErrorNotice
          message={`3D view unavailable: ${microduckModel.error}`}
          onRetry={() => void refreshMicroduckModel()}
        />
      </div>
    )
  }

  if (webglError) {
    return (
      <div className="flex h-full w-full items-center justify-center p-3 text-center">
        <p className="text-xs text-slate-400">
          3D view unavailable ({webglError}). Switch to the Joints tab for the run result.
        </p>
      </div>
    )
  }

  if (isRigRun) {
    return (
      <div className="flex h-full w-full items-center justify-center p-3 text-center">
        <p className="max-w-xs text-[11px] leading-relaxed text-slate-400">
          This run streams joint motion but no 3D body poses (generated rig), so there is no duck to
          pose. Install the official <span className="text-slate-200">MicroDuck</span> robot from the
          Robot panel and rerun for the live 3D duck.
        </p>
      </div>
    )
  }

  const loadingMeshes = loadProgress !== null && loadProgress.loaded < loadProgress.total

  return (
    <div className="relative h-full w-full">
      <div ref={containerRef} className="h-full w-full" role="img" aria-label="MicroDuck 3D view" />
      <div className="pointer-events-none absolute left-3 top-3 flex gap-2">
        <span
          className={`rounded-lg px-2.5 py-1 text-xs font-bold shadow-md ${
            arrange
              ? 'bg-amber-500 text-slate-950'
              : isLive
                ? 'bg-emerald-500 text-slate-950'
                : 'bg-sky-500 text-slate-950'
          }`}
        >
          {arrange
            ? 'Arranging · next Start'
            : isLive
              ? `Live · ${livePoses.length + envPoses.length} bodies`
              : 'STAND preview'}
        </span>
        {!arrange && customLayout && (
          <span className="rounded-lg bg-amber-500 px-2.5 py-1 text-xs font-bold text-slate-950 shadow-md">
            custom layout
          </span>
        )}
      </div>
      <div className="absolute right-3 top-3 flex items-center gap-2">
        <span className="pointer-events-none hidden rounded-lg border border-border-subtle/60 bg-black/70 px-2.5 py-1 font-mono text-xs text-slate-400 backdrop-blur-md md:block">
          {arrange
            ? 'drag duck & props to move · alt-drag duck to turn'
            : isLive
              ? 'live MuJoCo poses · drag orbit · scroll zoom'
              : 'official STAND pose · drag orbit · scroll zoom'}
        </span>
        {arrange ? (
          <>
            {customLayout && (
              <button
                type="button"
                onClick={() => clearNextLayout()}
                className="flex items-center gap-1 rounded-lg border border-border-subtle/80 bg-elevated/90 px-2.5 py-1 text-xs font-medium text-slate-200 backdrop-blur-md transition-colors hover:bg-elevated hover:text-white shadow-xs"
                title="Reset the duck spawn and prop spots to the composed defaults"
              >
                Reset
              </button>
            )}
            <button
              type="button"
              onClick={() => setArrange(false)}
              className="flex items-center gap-1.5 rounded-lg border border-amber-400/60 bg-amber-500 px-3 py-1 text-xs font-bold text-slate-950 shadow-md transition-all hover:bg-amber-400 active:scale-[0.98]"
              title="Finish arranging (the layout applies to the next Start)"
            >
              Done
            </button>
          </>
        ) : (
          <button
            type="button"
            onClick={() => setArrange(true)}
            disabled={runActive}
            className="flex items-center gap-1.5 rounded-lg border border-border-subtle/80 bg-elevated/90 px-2.5 py-1 text-xs font-medium text-slate-200 backdrop-blur-md transition-colors hover:bg-elevated hover:text-white shadow-xs disabled:cursor-not-allowed disabled:opacity-40"
            title={
              runActive
                ? 'Arranging is unavailable while a run streams'
                : 'Arrange the duck spawn and prop spots for the next run'
            }
          >
            <Move size={13} />
            Arrange
          </button>
        )}
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
      {(loadingMeshes || loadError) && (
        <div className="pointer-events-none absolute left-3 right-3 top-3 flex justify-center">
          {loadingMeshes && loadProgress && (
            <span className="rounded-lg border border-border-subtle/60 bg-black/75 px-3 py-1 font-mono text-xs text-slate-300 shadow-md backdrop-blur-md">
              loading meshes {loadProgress.loaded}/{loadProgress.total}…
            </span>
          )}
          {!loadingMeshes && loadError && (
            <span className="rounded-lg bg-amber-500 px-3 py-1 text-xs font-semibold text-slate-950 shadow-md">
              {loadError}
            </span>
          )}
        </div>
      )}
      {!meshesReady && !loadingMeshes && (
        <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
          <span className="rounded-lg border border-border-subtle/60 bg-black/75 px-3 py-1.5 font-mono text-xs text-slate-400 shadow-md backdrop-blur-md">
            preparing 3D view…
          </span>
        </div>
      )}
    </div>
  )
}
