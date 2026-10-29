import { useEffect } from 'react'
import { Bot, Download, Github, Linkedin } from 'lucide-react'
import { exportProjectUrl } from './api/client'
import { useWorkspaceStore } from './stores/workspace'
import { useExperimentStore } from './stores/experiment'
import { ExperimentBar } from './features/projects/ExperimentBar'
import { FileExplorer } from './features/projects/FileExplorer'
import { RunHistory } from './features/projects/RunHistory'
import { TaskInput } from './features/projects/TaskInput'
import { WorkspaceCenter } from './features/workspace/WorkspaceCenter'
import { DiagnosticsPanel } from './features/diagnostics/DiagnosticsPanel'
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
    <div className="flex shrink-0 flex-wrap items-center gap-2.5 border-b border-red-500/30 bg-red-500/10 px-5 py-2 text-xs text-red-200">
      <span className="h-2 w-2 rounded-full bg-red-400 ring-2 ring-red-400/20" />
      <span>
        Backend unreachable. Start the API server (default <span className="font-mono text-red-100">http://localhost:8000</span>) and retry.
      </span>
      <button
        type="button"
        onClick={() => void refreshProjects()}
        className="rounded-md border border-red-500/40 bg-red-500/15 px-2.5 py-1 text-xs font-medium hover:bg-red-500/25 transition-colors"
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
    <div className="flex h-full flex-col bg-base text-slate-100 antialiased">
      <header className="flex shrink-0 items-center gap-3.5 border-b border-border-subtle bg-sidebar/95 px-5 py-2.5 backdrop-blur-sm xl:grid xl:grid-cols-[320px_minmax(0,1fr)_420px] xl:gap-3.5 xl:px-3.5">
        <div className="flex items-center gap-3.5">
          <img
            src="/favicon.png"
            alt="HeyWalk logo"
            className="h-8 w-8 rounded-full object-cover ring-1 ring-white/10"
          />
          <div className="leading-tight">
            <h1 className="text-sm font-bold tracking-tight text-white">HeyWalk</h1>
            <p className="text-xs text-slate-400">From thought to motion</p>
          </div>
        </div>
        <span className="ml-2 hidden items-center gap-1.5 justify-self-start rounded-full border border-border-subtle/80 bg-white/[0.03] px-3 py-1 text-xs font-medium text-slate-400 sm:inline-flex xl:ml-0">
          <Bot size={13} className="text-slate-400" />
          Meta Global AI Developer Hackathon 2026
        </span>
        <div className="ml-auto flex items-center gap-3 xl:ml-0 xl:justify-self-end">
          <span
            className="inline-flex items-center gap-2 rounded-full border border-border-subtle/80 bg-white/[0.02] px-2.5 py-1 text-xs font-medium text-slate-300"
            title={backendOnline === false ? 'Backend unreachable' : 'Backend status'}
          >
            <span
              className={clsx(
                'h-2 w-2 rounded-full',
                backendOnline === false
                  ? 'bg-red-400 ring-2 ring-red-400/20'
                  : backendOnline
                    ? 'bg-emerald-400 ring-2 ring-emerald-400/20'
                    : 'bg-slate-500',
              )}
            />
            {backendOnline === false ? 'Offline' : backendOnline ? 'API connected' : '…'}
          </span>
          {activeProject && (
            <a
              href={exportProjectUrl(activeProject.id)}
              download={`${activeProject.name}.zip`}
              className="inline-flex items-center gap-1.5 rounded-lg border border-border-subtle bg-white/[0.05] px-3 py-1.5 text-xs font-medium text-slate-200 shadow-sm transition-all hover:bg-white/[0.09] hover:text-white active:scale-[0.98]"
              title="Download project source, config, and run summary (no secrets)"
            >
              <Download size={14} />
              Export
            </a>
          )}
        </div>
      </header>

      <BackendBanner />

      <main className="grid min-h-0 flex-1 grid-cols-1 gap-3.5 overflow-y-auto p-3.5 xl:grid-cols-[320px_minmax(0,1fr)_420px] xl:overflow-hidden">
        <div className="flex min-h-[420px] min-w-0 flex-col gap-3.5 xl:min-h-0 xl:overflow-hidden">
          <ExperimentBar />
          <FileExplorer />
          <RunHistory />
          <footer className="flex shrink-0 items-center justify-center gap-2 text-[11px] text-slate-500">
            <span>MIT Licence · © 2026 Wan Satya</span>
            <a
              href="https://linkedin.com/in/wawanbsetyawan"
              target="_blank"
              rel="noreferrer"
              title="LinkedIn"
              aria-label="LinkedIn"
              className="rounded p-1 hover:bg-white/5 hover:text-slate-300"
            >
              <Linkedin size={14} />
            </a>
            <a
              href="https://github.com/wansatya"
              target="_blank"
              rel="noreferrer"
              title="GitHub"
              aria-label="GitHub"
              className="rounded p-1 hover:bg-white/5 hover:text-slate-300"
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
    </div>
  )
}
