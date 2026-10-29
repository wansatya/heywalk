import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import {
  ApiError,
  cancelRun,
  createRun,
  describeApiError,
  getRun,
  getRunComparison,
  getRunLogs,
  getRunTelemetry,
  getRunTests,
  listRuns,
  pauseRun,
  rerunRun,
  resumeRun,
  triggerEstop,
} from '../api/client'
import { connectRunStream, type StreamConnectionState } from '../api/stream'
import {
  DEFAULT_SIM_LAYOUT,
  isTerminalStatus,
  type Backend,
  type EvaluationReport,
  type Run,
  type RunComparison,
  type RunMetrics,
  type RunObjective,
  type RunStatus,
  type SimLayout,
  type StreamEvent,
  type TelemetrySample,
} from '../api/types'

export type RunActionStatus = 'idle' | 'starting' | 'rerunning' | 'cancelling' | 'estopping' | 'pausing' | 'resuming' | 'error'

/** Scenario overrides for a new run. The backend defaults to MuJoCo server-side. */
export interface StartRunOptions {
  seed?: number
  /** Sim-time budget in seconds, or null for unlimited. */
  timeLimitS?: number | null
  backend?: Backend
  /** Arrange-mode scene layout (duck spawn + prop spots). */
  layout?: SimLayout
  /** Walk to the flag (locomotion) instead of running the joint tests. */
  locomotion?: boolean
  /** Phase sequence declared by generation (empty = flag default). */
  objectives?: RunObjective[]
}

const MAX_TELEMETRY_POINTS = 5000

let disconnectStream: (() => void) | null = null

interface ExperimentState {
  runs: Run[]
  activeRunId: string | null
  baselineRunId: string | null
  runDetail: Run | null
  telemetry: TelemetrySample[]
  logs: string[]
  comparison: RunComparison | null

  runsLoading: boolean
  runsError: string | null
  detailLoading: boolean
  comparisonLoading: boolean
  comparisonError: string | null
  streamState: StreamConnectionState
  runAction: RunActionStatus
  runActionError: string | null
  estopLatched: boolean
  estopUnsupported: boolean
  vizPaused: boolean
  /** Bumped by Reset view; viewports restore the default camera + clear saved. */
  cameraResetToken: number
  backendOnline: boolean | null
  /** Arrange-mode layout for the next Start (resets never touch history). */
  nextLayout: SimLayout

  fetchRuns: (projectId: string) => Promise<void>
  selectRun: (runId: string | null, projectId?: string) => Promise<void>
  setBaseline: (runId: string | null) => void
  startRun: (projectId: string, options?: StartRunOptions) => Promise<void>
  rerunRun: (runId: string, projectId?: string) => Promise<void>
  cancelActiveRun: () => Promise<void>
  latchEstop: () => Promise<void>
  pauseActiveRun: () => Promise<void>
  resumeActiveRun: () => Promise<void>
  resetView: () => void
  /** Clear all run state (the active project was deleted, none remains). */
  clearProject: () => void
  setVizPaused: (paused: boolean) => void
  setNextLayout: (layout: SimLayout) => void
  clearNextLayout: () => void
  fetchComparison: () => Promise<void>
  refreshTests: () => Promise<void>
  handleStreamEvent: (event: StreamEvent) => void
  setStreamState: (state: StreamConnectionState) => void
  setBackendOnline: (online: boolean) => void
}

function stopStream() {
  if (disconnectStream) {
    disconnectStream()
    disconnectStream = null
  }
}

export const useExperimentStore = create<ExperimentState>()(
  persist(
    (set, get) => ({
      runs: [],
      activeRunId: null,
      baselineRunId: null,
      runDetail: null,
      telemetry: [],
      logs: [],
      comparison: null,

      runsLoading: false,
      runsError: null,
      detailLoading: false,
      comparisonLoading: false,
      comparisonError: null,
      streamState: 'disconnected',
      runAction: 'idle',
      runActionError: null,
      estopLatched: false,
      estopUnsupported: false,
      vizPaused: false,
      cameraResetToken: 0,
      backendOnline: null,
      nextLayout: DEFAULT_SIM_LAYOUT,

      fetchRuns: async (projectId) => {
        set({ runsLoading: true, runsError: null })
        try {
          const runs = await listRuns(projectId)
          set({ runs, runsLoading: false, backendOnline: true })
          const { activeRunId } = get()
          if (runs.length > 0 && !runs.some((r) => r.id === activeRunId)) {
            await get().selectRun(runs[0].id, projectId)
          } else if (runs.length === 0) {
            stopStream()
            set({ activeRunId: null, runDetail: null, telemetry: [], logs: [], comparison: null })
          }
        } catch (err) {
          set({
            runsLoading: false,
            runsError: describeApiError(err),
            backendOnline: err instanceof ApiError && err.kind === 'network' ? false : get().backendOnline,
          })
        }
      },

      selectRun: async (runId, projectId) => {
        stopStream()
        set({
          activeRunId: runId,
          runDetail: null,
          telemetry: [],
          logs: [],
          comparison: null,
          comparisonError: null,
          estopLatched: false,
          estopUnsupported: false,
          streamState: 'disconnected',
          runActionError: null,
        })
        if (!runId) return
        set({ detailLoading: true })
        try {
          const [run, telemetry, logs] = await Promise.all([
            getRun(runId),
            getRunTelemetry(runId).catch((): TelemetrySample[] => []),
            getRunLogs(runId).catch((): string[] => []),
          ])
          set({
            runDetail: run,
            telemetry: telemetry.slice(-MAX_TELEMETRY_POINTS),
            logs,
            detailLoading: false,
            backendOnline: true,
          })
          if (projectId) {
            // Refresh the list entry so history shows the latest status.
            const runs = await listRuns(projectId).catch(() => get().runs)
            set({ runs })
          }
          if (!isTerminalStatus(run.status)) {
            disconnectStream = connectRunStream(runId, {
              onEvent: get().handleStreamEvent,
              onStateChange: get().setStreamState,
            })
          }
        } catch (err) {
          set({ detailLoading: false, runActionError: describeApiError(err) })
        }
      },

      setBaseline: (runId) => {
        set({ baselineRunId: runId, comparison: null, comparisonError: null })
        if (runId && get().activeRunId) void get().fetchComparison()
      },

      startRun: async (projectId, options = {}) => {
        set({ runAction: 'starting', runActionError: null })
        try {
          const world: Record<string, unknown> = {}
          if (options.backend) world.backend = options.backend
          if (options.timeLimitS !== undefined) world.time_limit = options.timeLimitS
          if (options.layout?.duck) {
            world.spawn_xy = [options.layout.duck.x, options.layout.duck.y]
            world.spawn_yaw = options.layout.duck.yaw
          }
          if (options.layout && Object.keys(options.layout.props).length > 0) {
            world.env_layout = Object.fromEntries(
              Object.entries(options.layout.props).map(([body, pos]) => [body, [pos.x, pos.y]]),
            )
          }
          if (options.locomotion !== undefined) world.locomotion = options.locomotion
          if (options.objectives && options.objectives.length > 0) {
            world.objectives = options.objectives
            // First phase for legacy readers; the runner prefers the list.
            world.objective = options.objectives[0]
          }
          const run = await createRun(projectId, {
            ...(options.seed !== undefined ? { seed: options.seed } : {}),
            ...(Object.keys(world).length > 0 ? { world_config: world } : {}),
          })
          set((state) => ({ runs: [run, ...state.runs], runAction: 'idle', backendOnline: true }))
          await get().selectRun(run.id, projectId)
        } catch (err) {
          set({
            runAction: 'error',
            runActionError: describeApiError(err),
            backendOnline: err instanceof ApiError && err.kind === 'network' ? false : get().backendOnline,
          })
        }
      },

      rerunRun: async (runId, projectId) => {
        set({ runAction: 'rerunning', runActionError: null })
        try {
          const run = await rerunRun(runId)
          set((state) => ({
            runs: [run, ...state.runs],
            runAction: 'idle',
            backendOnline: true,
            // The rerun keeps the same scenario; mark the parent as the
            // comparison baseline so Results shows before/after metrics.
            baselineRunId: run.parent_run_id ?? state.baselineRunId,
            comparison: null,
            comparisonError: null,
          }))
          await get().selectRun(run.id, projectId)
        } catch (err) {
          set({
            runAction: 'error',
            runActionError: describeApiError(err),
            backendOnline: err instanceof ApiError && err.kind === 'network' ? false : get().backendOnline,
          })
        }
      },

      cancelActiveRun: async () => {
        const { activeRunId } = get()
        if (!activeRunId) return
        set({ runAction: 'cancelling', runActionError: null })
        try {
          await cancelRun(activeRunId)
          set({ runAction: 'idle' })
          const run = await getRun(activeRunId)
          set({ runDetail: run })
          set((state) => ({
            runs: state.runs.map((r) => (r.id === run.id ? run : r)),
          }))
        } catch (err) {
          set({ runAction: 'error', runActionError: describeApiError(err) })
        }
      },

      latchEstop: async () => {
        const { activeRunId } = get()
        if (!activeRunId) return
        set({ runAction: 'estopping', runActionError: null })
        try {
          await triggerEstop(activeRunId)
          set({ estopLatched: true, runAction: 'idle' })
        } catch (err) {
          if (err instanceof ApiError && err.status === 404) {
            set({
              estopUnsupported: true,
              runAction: 'error',
              runActionError: 'Emergency-stop endpoint is not implemented by the backend yet.',
            })
          } else {
            set({ runAction: 'error', runActionError: describeApiError(err) })
          }
        }
      },

      pauseActiveRun: async () => {
        const { activeRunId } = get()
        if (!activeRunId) return
        set({ runAction: 'pausing', runActionError: null })
        try {
          const run = await pauseRun(activeRunId)
          set({ runAction: 'idle', runDetail: run })
          set((state) => ({
            runs: state.runs.map((r) => (r.id === run.id ? run : r)),
          }))
        } catch (err) {
          set({ runAction: 'error', runActionError: describeApiError(err) })
        }
      },

      resumeActiveRun: async () => {
        const { activeRunId } = get()
        if (!activeRunId) return
        set({ runAction: 'resuming', runActionError: null })
        try {
          const run = await resumeRun(activeRunId)
          set({ runAction: 'idle', runDetail: run })
          set((state) => ({
            runs: state.runs.map((r) => (r.id === run.id ? run : r)),
          }))
        } catch (err) {
          set({ runAction: 'error', runActionError: describeApiError(err) })
        }
      },

      resetView: () => {
        set((state) => ({
          telemetry: [],
          logs: [],
          vizPaused: false,
          estopLatched: false,
          cameraResetToken: state.cameraResetToken + 1,
        }))
      },

      clearProject: () => {
        stopStream()
        set({
          runs: [],
          activeRunId: null,
          baselineRunId: null,
          runDetail: null,
          telemetry: [],
          logs: [],
          comparison: null,
          runsError: null,
          comparisonError: null,
          runAction: 'idle',
          runActionError: null,
          estopLatched: false,
          vizPaused: false,
        })
      },

      setVizPaused: (paused) => set({ vizPaused: paused }),

      setNextLayout: (layout) => set({ nextLayout: layout }),
      clearNextLayout: () => set({ nextLayout: DEFAULT_SIM_LAYOUT }),

      fetchComparison: async () => {
        const { activeRunId, baselineRunId } = get()
        if (!activeRunId) return
        set({ comparisonLoading: true, comparisonError: null })
        try {
          const comparison = await getRunComparison(activeRunId, baselineRunId)
          set({ comparison, comparisonLoading: false })
        } catch (err) {
          set({ comparisonLoading: false, comparisonError: describeApiError(err) })
        }
      },

      refreshTests: async () => {
        const { activeRunId, runDetail } = get()
        if (!activeRunId) return
        try {
          const tests = await getRunTests(activeRunId)
          if (runDetail) set({ runDetail: { ...runDetail, tests } })
        } catch {
          // Test evidence is non-critical here; the run payload already carries
          // whatever the evaluator recorded.
        }
      },

      handleStreamEvent: (event) => {
        const { runDetail } = get()
        switch (event.type) {
          case 'status': {
            const status: RunStatus = event.status
            if (runDetail) set({ runDetail: { ...runDetail, status } })
            set((state) => ({
              runs: state.runs.map((r) => (r.id === state.activeRunId ? { ...r, status } : r)),
            }))
            const finishedRunId = get().activeRunId
            if (isTerminalStatus(status) && finishedRunId) {
              void getRun(finishedRunId)
                .then((run) => {
                  set({ runDetail: run })
                  set((state) => ({
                    runs: state.runs.map((r) => (r.id === run.id ? run : r)),
                  }))
                  void get().fetchComparison()
                })
                .catch(() => undefined)
            }
            break
          }
          case 'telemetry': {
            if (get().vizPaused) break
            set((state) => ({
              telemetry: [...state.telemetry.slice(-(MAX_TELEMETRY_POINTS - 1)), event.sample],
            }))
            if (event.sample.estop_active) set({ estopLatched: true })
            break
          }
          case 'log':
            set((state) => ({ logs: [...state.logs.slice(-1999), event.line] }))
            break
          case 'evaluation': {
            const evaluation: EvaluationReport = event.evaluation
            const metrics: RunMetrics | null = event.metrics
            if (runDetail) set({ runDetail: { ...runDetail, evaluation, metrics } })
            break
          }
          case 'error':
            set((state) => ({ logs: [...state.logs, `[stream] ${event.message}`] }))
            break
        }
      },

      setStreamState: (streamState) => set({ streamState }),
      setBackendOnline: (online) => set({ backendOnline: online }),
    }),
    {
      name: 'heywalk-experiment',
      partialize: (state) =>
        ({ activeRunId: state.activeRunId, baselineRunId: state.baselineRunId }) as ExperimentState,
    },
  ),
)
