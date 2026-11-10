import { Sparkles } from 'lucide-react'
import { useWorkspaceStore } from '../../stores/workspace'
import { useSettingsStore } from '../../stores/settings'
import { ActionButton, Panel } from '../../components/Panel'
import { useElapsedSeconds } from '../../components/useElapsedSeconds'

export function TaskInput() {
  const activeProjectId = useWorkspaceStore((s) => s.activeProjectId)
  const taskDraft = useWorkspaceStore((s) => s.taskDraft)
  const setTaskDraft = useWorkspaceStore((s) => s.setTaskDraft)
  const generate = useWorkspaceStore((s) => s.generate)
  const generation = useWorkspaceStore((s) => s.generation)
  const fix = useWorkspaceStore((s) => s.fix)
  const modelConfigured = useSettingsStore((s) => s.settings.data?.configured ?? null)
  const setSettingsOpen = useSettingsStore((s) => s.setOpen)

  const generating = generation.status === 'loading'
  const fixing = fix.status === 'loading'
  const generationElapsed = useElapsedSeconds(generating)
  const taskEmpty = taskDraft.trim().length === 0
  // Disable only on a known-missing key; unknown (loading/error) stays enabled.
  const needsKey = modelConfigured === false

  return (
    <Panel
      title="Task"
      className="shrink-0"
      actions={
        <ActionButton
          onClick={() => void generate()}
          disabled={!activeProjectId || taskEmpty || generating || fixing || needsKey}
          variant="primary"
          title={
            needsKey
              ? 'Set an API key in Settings to enable AI generation'
              : taskEmpty
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
        <label htmlFor="task-description" className="mb-2 block text-xs font-medium text-stone-500 leading-normal">
          Describe what the robot should do.
        </label>
        <textarea
          id="task-description"
          value={taskDraft}
          onChange={(e) => setTaskDraft(e.target.value)}
          rows={5}
          className="w-full resize-y rounded-xl border border-border-subtle bg-base p-3 text-xs leading-relaxed text-stone-800 outline-none transition-all placeholder:text-stone-400 focus:border-amber-500/60 focus:ring-1 focus:ring-amber-500/20 shadow-inner"
          placeholder="Walk to the green flag and stop safely…"
        />
        {generating && (
          <div
            className="mt-2.5 flex items-center gap-2 text-xs font-medium leading-relaxed text-stone-500"
            role="status"
            aria-live="polite"
          >
            <span className="h-3 w-3 shrink-0 animate-spin rounded-full border-2 border-stone-400 border-t-transparent" />
            AI model is generating the controller from this task… {generationElapsed}s
          </div>
        )}
        {needsKey && !generating && (
          <div className="mt-2.5 flex items-center justify-between gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
            <span>AI generation needs an API key.</span>
            <ActionButton onClick={() => setSettingsOpen(true)} title="Open AI model settings">
              Settings
            </ActionButton>
          </div>
        )}
      </div>
    </Panel>
  )
}
