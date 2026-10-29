import { CheckCircle2, XCircle } from 'lucide-react'
import type { TraceStep } from '../api/types'

function formatMs(ms: number): string {
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(1)}s`
}

/** Compact under-the-hood stage checklist for a generate/fix operation. */
export function TraceView({
  trace,
  meta,
  hint,
}: {
  trace: TraceStep[]
  meta?: string | null
  hint?: string | null
}) {
  if (trace.length === 0 && !meta && !hint) return null
  return (
    <div className="space-y-1.5 rounded-xl border border-border-subtle bg-black/40 p-3 font-mono text-xs shadow-xs">
      {meta && <p className="font-sans text-xs font-semibold text-slate-300">{meta}</p>}
      {hint && <p className="font-sans text-xs text-amber-200/90 leading-relaxed">Hint: {hint}</p>}
      {trace.map((step, i) => (
        <p key={`${step.stage}-${i}`} className="flex items-center gap-2 text-slate-300 py-0.5">
          {step.status === 'error' ? (
            <XCircle size={13} className="shrink-0 text-red-400" />
          ) : (
            <CheckCircle2 size={13} className="shrink-0 text-emerald-400" />
          )}
          <span className="min-w-0 truncate">
            <span className="text-slate-200 font-medium">{step.stage}</span>
            <span className="text-slate-400"> · {formatMs(step.ms)}</span>
            {step.detail && <span className="text-slate-400"> · {step.detail}</span>}
          </span>
        </p>
      ))}
    </div>
  )
}
