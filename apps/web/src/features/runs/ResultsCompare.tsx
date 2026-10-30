import { useEffect } from 'react'
import { Cpu, GitCompare, TriangleAlert } from 'lucide-react'
import { useExperimentStore } from '../../stores/experiment'
import { LoadingRow, ErrorNotice } from '../../components/Panel'
import { StatusPill } from '../../components/StatusPill'
import { clsx } from '../../components/clsx'
import { BACKEND_LABELS, isMujocoBackend, type ComparisonRunSummary } from '../../api/types'

function fmt(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return value.toFixed(digits)
}

function yesNo(value: boolean | undefined): string {
  if (value === undefined) return '—'
  return value ? 'yes' : 'no'
}

interface Row {
  label: string
  base: string
  cur: string
}

/** Metrics both backends report; always safe to compare. */
function commonRows(baseline: ComparisonRunSummary | null, current: ComparisonRunSummary | null): Row[] {
  const base = baseline?.metrics ?? null
  const cur = current?.metrics ?? null
  return [
    {
      label: 'Evaluator success',
      base: yesNo(base?.success),
      cur: yesNo(cur?.success),
    },
    { label: 'Run duration (s)', base: fmt(base?.completion_time), cur: fmt(cur?.completion_time) },
    {
      label: 'E-stop latency (s)',
      base: fmt(base?.emergency_stop_latency),
      cur: fmt(cur?.emergency_stop_latency),
    },
  ]
}

/** MuJoCo runs are judged by the automated joint-test suite. */
function mujocoRows(baseline: ComparisonRunSummary | null, current: ComparisonRunSummary | null): Row[] {
  const base = baseline?.metrics ?? null
  const cur = current?.metrics ?? null
  const tests = (summary: ComparisonRunSummary | null) => {
    if (!summary) return '—'
    if (summary.tests_total === 0) return '—'
    return `${summary.tests_passed}/${summary.tests_total}`
  }
  return [
    ...commonRows(baseline, current),
    { label: 'Tests passed', base: tests(baseline), cur: tests(current) },
    {
      label: 'Max joint error (rad/m)',
      base: fmt(base?.max_joint_error, 3),
      cur: fmt(cur?.max_joint_error, 3),
    },
    {
      label: 'Disqualifying contacts',
      base: fmt(base?.collision_count, 0),
      cur: fmt(cur?.collision_count, 0),
    },
  ]
}

/** Legacy 2D runs are judged by reaching the goal in the arena. */
function arenaRows(baseline: ComparisonRunSummary | null, current: ComparisonRunSummary | null): Row[] {
  const base = baseline?.metrics ?? null
  const cur = current?.metrics ?? null
  return [
    ...commonRows(baseline, current),
    { label: 'Collisions', base: fmt(base?.collision_count, 0), cur: fmt(cur?.collision_count, 0) },
    { label: 'Final goal distance (m)', base: fmt(base?.goal_distance), cur: fmt(cur?.goal_distance) },
    { label: 'Path length (m)', base: fmt(base?.path_length), cur: fmt(cur?.path_length) },
  ]
}

function verdict(
  baseline: ComparisonRunSummary | null,
  current: ComparisonRunSummary | null,
  mixedBackends: boolean,
): string {
  if (!current?.metrics) {
    return 'Current run has no evaluator metrics yet — run the simulation to completion first.'
  }
  if (!baseline?.metrics) {
    return 'No baseline selected. Pick a run in the history and set it as baseline.'
  }
  if (mixedBackends) {
    return 'These runs used different simulation backends, so the metrics measure different things. Only the common rows are directly comparable.'
  }
  const cur = current.metrics
  const base = baseline.metrics
  const testNote = isMujocoBackend(current.backend) && current.tests_total > 0
    ? ` Automated tests: ${current.tests_passed}/${current.tests_total} passed.`
    : ''
  if (cur.success && !base.success) {
    return `Improved: the corrected run now passes the evaluator.${testNote}`
  }
  if (cur.success && base.success) {
    return `Both runs pass. Compare the raw metrics for the measured difference.${testNote}`
  }
  if (!cur.success && base.success) {
    return `Regressed: the baseline passed but the current run does not.${testNote}`
  }
  return `Neither run passes the evaluator. Check the diagnostics for the remaining failure.${testNote}`
}

function BackendBadge({ backend }: { backend: string | undefined }) {
  if (!backend) return null
  const mujoco = isMujocoBackend(backend)
  return (
    <span
      className={clsx(
        'inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5 text-xs font-semibold tracking-wide',
        mujoco
          ? 'border-sky-200 bg-sky-50 text-sky-700'
          : 'border-stone-200 bg-stone-50 text-stone-600',
      )}
    >
      <Cpu size={11} />
      {BACKEND_LABELS[mujoco ? 'mujoco' : 'differential_drive']}
    </span>
  )
}

export function ResultsBody() {
  const activeRunId = useExperimentStore((s) => s.activeRunId)
  const baselineRunId = useExperimentStore((s) => s.baselineRunId)
  const comparison = useExperimentStore((s) => s.comparison)
  const comparisonLoading = useExperimentStore((s) => s.comparisonLoading)
  const comparisonError = useExperimentStore((s) => s.comparisonError)
  const fetchComparison = useExperimentStore((s) => s.fetchComparison)

  useEffect(() => {
    if (activeRunId) void fetchComparison()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeRunId, baselineRunId])

  const baseline = comparison?.baseline ?? null
  const current = comparison?.current ?? null
  const currentIsMujoco = isMujocoBackend(current?.backend)
  const mixedBackends =
    !!baseline && !!current && baseline.backend !== current.backend

  const rows = mixedBackends
    ? commonRows(baseline, current)
    : currentIsMujoco
      ? mujocoRows(baseline, current)
      : arenaRows(baseline, current)

  return (
    <div className="space-y-3 p-3.5">
      {comparisonLoading && <LoadingRow label="Loading comparison…" />}
      {comparisonError && (
        <ErrorNotice message={comparisonError} onRetry={() => void fetchComparison()} />
      )}
      {!comparisonLoading && !comparisonError && !comparison && (
        <div className="flex items-center gap-2 p-3 text-xs text-stone-500">
          <GitCompare size={16} className="text-stone-400" />
          <p>Select a run to see its evaluated metrics. Set another run as baseline to compare.</p>
        </div>
      )}
      {comparison && (
        <>
          <div className="flex flex-wrap items-center gap-2.5 text-xs">
            {comparison.baseline ? (
              <span className="inline-flex flex-wrap items-center gap-2">
                <span className="text-stone-500">
                  Baseline <span className="rounded bg-stone-100 px-1.5 py-0.5 font-mono text-stone-800">{comparison.baseline.run_id.slice(0, 8)}</span>
                </span>
                <BackendBadge backend={comparison.baseline.backend} />
                <StatusPill status={comparison.baseline.status} />
              </span>
            ) : (
              <span className="text-stone-400">No baseline</span>
            )}
            <span className="text-stone-400 font-bold">→</span>
            <span className="inline-flex flex-wrap items-center gap-2">
              <span className="text-stone-500">
                Current <span className="rounded bg-stone-100 px-1.5 py-0.5 font-mono text-stone-800">{comparison.current.run_id.slice(0, 8)}</span>
              </span>
              <BackendBadge backend={comparison.current.backend} />
              <StatusPill status={comparison.current.status} />
            </span>
          </div>
          {mixedBackends && (
            <p className="flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 p-2.5 text-xs text-amber-900 leading-relaxed">
              <TriangleAlert size={14} className="mt-0.5 shrink-0 text-amber-600" />
              <span>
                Baseline and current used different simulation backends. Their
                backend-specific metrics measure different things, so only the shared rows are shown.
                Run both on the same simulation backend to compare them directly.
              </span>
            </p>
          )}
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-border-subtle/80 text-left text-[11px] font-semibold uppercase tracking-wider text-stone-500">
                <th className="py-1.5 pr-3 font-semibold">Metric</th>
                <th className="py-1.5 pr-3 font-semibold">Baseline</th>
                <th className="py-1.5 font-semibold">Current</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border-subtle/50 font-mono text-xs">
              {rows.map((row) => (
                <tr key={row.label} className="transition-colors hover:bg-stone-50">
                  <td className="py-2 pr-3 font-sans text-stone-800">{row.label}</td>
                  <td className="py-2 pr-3 text-stone-500 tabular-nums">{row.base}</td>
                  <td className="py-2 font-semibold text-stone-900 tabular-nums">{row.cur}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="rounded-xl border border-border-subtle/70 bg-stone-50 p-3 text-xs leading-relaxed text-stone-800 shadow-xs">
            {verdict(baseline, current, mixedBackends)}
          </div>
          {comparison.current.failure_summary && (
            <p className="text-xs text-red-700 font-medium">Evaluator: {comparison.current.failure_summary}</p>
          )}
        </>
      )}
    </div>
  )
}
