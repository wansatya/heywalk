import { useEffect, useRef, useState } from 'react'
import { useExperimentStore } from '../../stores/experiment'
import { isTerminalStatus } from '../../api/types'

/**
 * Silence on a live stream that means the runner ended (telemetry normally
 * flows at ~20 Hz, so 2 s quiet is the container drain, not a stall).
 */
const RUNNER_SILENCE_MS = 2000
/** Sustained base stillness that means the robot stopped (not a footfall). */
const ROBOT_STOP_MS = 1500
/** Base stillness bounds (mirrors the backend's settle detection). */
const STILL_V = 0.05
const STILL_OMEGA = 0.1

/**
 * Centered run-phase indicator for the simulator viewports.
 *
 * Covers the two gaps with no other feedback: after Start/Rerun, before the
 * first telemetry sample arrives, and after the runner ends, while the
 * evaluation/comparison (diagnostics) still loads. "Evaluating" appears as
 * soon as the robot stops moving or the sensors go quiet on a live run —
 * the backend only confirms the evaluating status after draining the runner
 * container, seconds later. Rendered by both the MuJoCo and the 2D
 * viewports; null when there is nothing to wait for.
 */
export function RunPhaseOverlay() {
  const runAction = useExperimentStore((s) => s.runAction)
  const runDetail = useExperimentStore((s) => s.runDetail)
  const activeRunId = useExperimentStore((s) => s.activeRunId)
  const latest = useExperimentStore((s) =>
    s.telemetry.length > 0 ? s.telemetry[s.telemetry.length - 1] : null,
  )
  const streamState = useExperimentStore((s) => s.streamState)
  const comparisonLoading = useExperimentStore((s) => s.comparisonLoading)
  const detailLoading = useExperimentStore((s) => s.detailLoading)
  const vizPaused = useExperimentStore((s) => s.vizPaused)

  const live = runDetail !== null && !isTerminalStatus(runDetail.status)
  // End-of-run watch: live, pre-evaluating, samples flowing, stream healthy,
  // physics running, viz unmuted. Anything else (paused run, paused viz,
  // reconnecting stream) freezes telemetry without the runner ending, so
  // quiet there must not read as evaluating.
  const watchEnd =
    live &&
    runDetail.status !== 'evaluating' &&
    latest !== null &&
    streamState === 'live' &&
    !vizPaused &&
    runDetail.paused !== true

  const lastRunRef = useRef(activeRunId)
  const lastSampleRef = useRef(latest)
  const lastSampleAtRef = useRef(0)
  const sawMotionRef = useRef(false)
  const stillSinceRef = useRef<number | null>(null)
  const watchingRef = useRef(false)
  if (lastRunRef.current !== activeRunId) {
    lastRunRef.current = activeRunId
    lastSampleRef.current = null
    lastSampleAtRef.current = 0
    sawMotionRef.current = false
    stillSinceRef.current = null
    watchingRef.current = false
  }
  if (latest !== null && lastSampleRef.current !== latest) {
    lastSampleRef.current = latest
    const at = Date.now()
    lastSampleAtRef.current = at
    if (Math.abs(latest.v) >= STILL_V || Math.abs(latest.omega) >= STILL_OMEGA) {
      sawMotionRef.current = true
      stillSinceRef.current = null
    } else if (stillSinceRef.current === null) {
      stillSinceRef.current = at
    }
  }
  if (watchEnd && !watchingRef.current) {
    // Fresh grace period whenever the watch (re)starts, so a reconnect
    // flicker or a stale sample cannot trip the detectors instantly.
    lastSampleAtRef.current = Date.now()
    stillSinceRef.current = null
  }
  watchingRef.current = watchEnd

  const [now, setNow] = useState(0)
  useEffect(() => {
    if (!watchEnd) return
    setNow(Date.now())
    const id = window.setInterval(() => setNow(Date.now()), 500)
    return () => window.clearInterval(id)
  }, [watchEnd])

  const runnerSilent =
    watchEnd && now - lastSampleAtRef.current >= RUNNER_SILENCE_MS
  // The robot stopped only counts after it demonstrably moved: joint-test
  // runs on a welded trunk never move the base, so stillness alone would
  // fire there instantly.
  const robotStopped =
    watchEnd &&
    sawMotionRef.current &&
    stillSinceRef.current !== null &&
    now - stillSinceRef.current >= ROBOT_STOP_MS

  let label: string | null = null
  if (runAction === 'starting') label = 'Starting run…'
  else if (runAction === 'rerunning') label = 'Rerunning…'
  else if (live && runDetail.status === 'evaluating') label = 'Evaluating run…'
  else if (live && (runnerSilent || robotStopped)) label = 'Evaluating run…'
  else if (live && latest === null && streamState !== 'closed') {
    // A closed stream with no data means the failure is already reported
    // in the logs; keep waiting only while the stream can still deliver.
    label = 'Waiting for telemetry…'
  } else if (
    runDetail !== null &&
    isTerminalStatus(runDetail.status) &&
    (comparisonLoading || detailLoading)
  ) {
    label = 'Loading diagnostics…'
  }
  if (label === null) return null

  return (
    <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center bg-white/60">
      <div
        role="status"
        className="flex flex-col items-center gap-2.5 rounded-lg border border-border-subtle bg-white px-5 py-3 shadow-xl backdrop-blur-md"
      >
        <span className="relative block h-1 w-20 overflow-hidden rounded-full bg-stone-200" aria-hidden="true">
          {[0, 1, 2, 3, 4].map((i) => (
            <span key={i} className="kitt-dot" />
          ))}
        </span>
        <span className="font-mono text-xs text-stone-700">{label}</span>
      </div>
    </div>
  )
}
