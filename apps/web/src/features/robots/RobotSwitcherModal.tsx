import { useEffect } from 'react'
import { Bot, X } from 'lucide-react'
import { useWorkspaceStore } from '../../stores/workspace'
import type { StarterRobot } from '../../api/types'
import { ActionButton, ErrorNotice, LoadingRow } from '../../components/Panel'
import { clsx } from '../../components/clsx'

function RobotRow({
  robot,
  current,
  installing,
  onSwitch,
}: {
  robot: StarterRobot
  current: boolean
  installing: boolean
  onSwitch: () => void
}) {
  return (
    <div className="rounded-xl border border-border-subtle bg-white p-3.5 shadow-xs">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="flex flex-wrap items-center gap-2 text-sm font-semibold text-stone-900">
            {robot.name}
            {robot.source === 'foundry' && (
              <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider text-amber-700">
                Foundry
              </span>
            )}
            {current && (
              <span className="rounded-full bg-emerald-100 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider text-emerald-700">
                Current
              </span>
            )}
          </p>
          <p className="mt-1 text-xs leading-relaxed text-stone-500">{robot.summary}</p>
        </div>
        <ActionButton
          onClick={onSwitch}
          disabled={current || installing}
          variant={current ? 'default' : 'primary'}
          title={current ? 'Already installed' : `Install ${robot.name}`}
        >
          {installing ? 'Switching…' : current ? 'Installed' : 'Switch'}
        </ActionButton>
      </div>
      <p className="mt-2.5 font-mono text-[11px] text-stone-400 tabular-nums">
        {robot.joints.length} joints · {robot.tests.length} tests · {robot.license}
      </p>
      {robot.attribution && (
        <p className="mt-0.5 text-[11px] text-stone-400">{robot.attribution}</p>
      )}
    </div>
  )
}

/**
 * Switch-robot modal: lists the platform's robot catalog and installs the
 * chosen robot into the active project. Installs replace robot.xml and its
 * companions but never touch controller.py — the empty-controller hint in
 * the editor points at the sample import / regeneration next steps.
 */
export function RobotSwitcherModal() {
  const open = useWorkspaceStore((s) => s.robotModalOpen)
  const setOpen = useWorkspaceStore((s) => s.setRobotModalOpen)
  const catalog = useWorkspaceStore((s) => s.robotCatalog)
  const refreshCatalog = useWorkspaceStore((s) => s.refreshRobotCatalog)
  const installedRobotId = useWorkspaceStore((s) => s.installedRobotId)
  const installRobot = useWorkspaceStore((s) => s.installRobot)
  const robotInstall = useWorkspaceStore((s) => s.robotInstall)

  useEffect(() => {
    if (!open) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, setOpen])

  if (!open) return null

  const installing = robotInstall.status === 'loading'
  const robots = catalog.data ?? []

  const handleSwitch = async (robotId: string) => {
    await installRobot(robotId)
    if (useWorkspaceStore.getState().robotInstall.status === 'success') {
      setOpen(false)
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-stone-950/45 p-4"
      role="dialog"
      aria-modal="true"
      aria-label="Switch robot"
      onClick={() => {
        if (!installing) setOpen(false)
      }}
    >
      <div
        className="flex max-h-[85vh] w-full max-w-lg flex-col overflow-hidden rounded-2xl border border-border-subtle bg-elevated shadow-xl"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="flex shrink-0 items-center justify-between gap-2.5 border-b border-border-subtle/80 bg-sidebar/70 px-4 py-3">
          <h2 className="flex items-center gap-2 text-sm font-semibold text-stone-900">
            <Bot size={15} className="text-sky-600" />
            Switch robot
          </h2>
          <button
            type="button"
            onClick={() => setOpen(false)}
            disabled={installing}
            title="Close"
            className="rounded-lg p-1.5 text-stone-500 transition-colors hover:bg-stone-200/60 hover:text-stone-900 disabled:opacity-40"
          >
            <X size={15} />
          </button>
        </div>
        <div className="min-h-0 flex-1 space-y-2.5 overflow-y-auto p-4">
          <p className="text-xs leading-relaxed text-stone-500">
            Switching installs the robot model and its companions into this sequence.
            Your <span className="font-mono text-stone-600">controller.py</span> is left
            untouched — import the sample or regenerate it for the new joints.
          </p>
          {catalog.status === 'loading' && robots.length === 0 && (
            <LoadingRow label="Loading robot catalog…" />
          )}
          {catalog.status === 'error' && catalog.error && (
            <ErrorNotice message={catalog.error} onRetry={() => void refreshCatalog()} />
          )}
          {robots.map((robot) => (
            <RobotRow
              key={robot.id}
              robot={robot}
              current={robot.id === installedRobotId}
              installing={installing}
              onSwitch={() => void handleSwitch(robot.id)}
            />
          ))}
          {robotInstall.status === 'error' && robotInstall.error && (
            <ErrorNotice message={`Switch failed: ${robotInstall.error}`} />
          )}
        </div>
      </div>
    </div>
  )
}

/** Current-robot button for the Files panel header; opens the modal. */
export function RobotSwitchButton() {
  const installedRobotId = useWorkspaceStore((s) => s.installedRobotId)
  const setOpen = useWorkspaceStore((s) => s.setRobotModalOpen)
  const activeProjectId = useWorkspaceStore((s) => s.activeProjectId)
  if (!activeProjectId) return null
  const label = installedRobotId?.startsWith('foundry:')
    ? 'Foundry robot'
    : (installedRobotId ?? 'robot')
  return (
    <button
      type="button"
      onClick={() => setOpen(true)}
      title={installedRobotId ? `Robot: ${installedRobotId} — switch` : 'Choose a robot'}
      className={clsx(
        'flex items-center gap-1.5 rounded-lg border border-border-subtle bg-white px-2 py-1',
        'font-mono text-[11px] text-stone-600 shadow-sm transition-colors hover:bg-stone-50 hover:text-stone-900',
      )}
    >
      <Bot size={12} className="text-sky-600" />
      {label}
    </button>
  )
}
