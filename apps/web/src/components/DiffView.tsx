import { useState } from 'react'
import { FileDiff } from 'lucide-react'
import { changedHunks, diffLines } from './diff'
import { clsx } from './clsx'

export function DiffView({ path, oldText, newText }: { path: string; oldText: string | null; newText: string }) {
  const [open, setOpen] = useState(true)
  const rows = oldText === null ? null : diffLines(oldText, newText)
  const hunks = rows ? changedHunks(rows) : []
  const added = rows?.filter((r) => r.kind === 'add').length ?? 0
  const removed = rows?.filter((r) => r.kind === 'del').length ?? 0

  return (
    <div className="overflow-hidden rounded-xl border border-border-subtle bg-elevated shadow-xs">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="flex w-full items-center gap-2 bg-white/[0.04] px-3 py-2 font-mono text-xs text-slate-200 transition-colors hover:bg-white/[0.08]"
        title={open ? 'Collapse diff' : 'Expand diff'}
      >
        <FileDiff size={14} className="text-sky-300" />
        <span className="truncate font-semibold">{path}</span>
        <span className="ml-auto shrink-0 font-sans text-xs">
          {oldText === null ? (
            <span className="rounded bg-emerald-500/15 px-1.5 py-0.5 font-semibold text-emerald-300">new file</span>
          ) : (
            <span className="font-mono text-xs">
              <span className="font-semibold text-emerald-300">+{added}</span>
              <span className="text-slate-500"> / </span>
              <span className="font-semibold text-red-300">−{removed}</span>
            </span>
          )}
        </span>
      </button>
      {open && (
        <div className="max-h-56 overflow-auto bg-black/40 font-mono text-xs leading-relaxed py-1">
          {oldText === null && (
            <p className="px-3 py-1.5 text-slate-400">New file created by the correction — no previous version.</p>
          )}
          {rows === null && oldText !== null && (
            <p className="px-3 py-1.5 text-slate-400">File too large for an inline diff — see the editor.</p>
          )}
          {rows !== null && hunks.length === 0 && (
            <p className="px-3 py-1.5 text-slate-400">No line changes.</p>
          )}
          {hunks.map((row, idx) => (
            <div
              key={idx}
              className={clsx(
                'flex gap-2 whitespace-pre-wrap px-3 py-0.5',
                row.kind === 'add' && 'bg-emerald-500/15 text-emerald-200',
                row.kind === 'del' && 'bg-red-500/15 text-red-200',
                row.kind === 'same' && 'text-slate-500',
              )}
            >
              <span className="w-8 shrink-0 select-none text-right text-slate-500">
                {row.kind === 'add' ? row.newNo : row.oldNo}
              </span>
              <span className="w-3 shrink-0 select-none font-bold">
                {row.kind === 'add' ? '+' : row.kind === 'del' ? '−' : ' '}
              </span>
              <span className="min-w-0 break-words">{row.text || ' '}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
