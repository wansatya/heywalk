import { FlaskConical, Star } from 'lucide-react'
import { useWorkspaceStore } from '../../stores/workspace'
import { useExperimentStore } from '../../stores/experiment'
import { StatusPill } from '../../components/StatusPill'
import { ActionButton, ErrorNotice, LoadingRow, Panel } from '../../components/Panel'
import { clsx } from '../../components/clsx'

function formatTime(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

export function RunHistory() {
  const activeProjectId = useWorkspaceStore((s) => s.activeProjectId)
  const runs = useExperimentStore((s) => s.runs)
  const activeRunId = useExperimentStore((s) => s.activeRunId)
  const baselineRunId = useExperimentStore((s) => s.baselineRunId)
  const selectRun = useExperimentStore((s) => s.selectRun)
  const setBaseline = useExperimentStore((s) => s.setBaseline)
  const fetchRuns = useExperimentStore((s) => s.fetchRuns)
  const runsLoading = useExperimentStore((s) => s.runsLoading)
  const runsError = useExperimentStore((s) => s.runsError)

  return (
    <Panel
      title="Run history"
      className="flex-1"
      actions={
        activeProjectId ? (
          <ActionButton onClick={() => void fetchRuns(activeProjectId)} disabled={runsLoading} variant="ghost" title="Refresh run list">
            Refresh
          </ActionButton>
        ) : undefined
      }
    >
      {runsLoading && runs.length === 0 && <LoadingRow label="Loading runs…" />}
      {runsError && <ErrorNotice message={runsError} onRetry={activeProjectId ? () => void fetchRuns(activeProjectId) : undefined} />}
      {!runsLoading && !runsError && runs.length === 0 && (
        <div className="flex flex-col items-center gap-2 p-6 text-center text-xs text-stone-400">
          <FlaskConical size={20} />
          <p>No runs yet. Generate a controller, then press Start in the run bar.</p>
        </div>
      )}
      <ul className="divide-y divide-border-subtle/50">
        {runs.map((run) => {
          const isActive = run.id === activeRunId
          const isBaseline = run.id === baselineRunId
          const isCorrected = run.parent_run_id !== null
          return (
            <li key={run.id} className={clsx(isActive && 'bg-amber-50')}>
              <button
                type="button"
                onClick={() => void selectRun(run.id)}
                className="flex w-full items-center gap-2.5 p-3 text-left text-xs transition-colors hover:bg-stone-50"
              >
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="truncate font-mono text-xs font-semibold tracking-tight text-stone-700" title={run.id}>
                      {run.id.slice(0, 8)}
                    </span>
                    {isBaseline && (
                      <span className="inline-flex items-center gap-1 rounded border border-sky-200 bg-sky-50 px-1.5 py-0.5 text-[10px] font-semibold tracking-wide text-sky-700">
                        <Star size={10} /> baseline
                      </span>
                    )}
                    {isCorrected && (
                      <span className="rounded border border-emerald-200 bg-emerald-50 px-1.5 py-0.5 text-[10px] font-semibold tracking-wide text-emerald-700">
                        corrected
                      </span>
                    )}
                  </div>
                  <div className="mt-1 text-xs text-stone-400">
                    v{run.controller_version} · seed {run.seed} · {formatTime(run.started_at)}
                  </div>
                </div>
                <StatusPill status={run.status} />
              </button>
              {isActive && !isBaseline && (
                <div className="px-3 pb-2.5 pt-0.5">
                  <button
                    type="button"
                    onClick={() => setBaseline(run.id)}
                    className="text-xs font-medium text-sky-600 transition-colors hover:text-sky-500 hover:underline"
                  >
                    Set as baseline for comparison
                  </button>
                </div>
              )}
            </li>
          )
        })}
      </ul>
    </Panel>
  )
}
