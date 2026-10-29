import { clsx } from './clsx'
import type { RunStatus } from '../api/types'

const STATUS_STYLES: Record<RunStatus, string> = {
  queued: 'bg-slate-500/15 text-slate-300 border-slate-500/30',
  preparing: 'bg-sky-500/15 text-sky-300 border-sky-500/30',
  running: 'bg-amber-500/15 text-amber-300 border-amber-500/30',
  evaluating: 'bg-sky-500/15 text-sky-300 border-sky-500/30',
  succeeded: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30',
  failed: 'bg-red-500/15 text-red-300 border-red-500/30',
  timeout: 'bg-orange-500/15 text-orange-300 border-orange-500/30',
  cancelled: 'bg-slate-500/15 text-slate-400 border-slate-500/30',
  error: 'bg-red-500/15 text-red-300 border-red-500/30',
}

const STATUS_LABELS: Record<RunStatus, string> = {
  queued: 'Queued',
  preparing: 'Preparing',
  running: 'Running',
  evaluating: 'Evaluating',
  succeeded: 'Succeeded',
  failed: 'Failed',
  timeout: 'Timeout',
  cancelled: 'Cancelled',
  error: 'Error',
}

export function StatusPill({ status, pulse = false }: { status: RunStatus; pulse?: boolean }) {
  const live = pulse || status === 'running' || status === 'evaluating' || status === 'preparing'
  return (
    <span
      className={clsx(
        'inline-flex shrink-0 items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[11px] font-semibold tracking-wide',
        STATUS_STYLES[status],
      )}
    >
      <span className={clsx('h-1.5 w-1.5 rounded-full bg-current', live && 'animate-pulse')} />
      {STATUS_LABELS[status]}
    </span>
  )
}
