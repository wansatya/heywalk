import { Bot, CheckCircle2, FlaskConical, Gauge, RotateCcw, Wrench, XCircle } from 'lucide-react'
import { useWorkspaceStore } from '../../stores/workspace'
import { useExperimentStore } from '../../stores/experiment'
import { ActionButton, Panel, LoadingRow } from '../../components/Panel'
import { DiffView } from '../../components/DiffView'
import { TraceView } from '../../components/TraceView'
import { clsx } from '../../components/clsx'
import { useElapsedSeconds } from '../../components/useElapsedSeconds'
import { RobotSummary } from './RobotSummary'
import { BACKEND_LABELS, hasEvaluatorSuccess, isTerminalStatus, isUnverifiedSuccess, testPassRate, type Run, type TestResult } from '../../api/types'

interface PlainSummary {
  headline: string
  detail: string
  hint: string
  tone: 'ok' | 'bad' | 'neutral'
}

/** Plain-language verdict: what happened, in words anyone can follow. */
function plainSummary(run: Run): PlainSummary | null {
  const tests: TestResult[] = run.tests ?? []
  const locomotion = tests.find((t) => t.kind === 'locomotion')
  const failed = tests.filter((t) => !t.passed)
  switch (run.status) {
    case 'queued':
    case 'preparing':
      return { headline: 'Getting ready…', detail: 'The run is queued and the simulator is preparing.', hint: 'It will start moving shortly.', tone: 'neutral' }
    case 'running':
      return { headline: 'Run in progress…', detail: 'The robot is moving in the simulator right now.', hint: 'Watch the simulator, or stop the run from the run bar.', tone: 'neutral' }
    case 'evaluating':
      return { headline: 'Checking the result…', detail: 'The run finished moving and the evaluator is scoring it.', hint: 'Results appear below in a moment.', tone: 'neutral' }
    case 'cancelled':
      return { headline: 'Stopped early.', detail: 'You stopped this run before it finished, so there is no pass or fail.', hint: 'Start a new run whenever you are ready.', tone: 'neutral' }
    case 'timeout':
      return { headline: 'Ran out of time.', detail: 'The time limit ended before the run finished.', hint: 'Try a longer time budget, or press Fix for ideas.', tone: 'bad' }
    case 'error':
      return {
        headline: 'Something went wrong.',
        detail: run.failure_summary ?? 'The run failed before it could be scored.',
        hint: 'Open the Logs tab for details, then fix the task or controller and retry.',
        tone: 'bad',
      }
    case 'succeeded': {
      const detail = locomotion?.reason
        ?? (tests.length > 0 ? `All ${tests.length} checks passed.` : 'The evaluator confirmed the task is done.')
      return {
        headline: 'Task completed successfully.',
        detail,
        hint: 'Compare with the baseline in Results, or export the project.',
        tone: 'ok',
      }
    }
    case 'failed': {
      const first = failed[0] ?? locomotion
      if (first?.fallen) {
        return {
          headline: 'The robot fell over.',
          detail: first.reason || 'It lost balance before finishing.',
          hint: 'Press Fix above to ask the AI for a steadier controller, then rerun.',
          tone: 'bad',
        }
      }
      if (first && first.contacts > 0) {
        return {
          headline: 'The robot bumped into something.',
          detail: first.reason || 'Contact with an obstacle ended the run.',
          hint: 'Press Fix above to ask the AI to steer around it, then rerun.',
          tone: 'bad',
        }
      }
      if (first?.outcome === 'duration_elapsed') {
        const window = first.phase_budget_s
        return {
          headline: 'Time ran out before it finished.',
          detail: first.reason || 'The robot kept going but never completed the task.',
          hint: window
            ? `It used this phase's whole ${window.toFixed(0)}s window (each phase gets its own). Press Fix above for a faster or more direct approach, then rerun.`
            : 'Press Fix above for a faster or more direct approach, then rerun.',
          tone: 'bad',
        }
      }
      return {
        headline: "It didn't reach its goal.",
        detail: first?.reason || run.failure_summary || 'The run ended without completing the task.',
        hint: 'Press Fix above to ask the AI what went wrong, then rerun.',
        tone: 'bad',
      }
    }
  }
  return null
}

function SectionLabel({ icon, text }: { icon: React.ReactNode; text: string }) {
  return (
    <p className="mb-2 flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wider text-stone-500">
      {icon}
      {text}
    </p>
  )
}

export function DiagnosticsPanel() {
  const generation = useWorkspaceStore((s) => s.generation)
  const generationTrace = useWorkspaceStore((s) => s.generationTrace)
  const generationHint = useWorkspaceStore((s) => s.generationHint)
  const fix = useWorkspaceStore((s) => s.fix)
  const fixTrace = useWorkspaceStore((s) => s.fixTrace)
  const fixHint = useWorkspaceStore((s) => s.fixHint)
  const fixBase = useWorkspaceStore((s) => s.fixBase)
  const fixRunId = useWorkspaceStore((s) => s.fixRunId)
  const fixFromRun = useWorkspaceStore((s) => s.fixFromRun)
  const dismissFix = useWorkspaceStore((s) => s.dismissFix)
  const activeProjectId = useWorkspaceStore((s) => s.activeProjectId)
  const modelRequests = useWorkspaceStore((s) => s.modelRequests)
  const robotInstall = useWorkspaceStore((s) => s.robotInstall)
  const fixing = fix.status === 'loading'
  const generating = generation.status === 'loading'
  const modelElapsed = useElapsedSeconds(fixing)
  const lastRequest = modelRequests[0] ?? null

  const activeRunId = useExperimentStore((s) => s.activeRunId)
  const runDetail = useExperimentStore((s) => s.runDetail)
  const detailLoading = useExperimentStore((s) => s.detailLoading)
  const rerunRun = useExperimentStore((s) => s.rerunRun)
  const runAction = useExperimentStore((s) => s.runAction)

  const evaluated = runDetail !== null && (runDetail.metrics !== null || runDetail.evaluation !== null)
  const success = runDetail !== null && hasEvaluatorSuccess(runDetail)
  const unverified = runDetail !== null && isUnverifiedSuccess(runDetail)
  const passRate = runDetail !== null ? testPassRate(runDetail) : null
  const runFinished = runDetail !== null && isTerminalStatus(runDetail.status)
  // Generation progress lives in the Task card; Diagnostics only shows the
  // reasoning once there is a result (or a fix/last-request row to display).
  const modelSectionVisible =
    fix.status !== 'idle' ||
    generation.status === 'success' ||
    generation.status === 'error' ||
    (lastRequest !== null && generation.status === 'idle' && fix.status === 'idle')

  return (
    <Panel
      title="Diagnostics"
      className="min-h-0 flex-1"
      actions={
        <ActionButton
          onClick={() => activeRunId && void fixFromRun(activeRunId)}
          disabled={!activeRunId || !runFinished || generating || fixing}
          title={
            !activeRunId
              ? 'Run the simulation first'
              : !runFinished
                ? 'Wait for the run to finish'
                : 'Ask the model to diagnose the run and propose a targeted fix'
          }
        >
          <Wrench size={14} />
          {fixing ? 'Diagnosing…' : 'Fix'}
        </ActionButton>
      }
    >
      <div className="space-y-4 p-3.5 text-xs">
        {detailLoading && <LoadingRow label="Loading run details…" />}

        {!runDetail && !detailLoading && (
          <p className="text-stone-400">No run selected. Start a simulation to collect evidence here.</p>
        )}

        {runDetail && (() => {
          const summary = plainSummary(runDetail)
          return summary ? (
            <div
              className={clsx(
                'rounded-lg border p-2.5 shadow-xs',
                summary.tone === 'ok' && 'border-emerald-200 bg-emerald-50',
                summary.tone === 'bad' && 'border-red-200 bg-red-50',
                summary.tone === 'neutral' && 'border-border-subtle/60 bg-stone-50',
              )}
            >
              <p
                className={clsx(
                  'text-sm font-semibold leading-snug',
                  summary.tone === 'ok' && 'text-emerald-700',
                  summary.tone === 'bad' && 'text-red-700',
                  summary.tone === 'neutral' && 'text-stone-700',
                )}
              >
                {summary.headline}
              </p>
              <p className="mt-1 text-xs leading-relaxed text-stone-600">{summary.detail}</p>
              <p className="mt-1 text-xs leading-relaxed text-stone-400">{summary.hint}</p>
            </div>
          ) : null
        })()}

        {runDetail && (
          <div>
            <SectionLabel icon={<Gauge size={13} className="text-sky-600" />} text="Evaluator conclusions" />
            {!evaluated && (
              <p className="text-stone-400 leading-relaxed">
                Awaiting evaluation — the run has not produced evaluator evidence yet. Nothing here
                counts as success until it does.
              </p>
            )}
            {unverified && (
              <p className="rounded-lg border border-amber-200 bg-amber-50 p-2.5 text-amber-700 leading-relaxed">
                The backend reports success but attached no evaluator evidence. Treating this run as
                unverified — not a success.
              </p>
            )}
            {evaluated && (
              <div className="space-y-2">
                <div
                  className={clsx(
                    'flex items-center gap-2 rounded-lg border p-2.5 font-semibold text-xs shadow-xs',
                    success
                      ? 'border-emerald-200 bg-emerald-50 text-emerald-700'
                      : 'border-red-200 bg-red-50 text-red-700',
                  )}
                >
                  {success ? <CheckCircle2 size={15} className="text-emerald-500 shrink-0" /> : <XCircle size={15} className="text-red-500 shrink-0" />}
                  <span>{success ? 'Evaluator: task succeeded' : 'Evaluator: task did not succeed'}</span>
                </div>
                {(runDetail.evaluation?.reasons ?? []).map((reason, i) => (
                  <p key={i} className="text-stone-600 leading-relaxed">• {reason}</p>
                ))}
                {runDetail.failure_summary && (
                  <p className="text-stone-600 leading-relaxed">• {runDetail.failure_summary}</p>
                )}
                {runDetail.metrics && (
                  <p className="font-mono text-xs text-stone-400">
                    evaluator {runDetail.metrics.evaluator_version} · collisions{' '}
                    {runDetail.metrics.collision_count} · goal Δ{' '}
                    {runDetail.metrics.goal_distance.toFixed(2)} m
                  </p>
                )}
                {passRate && passRate.total > 0 && (
                  <div className="space-y-1">
                    <p className="font-mono text-xs text-stone-400">
                      automated tests {passRate.passed}/{passRate.total} passed · max joint error{' '}
                      {runDetail.metrics?.max_joint_error.toFixed(3) ?? '—'}
                    </p>
                    {runDetail.tests
                      .filter((test) => !test.passed)
                      .map((test) => (
                        <p key={test.id} className="text-xs text-red-600">
                          ✕ {test.joint ?? test.id}: {test.reason || `final error ${test.final_error.toFixed(3)}`}
                        </p>
                      ))}
                  </div>
                )}
              </div>
            )}
          </div>
        )}

        {runDetail && (
          <div>
            <SectionLabel icon={<FlaskConical size={13} className="text-amber-500" />} text="Execution events" />
            <div className="space-y-1 rounded-lg border border-border-subtle/60 bg-stone-50 p-2.5 font-mono text-xs text-stone-600">
              <p>run {runDetail.id.slice(0, 8)} · controller v{runDetail.controller_version} · seed {runDetail.seed}</p>
              <p className="text-stone-400">
                backend {BACKEND_LABELS[runDetail.backend] ?? runDetail.backend} · status {runDetail.status}
                {runDetail.exit_code !== null && ` · exit ${runDetail.exit_code}`}
              </p>
            </div>
          </div>
        )}

        {modelSectionVisible && (
          <div>
            <SectionLabel icon={<Bot size={13} className="text-purple-500" />} text="Model reasoning" />
            {fix.status === 'loading' && (
              <LoadingRow label={`Muse Spark is diagnosing… ${modelElapsed}s`} />
            )}
            {generation.status === 'error' && generation.error && (
              <div className="space-y-2">
                <p className="text-red-600">Generation failed: {generation.error}</p>
                <TraceView trace={generationTrace} hint={generationHint} />
              </div>
            )}
            {generation.data && fix.status === 'idle' && (
              <div className="space-y-2 text-stone-600 leading-relaxed">
                <p className="font-semibold text-stone-800">{generation.data.summary}</p>
                <p>
                  <span className="text-stone-500 font-medium">Expected: </span>
                  {generation.data.expected_behavior}
                </p>
                {generation.data.assumptions.length > 0 && (
                  <p>
                    <span className="text-stone-500 font-medium">Assumes: </span>
                    {generation.data.assumptions.join('; ')}
                  </p>
                )}
                {/* An installed starter robot replaced the model's files, so it
                    is the description that will actually be simulated. */}
                {robotInstall.data ? (
                  <RobotSummary robot={robotInstall.data} />
                ) : (
                  generation.data.robot && <RobotSummary robot={generation.data.robot} />
                )}
                <TraceView
                  trace={generationTrace}
                  meta={
                    generation.data.model_name
                      ? `${generation.data.model_name} · ${((generation.data.latency_ms ?? 0) / 1000).toFixed(1)}s`
                      : null
                  }
                />
              </div>
            )}
            {fix.status === 'error' && fix.error && (
              <div className="space-y-2">
                <p className="text-red-600">Diagnosis failed: {fix.error}</p>
                <TraceView trace={fixTrace} hint={fixHint} />
              </div>
            )}
            {lastRequest && generation.status === 'idle' && fix.status === 'idle' && (
              <p className="font-mono text-xs text-stone-400">
                Last request: {lastRequest.purpose} · {lastRequest.model_name} ·{' '}
                {lastRequest.latency_ms != null ? `${(lastRequest.latency_ms / 1000).toFixed(1)}s` : '—'} ·{' '}
                {lastRequest.prompt_version} · {lastRequest.status}
                {lastRequest.error_summary ? ` · ${lastRequest.error_summary}` : ''}
              </p>
            )}
            {fix.data && (
              <div className="space-y-2.5 rounded-xl border border-sky-200 bg-sky-50 p-3.5 shadow-xs">
                <p className="font-semibold text-sky-700 leading-snug">{fix.data.failure_hypothesis}</p>
                {fix.data.evidence.map((e, i) => (
                  <p key={i} className="text-stone-600 leading-relaxed">• {e}</p>
                ))}
                <p>
                  <span className="text-stone-500 font-medium">Proposed change: </span>
                  <span className="text-stone-700">{fix.data.proposed_change}</span>
                </p>
                <p>
                  <span className="text-stone-500 font-medium">Expected improvement: </span>
                  <span className="text-stone-700">{fix.data.expected_improvement}</span>
                </p>
                <p className="font-mono text-xs text-stone-400">
                  Applied to: {fix.data.updated_files.map((f) => f.path).join(', ') || '—'}
                  {' '}(editor already shows the corrected code)
                </p>
                {fix.data.robot && <RobotSummary robot={fix.data.robot} />}
                <div className="space-y-2">
                  {fix.data.updated_files.map((f) => (
                    <DiffView
                      key={f.path}
                      path={f.path}
                      oldText={fixBase[f.path] ?? null}
                      newText={f.content}
                    />
                  ))}
                </div>
                <div className="flex gap-2 pt-1.5">
                  <button
                    type="button"
                    onClick={() => fixRunId && void rerunRun(fixRunId, activeProjectId ?? undefined)}
                    disabled={!fixRunId || runAction === 'rerunning'}
                    className="inline-flex items-center gap-1.5 rounded-lg bg-sky-500 px-3 py-1.5 text-xs font-bold text-white shadow-sm transition-all hover:bg-sky-400 active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50"
                    title="Rerun the same scenario (world + seed) with the corrected controller"
                  >
                    <RotateCcw size={13} />
                    {runAction === 'rerunning' ? 'Rerunning…' : 'Rerun same scenario'}
                  </button>
                  <button
                    type="button"
                    onClick={dismissFix}
                    className="rounded-lg border border-border-subtle bg-white px-3 py-1.5 text-xs font-medium text-stone-600 transition-all hover:bg-stone-50 active:scale-[0.98]"
                  >
                    Dismiss
                  </button>
                </div>
                {fix.data && (
                  <TraceView
                    trace={fixTrace}
                    meta={
                      fix.data.model_name
                        ? `${fix.data.model_name} · ${((fix.data.latency_ms ?? 0) / 1000).toFixed(1)}s`
                        : null
                    }
                  />
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </Panel>
  )
}
