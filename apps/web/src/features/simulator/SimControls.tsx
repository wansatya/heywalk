import { AlertOctagon, Pause, PauseCircle, Play, PlayCircle, Repeat, RotateCcw, Square, X } from 'lucide-react'
import { CONTROLLER_PATH, useWorkspaceStore } from '../../stores/workspace'
import { useExperimentStore } from '../../stores/experiment'
import { StatusPill } from '../../components/StatusPill'
import { ActionButton } from '../../components/Panel'
import { clsx } from '../../components/clsx'
import { isDefaultLayout, isTerminalStatus } from '../../api/types'
import type { StreamConnectionState } from '../../api/stream'

const STREAM_LABELS: Record<StreamConnectionState, { label: string; className: string }> = {
  disconnected: { label: 'Stream idle', className: 'bg-slate-500' },
  connecting: { label: 'Connecting…', className: 'bg-sky-400 animate-pulse' },
  live: { label: 'Live', className: 'bg-emerald-400' },
  reconnecting: { label: 'Reconnecting…', className: 'bg-amber-400 animate-pulse' },
  closed: { label: 'Stream closed', className: 'bg-slate-500' },
}

export function SimControls() {
  const activeProjectId = useWorkspaceStore((s) => s.activeProjectId)
  const taskDefined = useWorkspaceStore((s) => s.taskDraft.trim().length > 0)
  const hasControllerFile = useWorkspaceStore((s) => s.files.some((f) => f.path === CONTROLLER_PATH))
  const controllerBlank = useWorkspaceStore((s) => s.controllerBlank)
  const saveState = useWorkspaceStore((s) => s.saveState)
  const objectives = useWorkspaceStore((s) => s.objectives)

  const runDetail = useExperimentStore((s) => s.runDetail)
  const activeRunId = useExperimentStore((s) => s.activeRunId)
  const runAction = useExperimentStore((s) => s.runAction)
  const runActionError = useExperimentStore((s) => s.runActionError)
  const streamState = useExperimentStore((s) => s.streamState)
  const vizPaused = useExperimentStore((s) => s.vizPaused)
  const telemetryCount = useExperimentStore((s) => s.telemetry.length)
  const estopLatched = useExperimentStore((s) => s.estopLatched)
  const startRun = useExperimentStore((s) => s.startRun)
  const rerunRun = useExperimentStore((s) => s.rerunRun)
  const cancelActiveRun = useExperimentStore((s) => s.cancelActiveRun)
  const latchEstop = useExperimentStore((s) => s.latchEstop)
  const pauseActiveRun = useExperimentStore((s) => s.pauseActiveRun)
  const resumeActiveRun = useExperimentStore((s) => s.resumeActiveRun)
  const resetView = useExperimentStore((s) => s.resetView)
  const setVizPaused = useExperimentStore((s) => s.setVizPaused)
  const nextLayout = useExperimentStore((s) => s.nextLayout)
  const clearNextLayout = useExperimentStore((s) => s.clearNextLayout)
  const customLayout = !isDefaultLayout(nextLayout)

  const live = runDetail !== null && !isTerminalStatus(runDetail.status)
  const finished = runDetail !== null && isTerminalStatus(runDetail.status)
  const simPaused = runDetail?.paused === true
  const saving = saveState.status === 'loading'
  const busy =
    runAction === 'starting' ||
    runAction === 'rerunning' ||
    runAction === 'cancelling' ||
    runAction === 'estopping' ||
    runAction === 'pausing' ||
    runAction === 'resuming' ||
    saving
  const controllerMissing = !hasControllerFile || controllerBlank === true
  const stream = STREAM_LABELS[streamState]

  // Runs execute saved files, so Start/Rerun persist the editor first and
  // abort when the save fails — the run always uses the updated controller.
  const runWithSave = async (run: () => Promise<void>) => {
    const workspace = useWorkspaceStore.getState()
    if (workspace.editorDirty) {
      await workspace.saveActiveFile()
      if (useWorkspaceStore.getState().saveState.status === 'error') return
    }
    await run()
  }
  const handleStart = () => {
    if (!activeProjectId) return
    void runWithSave(() =>
      startRun(activeProjectId, {
        layout: nextLayout,
        locomotion: true,
        objectives,
      }),
    )
  }
  const handleRerun = () => {
    if (!activeRunId || !activeProjectId) return
    void runWithSave(() => rerunRun(activeRunId, activeProjectId))
  }

  return (
    <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-border-subtle/80 bg-elevated/90 px-3.5 py-2.5 backdrop-blur-sm">
      <ActionButton
        onClick={handleStart}
        disabled={!activeProjectId || !taskDefined || controllerMissing || busy || live}
        variant="primary"
        title={
          !activeProjectId
            ? 'Create a sequence first'
            : !taskDefined
              ? 'Describe the task first — Start stays disabled until this sequence has a task'
              : controllerMissing
                ? 'controller.py is empty — import the sample script or generate a controller first'
                : live
                  ? 'A run is already in progress'
                  : saving
                    ? 'Saving the editor first…'
                    : 'Save the editor and run the updated controller.py'
        }
      >
        <Play size={14} />
        {runAction === 'starting' ? 'Starting…' : saving ? 'Saving…' : 'Start'}
      </ActionButton>

      <ActionButton
        onClick={() => void cancelActiveRun()}
        disabled={!live || busy}
        title="Request cancellation of the running simulation"
      >
        <Square size={14} />
        {runAction === 'cancelling' ? 'Cancelling…' : 'Stop'}
      </ActionButton>

      <ActionButton
        onClick={handleRerun}
        disabled={!activeRunId || !finished || controllerMissing || busy}
        title={
          !finished
            ? 'Rerun is available once the selected run finishes'
            : controllerMissing
              ? 'controller.py is empty — import the sample script or generate a controller first'
              : 'Save the editor and rerun the same scenario (world + seed) with the updated controller'
        }
      >
        <Repeat size={14} />
        {runAction === 'rerunning' ? 'Rerunning…' : saving ? 'Saving…' : 'Rerun'}
      </ActionButton>

      <ActionButton
        onClick={() => (simPaused ? void resumeActiveRun() : void pauseActiveRun())}
        disabled={!live || busy}
        title={
          simPaused
            ? 'Resume the suspended simulation (physics continues)'
            : 'Pause the running simulation (physics suspended, state kept)'
        }
      >
        {simPaused ? <PlayCircle size={14} /> : <PauseCircle size={14} />}
        {runAction === 'pausing' ? 'Pausing…' : runAction === 'resuming' ? 'Resuming…' : simPaused ? 'Resume sim' : 'Pause sim'}
      </ActionButton>

      <ActionButton
        onClick={() => setVizPaused(!vizPaused)}
        disabled={telemetryCount === 0}
        title={vizPaused ? 'Resume live visualization' : 'Pause live visualization (simulation keeps running)'}
      >
        <Pause size={14} />
        {vizPaused ? 'Resume view' : 'Pause view'}
      </ActionButton>

      {customLayout && (
        <button
          type="button"
          onClick={() => clearNextLayout()}
          title="A custom scene layout applies to the next Start — click to reset to the composed defaults"
          className="inline-flex items-center gap-1 rounded-lg border border-amber-400/50 bg-amber-500/15 px-2.5 py-1 text-xs font-medium text-amber-200 transition-colors hover:bg-amber-500/25 shadow-xs"
        >
          Layout*
          <X size={11} />
        </button>
      )}

      <ActionButton onClick={resetView} title="Clear trajectory and logs, and reset the camera to the default view">
        <RotateCcw size={14} />
        Reset view
      </ActionButton>

      <button
        type="button"
        onClick={() => void latchEstop()}
        disabled={!live || busy || estopLatched}
        title="Latch the emergency stop for the running simulation"
        className={clsx(
          'inline-flex items-center justify-center gap-1.5 rounded-lg border px-3 py-1.5 text-xs font-bold tracking-wider transition-all shadow-sm disabled:cursor-not-allowed disabled:opacity-40 active:scale-[0.98]',
          estopLatched
            ? 'border-red-400 bg-red-500 text-white shadow-red-500/25 animate-pulse'
            : 'border-red-500/40 bg-red-500/15 text-red-200 hover:bg-red-500/25',
        )}
      >
        <AlertOctagon size={14} />
        {estopLatched ? 'E-STOP ACTIVE' : runAction === 'estopping' ? 'SENDING…' : 'E-STOP'}
      </button>

      <div className="ml-auto flex items-center gap-3">
        <span className="inline-flex items-center gap-1.5 text-xs font-medium text-slate-400">
          <span className={clsx('h-1.5 w-1.5 rounded-full', stream.className)} />
          {stream.label}
        </span>
        {runDetail && <StatusPill status={runDetail.status} />}
      </div>

      {runActionError && (
        <p className="w-full text-xs text-red-300" role="alert">
          {runActionError}
        </p>
      )}
    </div>
  )
}
