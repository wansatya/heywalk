import { useState } from 'react'
import { FolderPlus, Trash2, X } from 'lucide-react'
import { useWorkspaceStore } from '../../stores/workspace'
import { useExperimentStore } from '../../stores/experiment'
import { ActionButton, ErrorNotice, LoadingRow } from '../../components/Panel'

/** Experiment switcher: new/switch/delete, like managing folders. */
export function ExperimentBar() {
  const projects = useWorkspaceStore((s) => s.projects)
  const activeProjectId = useWorkspaceStore((s) => s.activeProjectId)
  const newExperiment = useWorkspaceStore((s) => s.newExperiment)
  const selectProject = useWorkspaceStore((s) => s.selectProject)
  const deleteProject = useWorkspaceStore((s) => s.deleteProject)
  const projectsState = useWorkspaceStore((s) => s.projectsState)
  const refreshProjects = useWorkspaceStore((s) => s.refreshProjects)

  const fetchRuns = useExperimentStore((s) => s.fetchRuns)
  const clearProject = useExperimentStore((s) => s.clearProject)

  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)
  const [deleting, setDeleting] = useState(false)
  const [deleteError, setDeleteError] = useState<string | null>(null)
  const [confirmDelete, setConfirmDelete] = useState(false)

  const handleNewExperiment = async () => {
    setCreating(true)
    setCreateError(null)
    try {
      await newExperiment()
      const id = useWorkspaceStore.getState().activeProjectId
      if (id) await fetchRuns(id)
    } catch (err) {
      setCreateError(err instanceof Error ? err.message : 'Could not create the sequence.')
    } finally {
      setCreating(false)
    }
  }

  const handleSelectProject = async (id: string) => {
    setConfirmDelete(false)
    await selectProject(id)
    await fetchRuns(id)
  }

  const handleDeleteProject = async () => {
    if (!activeProjectId) return
    if (!confirmDelete) {
      setConfirmDelete(true)
      return
    }
    setDeleting(true)
    setDeleteError(null)
    try {
      const next = await deleteProject(activeProjectId)
      setConfirmDelete(false)
      if (next) await fetchRuns(next)
      else clearProject()
    } catch (err) {
      setDeleteError(err instanceof Error ? err.message : 'Could not delete the sequence.')
    } finally {
      setDeleting(false)
    }
  }

  return (
    <div className="shrink-0 rounded-xl border border-border-subtle bg-elevated p-2.5 shadow-sm">
      {projectsState.status === 'error' && projectsState.error && (
        <ErrorNotice message={projectsState.error} onRetry={() => void refreshProjects()} />
      )}
      {createError && <ErrorNotice message={createError} />}
      {deleteError && <ErrorNotice message={deleteError} />}
      {projectsState.status === 'loading' && projects.length === 0 ? (
        <LoadingRow label="Loading sequences…" />
      ) : (
        <div className="flex items-center gap-1.5">
          <select
            id="project-select"
            value={activeProjectId ?? ''}
            onChange={(e) => void handleSelectProject(e.target.value)}
            disabled={projects.length === 0}
            aria-label="Active sequence"
            className="min-w-0 flex-1 rounded-lg border border-border-subtle bg-base px-3 py-1.5 text-xs font-medium text-slate-100 outline-none transition-all focus:border-amber-400/60 focus:ring-1 focus:ring-amber-400/20 disabled:opacity-50"
          >
            {projects.length === 0 && <option value="">No sequences yet</option>}
            {projects.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
          <ActionButton
            onClick={() => void handleNewExperiment()}
            disabled={creating}
            variant="ghost"
            title="Create a new sequence (MicroDuck is installed automatically with an empty controller)"
          >
            <FolderPlus size={14} />
            {creating ? 'Creating…' : ''}
          </ActionButton>
          {confirmDelete && !deleting && (
            <ActionButton
              onClick={() => setConfirmDelete(false)}
              variant="ghost"
              title="Keep this sequence"
            >
              <X size={14} />
            </ActionButton>
          )}
          <ActionButton
            onClick={() => void handleDeleteProject()}
            disabled={!activeProjectId || deleting}
            variant={confirmDelete ? 'danger' : 'ghost'}
            title={
              confirmDelete
                ? 'Click again to permanently delete this sequence with all its files, runs, and evidence'
                : 'Delete this sequence with all its files, runs, and evidence'
            }
          >
            <Trash2 size={14} />
            {deleting ? 'Deleting…' : confirmDelete ? 'Confirm' : ''}
          </ActionButton>
        </div>
      )}
    </div>
  )
}
