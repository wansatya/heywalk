import { Suspense, lazy, useMemo, useState } from 'react'
import { Activity, AlertOctagon, AlertTriangle, Box, CheckCircle2, CircleDashed, Cpu, ListChecks, X, XCircle } from 'lucide-react'
import { useExperimentStore } from '../../stores/experiment'
import {
  BACKEND_LABELS,
  isMujocoWorld,
  type JointState,
  type PlannedTest,
  type TestResult,
} from '../../api/types'
import { clsx } from '../../components/clsx'
import { propLabel, objectiveLabel } from './objectiveLabel'
import { RunPhaseOverlay } from './RunPhaseOverlay'

// The 3D skeleton pulls in three.js; keep it out of the initial bundle.
const MicroduckViewport = lazy(() =>
  import('./MicroduckViewport').then((m) => ({ default: m.MicroduckViewport })),
)

/** Smallest power-of-two-ish scale that fits `value`, so gauges stay stable. */
function niceScale(value: number): number {
  const abs = Math.abs(value)
  if (!Number.isFinite(abs) || abs <= 1e-9) return 1
  const magnitude = 10 ** Math.floor(Math.log10(abs))
  for (const step of [1, 2, 2.5, 5, 10]) {
    if (abs <= step * magnitude) return step * magnitude
  }
  return 10 * magnitude
}

function JointGauge({
  joint,
  scale,
  passed,
}: {
  joint: JointState
  scale: number
  passed: boolean | null
}) {
  const pct = (value: number) => {
    const clamped = Math.max(-scale, Math.min(scale, value))
    return ((clamped + scale) / (2 * scale)) * 100
  }
  const pos = pct(joint.position)
  const target = pct(joint.target)
  const left = Math.min(pos, target)
  const width = Math.abs(pos - target)

  return (
    <div className="space-y-1.5 rounded-lg border border-border-subtle/60 bg-white p-2.5">
      <div className="flex items-baseline justify-between gap-2">
        <span className="flex items-center gap-2 font-mono text-xs font-semibold text-stone-800">
          {passed === null ? (
            <CircleDashed size={12} className="text-stone-400" />
          ) : passed ? (
            <CheckCircle2 size={12} className="text-emerald-600" />
          ) : (
            <XCircle size={12} className="text-red-500" />
          )}
          {joint.name}
        </span>
        <span className="font-mono text-xs text-stone-500 tabular-nums">
          {joint.position.toFixed(3)} → {joint.target.toFixed(3)}
        </span>
      </div>
      <div className="relative h-3.5 w-full overflow-hidden rounded-full border border-stone-200 bg-stone-100">
        {/* centre line = zero position */}
        <span className="absolute left-1/2 top-0 h-full w-px bg-stone-300" />
        {/* error between commanded target and measured position */}
        <span
          className="absolute top-0 h-full bg-amber-200"
          style={{ left: `${left}%`, width: `${width}%` }}
        />
        {/* commanded target marker */}
        <span
          className="absolute top-0 h-full w-1 bg-sky-500 shadow-xs"
          style={{ left: `calc(${target}% - 1px)` }}
          title={`target ${joint.target.toFixed(3)}`}
        />
        {/* measured position */}
        <span
          className="absolute top-0 h-full w-1 rounded-full bg-emerald-500 shadow-xs"
          style={{ left: `calc(${pos}% - 2px)` }}
          title={`position ${joint.position.toFixed(3)}`}
        />
      </div>
      <div className="flex justify-between px-0.5 font-mono text-[11px] text-stone-500 tabular-nums">
        <span>-{scale}</span>
        <span>v {joint.velocity.toFixed(2)} · τ {joint.effort.toFixed(2)}</span>
        <span>+{scale}</span>
      </div>
    </div>
  )
}

function locomotionLabel(test: TestResult): string {
  const objective = test.objective ?? 'go_to'
  if (objective === 'kick') return 'Kick the ball'
  if (objective === 'touch') return 'Touch the wall'
  if (objective === 'wander') return 'Wander'
  if (test.target_body) return `Walk to the ${propLabel(test.target_body)}`
  return 'Walk to flag'
}

function TestRow({ test, live, estopLimit }: { test: TestResult; live: boolean; estopLimit?: number }) {
  const isEstop = test.kind === 'estop'
  const isLocomotion = test.kind === 'locomotion'
  const isKick = isLocomotion && (test.objective ?? 'go_to') === 'kick'
  return (
    <div
      className={clsx(
        'flex flex-wrap items-center gap-x-2.5 gap-y-1 rounded-lg border px-3 py-1.5 font-mono text-xs leading-normal',
        test.passed
          ? 'border-emerald-200 bg-emerald-50 text-emerald-900'
          : 'border-red-200 bg-red-50 text-red-900',
      )}
    >
      {test.passed ? <CheckCircle2 size={12} className="shrink-0 text-emerald-600" /> : <XCircle size={12} className="shrink-0 text-red-500" />}
      <span className="font-sans font-semibold">
        {isEstop ? 'Emergency stop' : isLocomotion ? locomotionLabel(test) : (test.joint ?? test.id)}
      </span>
      {isEstop ? (
        <span className="text-stone-600 tabular-nums">
          stop latency{' '}
          {test.settling_time != null ? `${test.settling_time.toFixed(3)}s` : '—'}
          {estopLimit !== undefined ? ` / limit ${estopLimit.toFixed(2)}s` : ''} · vmax{' '}
          {test.max_velocity?.toFixed(2) ?? '—'}
        </span>
      ) : isLocomotion ? (
        <span className="text-stone-600 tabular-nums">
          {test.phases && test.phases > 1 ? `phase ${test.phase ?? 1}/${test.phases} · ` : ''}
          walked {test.distance_m?.toFixed(2) ?? '—'} m
          {isKick ? (
            <>
              {' '}· ball {test.ball_displacement_m?.toFixed(2) ?? '—'} m
              {test.ball_touched ? ' (touched)' : ' (not touched)'}
            </>
          ) : (
            <>
              {' '}· closest {test.min_flag_distance_m?.toFixed(2) ?? '—'} m · stopped{' '}
              {test.final_flag_distance_m?.toFixed(2) ?? '—'} m / radius {test.tolerance.toFixed(2)} m
            </>
          )}
          {test.phase_budget_s ? (
            <>
              {' '}· {test.phase_duration_s?.toFixed(1) ?? '—'}s / {test.phase_budget_s.toFixed(0)}s window
            </>
          ) : null}
          {test.fallen ? ' · FELL' : ''}
          {test.contacts > 0 ? ` · contacts ${test.contacts}` : ''}
        </span>
      ) : (
        <>
          <span className="text-stone-600 tabular-nums">
            target {test.target?.toFixed(3) ?? '—'} · got {test.final_position?.toFixed(3) ?? '—'} · err{' '}
            {test.final_error.toFixed(3)} / tol {test.tolerance.toFixed(3)}
          </span>
          <span className="text-stone-500 tabular-nums">
            peak err {test.max_error?.toFixed(3) ?? '—'} · vmax {test.max_velocity?.toFixed(2) ?? '—'} ·{' '}
            {test.duration?.toFixed(2) ?? '—'}s
            {test.settling_time != null ? ` · settle ${test.settling_time.toFixed(2)}s` : ''}
            {test.contacts > 0 ? ` · contacts ${test.contacts}` : ''}
          </span>
        </>
      )}
      {!test.passed && test.reason && (
        <span className="w-full font-sans text-red-700">{test.reason}</span>
      )}
      {live && <span className="font-sans text-stone-500">(executing)</span>}
    </div>
  )
}

function PlannedRow({ test }: { test: PlannedTest }) {
  return (
    <div className="flex items-center gap-2 rounded-lg border border-border-subtle/70 bg-stone-50 px-3 py-1.5 font-mono text-xs text-stone-500">
      <CircleDashed size={12} className="shrink-0 text-stone-400" />
      <span className="font-sans font-medium text-stone-700">{test.joint}</span>
      <span className="tabular-nums">
        target {test.target.toFixed(3)} · tol {test.tolerance.toFixed(3)}
      </span>
      <span className="ml-auto font-sans text-[11px] text-stone-400">pending</span>
    </div>
  )
}

/**
 * MuJoCo viewport: the run's objective evidence is joint motion, not an arena
 * trajectory. Each actuated joint is drawn as a gauge with its commanded target,
 * and the automated test suite is listed with its measured result.
 */
export function JointViewport() {
  const runDetail = useExperimentStore((s) => s.runDetail)
  const telemetry = useExperimentStore((s) => s.telemetry)
  // 3D model vs joint gauges share the upper pane; the automated tests below
  // stay visible in both views. Null = follow the run (live/preview duck by
  // default, gauges for rig runs that stream no body poses).
  const [viewOverride, setViewOverride] = useState<'model' | 'joints' | null>(null)
  // The automated-tests panel floats over the simulator and can be closed;
  // the header badge and the reopen pill keep the verdict reachable.
  const [testsOpen, setTestsOpen] = useState(true)

  const world = isMujocoWorld(runDetail?.world_config) ? runDetail.world_config : null
  const latest = telemetry.length > 0 ? telemetry[telemetry.length - 1] : null
  const joints = latest?.joints ?? []
  const hasBodies = (latest?.bodies?.length ?? 0) > 0
  const view = viewOverride ?? (hasBodies || joints.length === 0 ? 'model' : 'joints')
  const liveTestId = latest?.test_id ?? null
  const tests = runDetail?.tests ?? []
  const planned = world?.tests ?? []

  // The joint range is not part of the run payload, so scale each gauge to the
  // largest excursion the scenario targets or the run has actually reached.
  // Deriving it from the planned suite keeps the scale stable mid-run.
  const scales = useMemo(() => {
    const map = new Map<string, number>()
    const bump = (name: string, value: number) => {
      map.set(name, Math.max(map.get(name) ?? 0, Math.abs(value)))
    }
    for (const test of planned) bump(test.joint, test.target)
    for (const test of tests) {
      if (test.joint === undefined) continue
      if (test.target !== undefined) bump(test.joint, test.target)
      if (test.final_position !== undefined) bump(test.joint, test.final_position)
    }
    for (const joint of joints) {
      bump(joint.name, joint.position)
      bump(joint.name, joint.target)
    }
    return map
  }, [planned, tests, joints])

  // The e-stop and locomotion cases are not judged by one joint's position,
  // so they must not colour any joint's gauge.
  const passedByJoint = new Map<string, boolean>()
  for (const test of tests) {
    if (test.kind === 'estop' || test.kind === 'locomotion' || test.joint === undefined) continue
    const prior = passedByJoint.get(test.joint)
    passedByJoint.set(test.joint, prior === undefined ? test.passed : prior && test.passed)
  }

  const estop = latest?.estop_active === true
  const contacts = latest?.contacts ?? 0
  const peakForce = latest?.max_contact_force ?? 0
  const fallen = latest?.fallen === true
  const trunkZ = latest?.trunk_z ?? null
  const expectedContacts = latest?.expected_contacts ?? 0
  const sensors = latest?.sensors ?? null
  const finished = runDetail !== null && tests.length > 0
  const passed = tests.filter((t) => t.passed).length

  return (
    <div className="flex h-full w-full flex-col overflow-hidden bg-base">
      <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-border-subtle/80 bg-sidebar/50 px-3.5 py-2 text-xs backdrop-blur-xs">
        <span className="inline-flex items-center gap-1.5 rounded-lg border border-sky-200 bg-sky-50 px-2.5 py-1 text-xs font-semibold text-sky-700">
          <Cpu size={12} />
          {BACKEND_LABELS.mujoco}
        </span>
        {contacts > 0 && (
          <span className="rounded-lg bg-amber-500 px-2.5 py-1 text-xs font-bold text-white shadow-xs">
            Contacts {contacts} · {peakForce.toFixed(1)} N
          </span>
        )}
        {expectedContacts > 0 && (
          <span className="rounded-md bg-stone-100 px-2 py-0.5 text-xs text-stone-600" title="Foot-sole ground touches (expected, not collisions)">
            Feet ×{expectedContacts}
          </span>
        )}
        {fallen && (
          <span className="inline-flex items-center gap-1.5 rounded-lg bg-red-500 px-2.5 py-1 text-xs font-bold text-white shadow-xs">
            <AlertTriangle size={12} />
            FALLEN{trunkZ !== null ? ` · trunk ${trunkZ.toFixed(3)} m` : ''}
          </span>
        )}
        {estop && (
          <span className="inline-flex items-center gap-1.5 rounded-lg bg-red-500 px-2.5 py-1 text-xs font-bold text-white shadow-xs">
            <AlertOctagon size={12} />
            E-STOP
          </span>
        )}
        <div
          className="ml-auto flex items-center gap-1 rounded-lg border border-border-subtle/80 bg-white p-0.5"
          role="tablist"
          aria-label="Simulator view"
        >
          <button
            type="button"
            role="tab"
            aria-selected={view === 'model'}
            onClick={() => setViewOverride('model')}
            title="Show the 3D MicroDuck view"
            className={clsx(
              'inline-flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-semibold transition-all',
              view === 'model' ? 'bg-sky-500 text-white shadow-xs' : 'text-stone-500 hover:text-stone-800',
            )}
          >
            <Box size={12} />
            World
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={view === 'joints'}
            onClick={() => setViewOverride('joints')}
            title="Show the joint telemetry gauges"
            className={clsx(
              'inline-flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-semibold transition-all',
              view === 'joints' ? 'bg-sky-500 text-white shadow-xs' : 'text-stone-500 hover:text-stone-800',
            )}
          >
            <Activity size={12} />
            Joints
          </button>
        </div>
        {finished && (
          <span
            className={clsx(
              'rounded-lg px-2.5 py-1 text-xs font-bold shadow-xs',
              passed === tests.length
                ? 'bg-emerald-500 text-white'
                : 'bg-red-500 text-white',
            )}
          >
            Tests {passed}/{tests.length}
          </span>
        )}
      </div>

      <div className="relative min-h-0 flex-1">
        <RunPhaseOverlay />
        {view === 'model' ? (
          <Suspense fallback={null}>
            <MicroduckViewport />
          </Suspense>
        ) : (
          <div className="h-full overflow-y-auto p-3.5">
            {joints.length === 0 ? (
              <div className="flex h-full flex-col items-center justify-center gap-2 p-6 text-center">
                <Activity size={20} className="text-stone-400" />
                <p className="text-xs text-stone-500">No joint telemetry yet.</p>
                <p className="max-w-xs text-xs text-stone-400 leading-relaxed">
                  Start a run to stream measured joint positions, velocities, and the
                  automated test results here.
                </p>
              </div>
            ) : (
              <div className="space-y-3">
                {joints.map((joint) => (
                  <JointGauge
                    key={joint.name}
                    joint={joint}
                    scale={niceScale(scales.get(joint.name) ?? 1)}
                    passed={passedByJoint.get(joint.name) ?? null}
                  />
                ))}
              </div>
            )}
          </div>
        )}

        {(sensors || tests.length > 0 || planned.length > 0) && (
          <div className="absolute inset-x-3 bottom-3 flex max-h-[60%] flex-col gap-1.5">
            {sensors && (
              <div className="pointer-events-none flex shrink-0 flex-col gap-1 self-start rounded-lg border border-border-subtle/60 bg-white/85 px-2.5 py-1 font-mono text-[11px] text-stone-600 shadow-xl backdrop-blur-md">
                {Object.entries(sensors).map(([name, values]) => (
                  <span key={name} className="whitespace-nowrap">
                    {name} [{values.map((v) => v.toFixed(2)).join(' ')}]
                  </span>
                ))}
              </div>
            )}
            {(tests.length > 0 || planned.length > 0) &&
              (testsOpen ? (
                <div className="min-h-0 space-y-1.5 overflow-y-auto rounded-xl border border-border-subtle/80 bg-white/90 p-3 shadow-2xl backdrop-blur-md">
                  <div className="flex items-center gap-2">
                    <p className="text-xs font-semibold uppercase tracking-wider text-stone-500">
                      Automated tests
                    </p>
                    {finished && (
                      <span
                        className={clsx(
                          'rounded-md px-2 py-0.5 font-mono text-xs font-bold',
                          passed === tests.length
                            ? 'bg-emerald-500 text-white'
                            : 'bg-red-500 text-white',
                        )}
                      >
                        {passed}/{tests.length}
                      </span>
                    )}
                    <button
                      type="button"
                      onClick={() => setTestsOpen(false)}
                      title="Hide the automated tests panel"
                      aria-label="Hide automated tests"
                      className="ml-auto rounded p-1 text-stone-400 hover:bg-stone-100 hover:text-stone-700"
                    >
                      <X size={14} />
                    </button>
                  </div>
                  {tests.map((test) => (
                    <TestRow
                      key={test.id}
                      test={test}
                      live={test.id === liveTestId}
                      estopLimit={world?.estop_latency_limit}
                    />
                  ))}
                  {planned
                    .filter((test) => !tests.some((done) => done.id === test.id))
                    .map((test) => (
                      <PlannedRow key={test.id} test={test} />
                    ))}
                </div>
              ) : (
                <button
                  type="button"
                  onClick={() => setTestsOpen(true)}
                  title="Show the automated tests panel"
                  className="inline-flex items-center gap-2 self-start rounded-lg border border-border-subtle/80 bg-white/90 px-3 py-1.5 text-xs font-medium text-stone-700 shadow-xl backdrop-blur-md hover:bg-white transition-colors"
                >
                  <ListChecks size={14} />
                  Tests{finished ? ` ${passed}/${tests.length}` : ''}
                </button>
              ))}
          </div>
        )}
      </div>
    </div>
  )
}
