import { Suspense, lazy, useEffect, useState } from 'react'
import { ChevronDown, ChevronUp, X } from 'lucide-react'
import { useWorkspaceStore } from '../../stores/workspace'
import { useExperimentStore } from '../../stores/experiment'
import { isTerminalStatus } from '../../api/types'
import { SIMULATOR_PANEL_ID, SimControls } from '../simulator/SimControls'
import { SimViewportFallback } from '../simulator/SimViewportFallback'
import { ControllerEditor } from '../editor/ControllerEditor'
import { ResultsBody } from '../runs/ResultsCompare'
import { LogsBody } from '../diagnostics/LogsPanel'
import { clsx } from '../../components/clsx'

const SimViewport = lazy(() =>
  import('../simulator/SimViewport').then((m) => ({ default: m.SimViewport })),
)

type BottomTab = 'results' | 'logs'

export function WorkspaceCenter() {
  const centerTab = useWorkspaceStore((s) => s.centerTab)
  const setCenterTab = useWorkspaceStore((s) => s.setCenterTab)
  const activeFilePath = useWorkspaceStore((s) => s.activeFilePath)
  const openPaths = useWorkspaceStore((s) => s.openPaths)
  const openFile = useWorkspaceStore((s) => s.openFile)
  const closeFile = useWorkspaceStore((s) => s.closeFile)
  const editorDirty = useWorkspaceStore((s) => s.editorDirty)

  const activeRunId = useExperimentStore((s) => s.activeRunId)
  const runDetail = useExperimentStore((s) => s.runDetail)
  const comparison = useExperimentStore((s) => s.comparison)
  const setBaseline = useExperimentStore((s) => s.setBaseline)

  const [bottomTab, setBottomTab] = useState<BottomTab>('results')
  const [bottomOpen, setBottomOpen] = useState(true)
  const [autoScroll, setAutoScroll] = useState(true)
  const runnerStopped = !runDetail || isTerminalStatus(runDetail.status)

  // A live run streams here: follow the logs until it stops.
  useEffect(() => {
    if (activeRunId && runDetail && !isTerminalStatus(runDetail.status)) {
      setBottomTab('logs')
    }
  }, [activeRunId, runDetail])

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3.5">
      <section id={SIMULATOR_PANEL_ID} className="flex min-h-[380px] flex-1 flex-col overflow-hidden rounded-xl border border-border-subtle bg-elevated shadow-sm">
        <div
          className="flex shrink-0 items-center gap-1.5 overflow-x-auto border-b border-border-subtle/80 bg-sidebar/60 px-3 pt-2"
          role="tablist"
          aria-label="Simulator and open files"
        >
          <button
            type="button"
            role="tab"
            aria-selected={centerTab === 'simulator'}
            onClick={() => setCenterTab('simulator')}
            className={clsx(
              'shrink-0 rounded-t-lg px-3.5 py-1.5 text-xs font-medium transition-all',
              centerTab === 'simulator'
                ? 'bg-elevated font-semibold text-stone-800 shadow-xs'
                : 'text-stone-400 hover:bg-stone-100 hover:text-stone-700',
            )}
          >
            Simulator
          </button>
          {openPaths.map((path) => {
            const selected = centerTab === 'code' && path === activeFilePath
            const dirty = selected && editorDirty
            return (
              <div
                key={path}
                role="tab"
                aria-selected={selected}
                className={clsx(
                  'flex max-w-[220px] shrink-0 items-center rounded-t-lg py-1.5 pl-3 pr-1.5 font-mono text-xs transition-all',
                  selected
                    ? 'bg-elevated font-medium text-stone-800 shadow-xs'
                    : 'text-stone-400 hover:bg-stone-100 hover:text-stone-700',
                )}
              >
                <button
                  type="button"
                  onClick={() => void openFile(path)}
                  title={path}
                  className="min-w-0 flex-1 truncate text-left"
                >
                  {path.split('/').pop()}
                  {dirty ? ' ●' : ''}
                </button>
                <button
                  type="button"
                  onClick={() => closeFile(path)}
                  title={`Close ${path}`}
                  aria-label={`Close ${path}`}
                  className="ml-1 rounded p-0.5 text-stone-400 hover:bg-stone-200 hover:text-stone-700"
                >
                  <X size={12} />
                </button>
              </div>
            )
          })}
        </div>
        <div className="relative min-h-0 flex-1">
          {centerTab === 'simulator' ? (
            <div className="flex h-full flex-col">
              <SimControls />
              <div className="relative min-h-0 flex-1">
                <Suspense fallback={<SimViewportFallback />}>
                  <SimViewport />
                </Suspense>
              </div>
            </div>
          ) : (
            <ControllerEditor />
          )}
        </div>
      </section>

      {bottomOpen ? (
        <section className="flex max-h-[38%] min-h-[180px] shrink-0 flex-col overflow-hidden rounded-xl border border-border-subtle bg-elevated shadow-sm">
          <div
            className="flex shrink-0 items-center gap-1.5 border-b border-border-subtle/80 bg-sidebar/60 px-3 pt-2"
            role="tablist"
            aria-label="Results or logs"
          >
            <button
              type="button"
              role="tab"
              aria-selected={bottomTab === 'results'}
              onClick={() => setBottomTab('results')}
              title={runnerStopped ? 'Evaluation results' : 'Results appear when the run stops'}
              className={clsx(
                'rounded-t-lg px-3.5 py-1.5 text-xs font-medium transition-all',
                bottomTab === 'results'
                  ? 'bg-elevated font-semibold text-stone-800 shadow-xs'
                  : 'text-stone-400 hover:bg-stone-100 hover:text-stone-700',
              )}
            >
              Results
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={bottomTab === 'logs'}
              onClick={() => setBottomTab('logs')}
              className={clsx(
                'rounded-t-lg px-3.5 py-1.5 text-xs font-medium transition-all',
                bottomTab === 'logs'
                  ? 'bg-elevated font-semibold text-stone-800 shadow-xs'
                  : 'text-stone-400 hover:bg-stone-100 hover:text-stone-700',
              )}
            >
              Logs
            </button>
            <div className="ml-auto flex items-center gap-3 pb-1.5 pr-1.5">
              {bottomTab === 'results' && comparison?.baseline && (
                <button
                  type="button"
                  onClick={() => setBaseline(null)}
                  className="text-xs font-medium text-stone-400 hover:text-stone-700 hover:underline"
                  title="Clear the baseline run"
                >
                  Clear baseline
                </button>
              )}
              {bottomTab === 'logs' && (
                <label className="flex cursor-pointer items-center gap-1.5 text-xs text-stone-500 hover:text-stone-700">
                  <input
                    type="checkbox"
                    checked={autoScroll}
                    onChange={(e) => setAutoScroll(e.target.checked)}
                    className="accent-amber-600"
                  />
                  Autoscroll
                </label>
              )}
              <button
                type="button"
                onClick={() => setBottomOpen(false)}
                title="Minimize panel (simulator and code go full height)"
                aria-label="Minimize results and logs"
                className="rounded p-1 text-stone-400 hover:bg-stone-200 hover:text-stone-700"
              >
                <ChevronDown size={14} />
              </button>
            </div>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto">
            {bottomTab === 'results' ? (
              runnerStopped ? (
                <ResultsBody />
              ) : (
                <p className="p-3.5 text-xs text-stone-400">
                  The run is still going — results appear here when it stops.
                </p>
              )
            ) : (
              <LogsBody autoScroll={autoScroll} />
            )}
          </div>
        </section>
      ) : (
        <div className="flex shrink-0 items-center gap-1.5 rounded-xl border border-border-subtle bg-elevated px-3 py-1.5 shadow-sm">
          <button
            type="button"
            onClick={() => {
              setBottomTab('results')
              setBottomOpen(true)
            }}
            className="rounded-lg px-2.5 py-1 text-xs font-medium text-stone-400 transition-colors hover:bg-stone-100 hover:text-stone-700"
          >
            Results
          </button>
          <button
            type="button"
            onClick={() => {
              setBottomTab('logs')
              setBottomOpen(true)
            }}
            className="rounded-lg px-2.5 py-1 text-xs font-medium text-stone-400 transition-colors hover:bg-stone-100 hover:text-stone-700"
          >
            Logs
          </button>
          <button
            type="button"
            onClick={() => setBottomOpen(true)}
            title="Expand panel"
            aria-label="Expand results and logs"
            className="ml-auto rounded p-1 text-stone-400 hover:bg-stone-200 hover:text-stone-700"
          >
            <ChevronUp size={14} />
          </button>
        </div>
      )}
    </div>
  )
}
