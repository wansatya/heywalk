import { useEffect } from 'react'
import { Bot, Download, Github, Linkedin, Settings } from 'lucide-react'
import { exportProjectUrl } from './api/client'
import { useWorkspaceStore } from './stores/workspace'
import { useExperimentStore } from './stores/experiment'
import { useFoundryStore } from './stores/foundry'
import { useSettingsStore } from './stores/settings'
import { ExperimentBar } from './features/projects/ExperimentBar'
import { FileExplorer } from './features/projects/FileExplorer'
import { RobotSwitcherModal } from './features/robots/RobotSwitcherModal'
import { RunHistory } from './features/projects/RunHistory'
import { TaskInput } from './features/projects/TaskInput'
import { WorkspaceCenter } from './features/workspace/WorkspaceCenter'
import { DiagnosticsPanel } from './features/diagnostics/DiagnosticsPanel'
import { FoundryPage } from './features/foundry/FoundryPage'
import { SettingsModal } from './features/settings/SettingsModal'
import { clsx } from './components/clsx'

function BackendBanner() {
  const projectsState = useWorkspaceStore((s) => s.projectsState)
  const refreshProjects = useWorkspaceStore((s) => s.refreshProjects)
  const backendOnline = useExperimentStore((s) => s.backendOnline)

  const offline =
    backendOnline === false ||
    (projectsState.status === 'error' && (projectsState.data ?? []).length === 0)
  if (!offline) return null

  return (
    <div className="flex shrink-0 flex-wrap items-center gap-2.5 border-b border-red-300/50 bg-red-50 px-5 py-2 text-xs text-red-700">
      <span className="h-2 w-2 rounded-full bg-red-500 ring-2 ring-red-200" />
      <span>
        Backend unreachable. Start the API server (default <span className="font-mono text-red-800">http://localhost:8000</span>) and retry.
      </span>
      <button
        type="button"
        onClick={() => void refreshProjects()}
        className="rounded-md border border-red-300 bg-red-100 px-2.5 py-1 text-xs font-medium hover:bg-red-200 transition-colors"
      >
        Retry
      </button>
    </div>
  )
}

export default function App() {
  const activeProjectId = useWorkspaceStore((s) => s.activeProjectId)
  const projects = useWorkspaceStore((s) => s.projects)
  const backendOnline = useExperimentStore((s) => s.backendOnline)
  const foundryOpen = useFoundryStore((s) => s.open)
  // const setFoundryOpen = useFoundryStore((s) => s.setOpen)
  const setSettingsOpen = useSettingsStore((s) => s.setOpen)

  useEffect(() => {
    const workspace = useWorkspaceStore.getState()
    const experiments = useExperimentStore.getState()
    void workspace.refreshProjects().then(() => {
      const id = useWorkspaceStore.getState().activeProjectId
      if (id) void experiments.fetchRuns(id)
      else experiments.setBackendOnline(true)
    })
  }, [])

  const activeProject = projects.find((p) => p.id === activeProjectId) ?? null

  return (
    <div className="flex h-full flex-col bg-base text-stone-800 antialiased">
      <header className="flex shrink-0 flex-wrap items-center gap-3.5 border-b border-border-subtle bg-sidebar/95 px-5 py-2.5 backdrop-blur-sm xl:grid xl:grid-cols-[320px_minmax(0,1fr)_max-content] xl:gap-3.5 xl:px-3.5">
        <div className="flex shrink-0 items-center gap-3.5">
          <img
            src="/favicon.png"
            alt="HeyWalk logo"
            className="h-8 w-8 rounded-full object-cover ring-1 ring-stone-200"
          />
          <div className="leading-tight">
            <h1 className="text-sm font-bold tracking-tight text-stone-900">HeyWalk</h1>
            <p className="text-xs text-stone-500">From thought to motion</p>
          </div>
        </div>
        <span className="ml-2 hidden shrink-0 items-center gap-1.5 justify-self-start rounded-full border border-border-subtle bg-stone-100/60 px-3 py-1 text-xs font-medium text-stone-500 sm:inline-flex xl:ml-0">
          <Bot size={13} className="text-stone-400" />
          Bring Your Own Model!
        </span>
        <div className="ml-auto flex shrink-0 items-center gap-2 sm:gap-3 xl:ml-0 xl:justify-self-end">
          {/* <button
            type="button"
            onClick={() => {
              if (foundryOpen) {
                setFoundryOpen(false)
                void useWorkspaceStore.getState().refreshProjects()
              } else {
                setFoundryOpen(true)
              }
            }}
            className={clsx(
              'inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-lg border px-3 py-1.5 text-xs font-medium shadow-sm transition-all active:scale-[0.98]',
              foundryOpen
                ? 'border-amber-300 bg-amber-100 text-amber-800'
                : 'border-border-subtle bg-white text-stone-600 hover:bg-stone-50 hover:text-stone-900',
            )}
            title={foundryOpen ? 'Back to the IDE' : 'Generate a robot from a reference image'}
          >
            <Hammer size={14} />
            {foundryOpen ? 'Back to IDE' : 'Robot Foundry'}
          </button> */}
          <button
            type="button"
            onClick={() => setSettingsOpen(true)}
            className="inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-lg border border-border-subtle bg-white px-3 py-1.5 text-xs font-medium text-stone-600 shadow-sm transition-all hover:bg-stone-50 hover:text-stone-900 active:scale-[0.98]"
            title="AI model settings: API URL, model name, and API key"
          >
            <Settings size={14} />
            Settings
          </button>
          <span
            className="inline-flex shrink-0 items-center gap-2 whitespace-nowrap rounded-full border border-border-subtle bg-stone-50 px-2.5 py-1 text-xs font-medium text-stone-600"
            title={backendOnline === false ? 'Backend unreachable' : 'Backend status'}
          >
            <span
              className={clsx(
                'h-2 w-2 rounded-full',
                backendOnline === false
                  ? 'bg-red-500 ring-2 ring-red-200'
                  : backendOnline
                    ? 'bg-emerald-500 ring-2 ring-emerald-200'
                    : 'bg-stone-400',
              )}
            />
            {backendOnline === false ? 'Offline' : backendOnline ? 'API connected' : '…'}
          </span>
          {activeProject && (
            <a
              href={exportProjectUrl(activeProject.id)}
              download={`${activeProject.name}.zip`}
              className="inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-lg border border-border-subtle bg-white px-3 py-1.5 text-xs font-medium text-stone-600 shadow-sm transition-all hover:bg-stone-50 hover:text-stone-900 active:scale-[0.98]"
              title="Download project source, config, and run summary (no secrets)"
            >
              <Download size={14} />
              Export ROS
            </a>
          )}
        </div>
      </header>

      <BackendBanner />

      {foundryOpen ? (
        <FoundryPage />
      ) : (
        <main className="grid min-h-0 flex-1 grid-cols-1 gap-3.5 overflow-y-auto p-3.5 xl:grid-cols-[320px_minmax(0,1fr)_420px] xl:overflow-hidden">
          <div className="flex min-h-[420px] min-w-0 flex-col gap-3.5 xl:min-h-0 xl:overflow-hidden">
            <ExperimentBar />
            <FileExplorer />
            <RunHistory />
            <footer className="flex shrink-0 items-center justify-center gap-2 text-[11px] text-stone-400">
              <span>Apache-2.0 Licence · © 2026 Wan Satya</span>
              <a
                href="https://linkedin.com/in/wawanbsetyawan"
                target="_blank"
                rel="noreferrer"
                title="LinkedIn"
                aria-label="LinkedIn"
                className="rounded p-1 hover:bg-stone-200/60 hover:text-stone-600"
              >
                <Linkedin size={14} />
              </a>
              <a
                href="https://github.com/wansatya"
                target="_blank"
                rel="noreferrer"
                title="GitHub"
                aria-label="GitHub"
                className="rounded p-1 hover:bg-stone-200/60 hover:text-stone-600"
              >
                <Github size={14} />
              </a>
            </footer>
          </div>

          <div className="flex min-h-[560px] min-w-0 flex-col xl:min-h-0 xl:overflow-y-auto">
            <WorkspaceCenter />
          </div>

          <div className="flex min-h-[560px] min-w-0 flex-col gap-3.5 xl:min-h-0 xl:overflow-y-auto">
            <TaskInput />
            <DiagnosticsPanel />
          </div>
        </main>
      )}

      <RobotSwitcherModal />
      <SettingsModal />
    </div>
  )
}
