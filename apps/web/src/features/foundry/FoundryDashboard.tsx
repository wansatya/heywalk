import { useEffect, useState } from 'react'
import { Bot, Box, Camera, Play, Plus, ScanLine, Trash2, Wrench } from 'lucide-react'
import { useFoundryStore } from '../../stores/foundry'
import { useWorkspaceStore } from '../../stores/workspace'
import type { StarterRobot } from '../../api/types'
import { ActionButton, ErrorNotice, LoadingRow } from '../../components/Panel'
import { clsx } from '../../components/clsx'

const CATEGORIES = [
  { id: 'biped', label: 'Biped (MicroDuck-like)' },
  { id: 'humanoid', label: 'Humanoid' },
  { id: 'wheeled', label: 'Wheeled' },
  { id: 'arm', label: 'Robot arm' },
  { id: 'vehicle', label: 'Vehicle' },
]

const STATUS_STYLES: Record<string, string> = {
  DRAFT: 'bg-stone-100 text-stone-600',
  ANALYZING: 'bg-sky-100 text-sky-700',
  RECONSTRUCTING: 'bg-sky-100 text-sky-700',
  ASSEMBLING: 'bg-sky-100 text-sky-700',
  GENERATING: 'bg-sky-100 text-sky-700',
  VALIDATING: 'bg-sky-100 text-sky-700',
  NEEDS_REVIEW: 'bg-amber-100 text-amber-700',
  READY: 'bg-emerald-100 text-emerald-700',
  FAILED: 'bg-red-100 text-red-700',
}

function StatusBadge({ status }: { status: string }) {
  return (
    <span
      className={clsx(
        'rounded-full px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide',
        STATUS_STYLES[status] ?? 'bg-sky-100 text-sky-700',
      )}
    >
      {status.replaceAll('_', ' ')}
    </span>
  )
}

export function FoundryDashboard() {
  const projects = useFoundryStore((s) => s.projects)
  const projectsState = useFoundryStore((s) => s.projectsState)
  const actionError = useFoundryStore((s) => s.actionError)
  const createProject = useFoundryStore((s) => s.createProject)
  const selectProject = useFoundryStore((s) => s.selectProject)
  const deleteProject = useFoundryStore((s) => s.deleteProject)
  const robotCatalog = useWorkspaceStore((s) => s.robotCatalog)
  const refreshRobotCatalog = useWorkspaceStore((s) => s.refreshRobotCatalog)
  const newExperiment = useWorkspaceStore((s) => s.newExperiment)

  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [category, setCategory] = useState('biped')
  const [creating, setCreating] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null)
  const [openingRobot, setOpeningRobot] = useState<string | null>(null)
  const [platformError, setPlatformError] = useState<string | null>(null)

  useEffect(() => {
    void refreshRobotCatalog()
  }, [refreshRobotCatalog])

  const handleCreate = async () => {
    if (!name.trim()) return
    setCreating(true)
    const project = await createProject(name.trim(), description.trim(), category)
    setCreating(false)
    if (project) {
      setName('')
      setDescription('')
      await selectProject(project.id)
    }
  }

  const handleOpenRobot = async (robot: StarterRobot) => {
    setPlatformError(null)
    setOpeningRobot(robot.id)
    try {
      await newExperiment(robot.name, robot.id)
      if (useWorkspaceStore.getState().robotInstall.status === 'success') {
        useFoundryStore.getState().setOpen(false)
      } else {
        setPlatformError(
          useWorkspaceStore.getState().robotInstall.error ??
          `Could not install ${robot.name}.`,
        )
      }
    } catch (err) {
      setPlatformError(
        err instanceof Error ? err.message : `Could not open ${robot.name}.`,
      )
    } finally {
      setOpeningRobot(null)
    }
  }

  const platformRobots = (robotCatalog.data ?? []).filter(
    (robot) => (robot.source ?? 'bundled') === 'bundled',
  )

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
      <div className="mx-auto flex w-full max-w-6xl flex-col gap-5 p-5">
        <section className="overflow-hidden rounded-2xl border border-border-subtle bg-gradient-to-br from-stone-900 via-stone-800 to-amber-900 px-6 py-6 text-white shadow-lg">
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div className="max-w-3xl">
              <p className="mb-1 text-[11px] font-bold uppercase tracking-[0.18em] text-amber-300">
                Robot Foundry
              </p>
              <h2 className="text-2xl font-bold tracking-tight">From a photo to a runnable robot</h2>
              <p className="mt-2 text-sm leading-relaxed text-stone-200">
                Upload a reference image, review the segmented parts, generate real meshes, and
                compile a MuJoCo model. Reconstruction, MJCF generation, and validation stay
                separate so each stage can be inspected and re-run.
              </p>
            </div>
            <div className="flex items-center gap-2 rounded-xl bg-white/10 px-3 py-2 text-xs text-stone-200 ring-1 ring-white/15">
              <Bot size={16} className="text-amber-300" />
              Honest by default: heuristic output and estimated physics are labelled.
            </div>
          </div>
        </section>

        {actionError && <ErrorNotice message={actionError} />}

        <section className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_360px]">
          <div className="rounded-2xl border border-border-subtle bg-elevated p-4 shadow-sm">
            <div className="mb-3 flex items-center justify-between">
              <h3 className="text-sm font-bold text-stone-800">Your robots</h3>
              <span className="text-xs text-stone-400">{projects.length} project{projects.length === 1 ? '' : 's'}</span>
            </div>
            {projectsState.status === 'loading' && projects.length === 0 ? (
              <LoadingRow label="Loading Foundry projects…" />
            ) : projects.length === 0 ? (
              <div className="flex flex-col items-center gap-2 rounded-xl border border-dashed border-border-subtle bg-base/60 px-4 py-10 text-center">
                <ScanLine size={28} className="text-stone-300" />
                <p className="text-sm font-medium text-stone-600">No robots yet</p>
                <p className="max-w-sm text-xs leading-relaxed text-stone-400">
                  Create a project, upload one or more photos, then run the stages. A normal
                  HeyWalk sequence is created automatically for the simulator.
                </p>
              </div>
            ) : (
              <ul className="grid gap-2.5 sm:grid-cols-2">
                {projects.map((project) => (
                  <li
                    key={project.id}
                    className="group flex flex-col overflow-hidden rounded-xl border border-border-subtle bg-base/50 transition-all hover:-translate-y-0.5 hover:border-amber-300 hover:shadow-md"
                  >
                    <button
                      type="button"
                      onClick={() => void selectProject(project.id)}
                      className="flex flex-1 flex-col text-left"
                    >
                      <div className="relative h-28 w-full overflow-hidden bg-stone-100">
                        {project.thumbnail_asset_url ? (
                          <img
                            src={project.thumbnail_asset_url}
                            alt=""
                            className="h-full w-full object-cover transition-transform duration-300 group-hover:scale-[1.03]"
                          />
                        ) : (
                          <div className="flex h-full items-center justify-center text-stone-300">
                            <Box size={30} />
                          </div>
                        )}
                        <span className="absolute left-2 top-2">
                          <StatusBadge status={project.status} />
                        </span>
                      </div>
                      <div className="flex flex-col gap-1 p-3">
                        <span className="truncate text-sm font-semibold text-stone-800">{project.name}</span>
                        <span className="text-[11px] capitalize text-stone-400">
                          {project.category} · v{project.latest_version ?? '—'} · {project.component_count} parts
                        </span>
                        <span className="text-[11px] text-stone-400">
                          {project.joint_count} joints · {project.actuated_joint_count} actuated
                        </span>
                      </div>
                    </button>
                    <div className="flex items-center justify-between border-t border-border-subtle/70 px-3 py-2">
                      <button
                        type="button"
                        onClick={() => void selectProject(project.id)}
                        className="inline-flex items-center gap-1 text-[11px] font-semibold text-amber-700 hover:text-amber-900"
                      >
                        <Wrench size={12} /> Open workspace
                      </button>
                      <button
                        type="button"
                        onClick={() => {
                          if (confirmDelete === project.id) {
                            void deleteProject(project.id)
                            setConfirmDelete(null)
                          } else {
                            setConfirmDelete(project.id)
                          }
                        }}
                        title="Delete this Foundry robot package"
                        className={clsx(
                          'inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px]',
                          confirmDelete === project.id
                            ? 'bg-red-100 font-semibold text-red-700'
                            : 'text-stone-400 hover:bg-red-50 hover:text-red-600',
                        )}
                      >
                        <Trash2 size={12} />
                        {confirmDelete === project.id ? 'Confirm' : ''}
                      </button>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="flex flex-col gap-3 rounded-2xl border border-border-subtle bg-elevated p-4 shadow-sm">
            <h3 className="text-sm font-bold text-stone-800">New robot</h3>
            <label className="flex flex-col gap-1 text-xs font-medium text-stone-500">
              Name
              <input
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder="e.g. Workshop rover"
                maxLength={120}
                className="rounded-lg border border-border-subtle bg-base px-3 py-2 text-sm text-stone-800 outline-none focus:border-amber-500/60 focus:ring-1 focus:ring-amber-500/20"
              />
            </label>
            <label className="flex flex-col gap-1 text-xs font-medium text-stone-500">
              Category
              <select
                value={category}
                onChange={(event) => setCategory(event.target.value)}
                className="rounded-lg border border-border-subtle bg-base px-3 py-2 text-sm text-stone-800 outline-none focus:border-amber-500/60"
              >
                {CATEGORIES.map((entry) => (
                  <option key={entry.id} value={entry.id}>{entry.label}</option>
                ))}
              </select>
            </label>
            <label className="flex flex-col gap-1 text-xs font-medium text-stone-500">
              Description
              <textarea
                value={description}
                onChange={(event) => setDescription(event.target.value)}
                rows={3}
                placeholder="Optional: what is this robot for?"
                maxLength={4000}
                className="resize-none rounded-lg border border-border-subtle bg-base px-3 py-2 text-sm text-stone-800 outline-none focus:border-amber-500/60 focus:ring-1 focus:ring-amber-500/20"
              />
            </label>
            <ActionButton
              onClick={() => void handleCreate()}
              disabled={!name.trim() || creating}
              variant="primary"
            >
              <Plus size={14} />
              {creating ? 'Creating…' : 'Create robot project'}
            </ActionButton>
            <p className="flex items-start gap-1.5 text-[11px] leading-relaxed text-stone-400">
              <Camera size={13} className="mt-0.5 shrink-0" />
              One image is enough to start; several views improve the visible geometry. Hidden
              surfaces cannot be reconstructed from a single photo.
            </p>
          </div>
        </section>

        <section className="rounded-2xl border border-border-subtle bg-elevated p-4 shadow-sm">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <div>
              <h3 className="text-sm font-bold text-stone-800">Platform robots</h3>
              <p className="text-xs text-stone-400">
                Bundled robots are ready to open in a new sequence. Foundry robots you generate
                join them in the simulator&apos;s switch-robot catalog.
              </p>
            </div>
            <span className="text-xs text-stone-400">
              {platformRobots.length} bundled
            </span>
          </div>
          {platformError && <ErrorNotice message={platformError} />}
          {robotCatalog.status === 'loading' && platformRobots.length === 0 ? (
            <LoadingRow label="Loading platform robots…" />
          ) : robotCatalog.status === 'error' && robotCatalog.error ? (
            <ErrorNotice
              message={robotCatalog.error}
              onRetry={() => void refreshRobotCatalog()}
            />
          ) : platformRobots.length === 0 ? (
            <p className="rounded-xl border border-dashed border-border-subtle bg-base/60 px-4 py-6 text-center text-xs text-stone-400">
              No bundled robots are available from this backend.
            </p>
          ) : (
            <ul className="grid gap-2.5 sm:grid-cols-2 lg:grid-cols-3">
              {platformRobots.map((robot) => (
                <li
                  key={robot.id}
                  className="flex flex-col rounded-xl border border-border-subtle bg-base/50 p-3"
                >
                  <div className="flex items-start gap-2.5">
                    <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-sky-100 text-sky-700">
                      <Bot size={18} />
                    </span>
                    <div className="min-w-0">
                      <p className="flex items-center gap-2 text-sm font-semibold text-stone-800">
                        {robot.name}
                        <span className="rounded-full bg-sky-100 px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide text-sky-700">
                          Bundled
                        </span>
                      </p>
                      <p className="mt-0.5 line-clamp-2 text-[11px] leading-relaxed text-stone-500">
                        {robot.summary}
                      </p>
                    </div>
                  </div>
                  <div className="mt-auto flex items-center justify-between gap-2 pt-3">
                    <span className="font-mono text-[10px] text-stone-400 tabular-nums">
                      {robot.joints.length} joints · {robot.tests.length} tests
                    </span>
                    <ActionButton
                      onClick={() => void handleOpenRobot(robot)}
                      disabled={openingRobot !== null}
                      variant="primary"
                      title={`Create a new sequence with ${robot.name}`}
                    >
                      <Play size={12} />
                      {openingRobot === robot.id ? 'Opening…' : 'Open in simulator'}
                    </ActionButton>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </div>
  )
}
