import { useEffect, useRef } from 'react'
import { Terminal } from 'lucide-react'
import { useExperimentStore } from '../../stores/experiment'

/** Indent a JSON payload embedded in a log line; untouched when absent. */
function prettyLine(line: string): string {
  const objStart = line.indexOf('{')
  const arrStart = line.indexOf('[')
  const starts = [objStart, arrStart].filter((i) => i !== -1)
  if (starts.length === 0) return line
  const start = Math.min(...starts)
  const open = line[start]
  const close = open === '{' ? '}' : ']'
  const end = line.lastIndexOf(close)
  if (end <= start) return line
  try {
    const parsed: unknown = JSON.parse(line.slice(start, end + 1))
    return `${line.slice(0, start)}${JSON.stringify(parsed, null, 2)}${line.slice(end + 1)}`
  } catch {
    return line
  }
}

export function LogsBody({ autoScroll }: { autoScroll: boolean }) {
  const logs = useExperimentStore((s) => s.logs)
  const activeRunId = useExperimentStore((s) => s.activeRunId)
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (autoScroll) bottomRef.current?.scrollIntoView({ block: 'end' })
  }, [logs, autoScroll])

  return (
    <div className="bg-base p-3.5 space-y-0.5 font-mono text-xs leading-relaxed">
      {logs.length === 0 && (
        <div className="flex items-center gap-2.5 p-4 text-xs text-stone-400">
          <Terminal size={15} />
          <span>{activeRunId ? 'No log lines yet for this run.' : 'Select or start a run to see logs.'}</span>
        </div>
      )}
      {logs.map((line, i) => (
        // Log lines are untrusted execution output; React escapes them by default.
        <p key={i} className="whitespace-pre-wrap break-words text-stone-600 hover:bg-stone-100/60 px-1 rounded transition-colors">
          {prettyLine(line)}
        </p>
      ))}
      <div ref={bottomRef} />
    </div>
  )
}
