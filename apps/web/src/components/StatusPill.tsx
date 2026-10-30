import { clsx } from './clsx'
import type { RunStatus } from '../api/types'

const STATUS_STYLES: Record<RunStatus, string> = {
  queued: 'bg-stone-100 text-stone-600 border-stone-200',
  preparing: 'bg-sky-50 text-sky-700 border-sky-200',
  running: 'bg-amber-50 text-amber-700 border-amber-200',
  evaluating: 'bg-sky-50 text-sky-700 border-sky-200',
  succeeded: 'bg-emerald-50 text-emerald-700 border-emerald-200',
  failed: 'bg-red-50 text-red-700 border-red-200',
  timeout: 'bg-orange-50 text-orange-700 border-orange-200',
  cancelled: 'bg-stone-100 text-stone-500 border-stone-200',
  error: 'bg-red-50 text-red-700 border-red-200',
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
