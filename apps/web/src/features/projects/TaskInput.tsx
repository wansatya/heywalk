import { Sparkles } from 'lucide-react'
import { useWorkspaceStore } from '../../stores/workspace'
import { ActionButton, Panel } from '../../components/Panel'

/** One-click objective presets: the runner knows these props by name. */
const TASK_EXAMPLES = [
  'Walk to the green flag and stop safely.',
  'Go to the box in front of you and stop.',
  'Kick the ball away from you, then stop.',
  'Kick the ball away, then walk to the green flag and stop safely.',
] as const

export function TaskInput() {
  const activeProjectId = useWorkspaceStore((s) => s.activeProjectId)
  const taskDraft = useWorkspaceStore((s) => s.taskDraft)
  const setTaskDraft = useWorkspaceStore((s) => s.setTaskDraft)
  const generate = useWorkspaceStore((s) => s.generate)
  const generation = useWorkspaceStore((s) => s.generation)
  const fix = useWorkspaceStore((s) => s.fix)

  const generating = generation.status === 'loading'
  const fixing = fix.status === 'loading'
  const taskEmpty = taskDraft.trim().length === 0

  return (
    <Panel
      title="Task"
      className="shrink-0"
      actions={
        <ActionButton
          onClick={() => void generate()}
          disabled={!activeProjectId || taskEmpty || generating || fixing}
          variant="primary"
          title={
            taskEmpty
              ? 'Describe the task first (or pick an example below)'
              : 'Generate the robot behavior from this task description'
          }
        >
          <Sparkles size={14} />
          {generating ? 'Generating…' : 'Generate'}
        </ActionButton>
      }
    >
      <div className="p-3.5">
        <label htmlFor="task-description" className="mb-2 block text-xs font-medium text-slate-400 leading-normal">
          Describe what the robot should do.
        </label>
        <textarea
          id="task-description"
          value={taskDraft}
          onChange={(e) => setTaskDraft(e.target.value)}
          rows={5}
          className="w-full resize-y rounded-xl border border-border-subtle bg-base p-3 text-xs leading-relaxed text-slate-100 outline-none transition-all placeholder:text-slate-500 focus:border-amber-400/60 focus:ring-1 focus:ring-amber-400/20 shadow-inner"
          placeholder="Walk to the green flag and stop safely…"
        />
        <div className="mt-2 flex flex-wrap gap-1.5">
          {TASK_EXAMPLES.map((example) => (
            <button
              key={example}
              type="button"
              onClick={() => setTaskDraft(example)}
              disabled={generating || fixing}
              className="rounded-full border border-border-subtle bg-white/[0.03] px-2.5 py-1 text-start text-[11px] text-slate-400 transition-colors hover:border-amber-400/40 hover:text-slate-200 disabled:opacity-50"
              title="Use this task description"
            >
              {example}
            </button>
          ))}
        </div>
      </div>
    </Panel>
  )
}
